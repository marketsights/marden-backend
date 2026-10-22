"""The email outbox: instant red alerts, daily and weekly digests, and sending with retries.

    sweep_instant()     red alerts of the last day not yet in an instant email -> one row per user
    build_digests(now)  daily digest (07:00 ET, the alerts not already emailed) and weekly summary (Mondays
                        07:00 ET, the week's alerts) -> one row per user and period
    send_due()          due 'queued' rows -> provider; sent | failed | not_configured | cancelled; a sent
                        email marks its alerts emailed and re-sends them on the stream (Alert.emailed)

Rows are idempotent by `idempotency_key` (`instant:<org>:<user>:<first alert id>`,
`daily:<org>:<user>:<YYYY-MM-DD>`, `weekly:<org>:<user>:<Monday>`), so rebuilding them changes nothing.
At send time the member's access and settings are checked again (an unsubscribe or a lapsed product
cancels the row). Without a provider, every due row becomes 'not_configured' with the missing variable
names: nothing pretends to have been sent. A transient failure (network, 429, 5xx) is retried after 1, 2,
4, 8 and 16 minutes, then the row is 'failed'; a permanent one (4xx) fails at once. A row that raises while it
is rendered or sent is retried on the same schedule (its error type in last_error, the trace in the worker
log), so it never holds up the rows behind it. Delivery is at least once: Resend dedupes on the
Idempotency-Key header; a worker dying mid-send can repeat a Postmark email.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, time, timedelta
from typing import Any

import httpx
import psycopg

from marden.alerts.email_templates import render, subject_for
from marden.alerts.engine import row_alert
from marden.alerts.fanout import live_member, live_members
from marden.alerts.providers import Message, Provider, make_provider
from marden.alerts.settings import EmailConfig, email_config
from marden.core import db, events
from marden.core.db import Conn
from marden.core.jsonutil import jsonb
from marden.readmodels.context import BUSINESS_TZ

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 6
INSTANT_WINDOW = timedelta(hours=24)
DIGEST_HOUR = 7  # 00-global: daily digest 7:00 ET; the weekly summary goes out Mondays at the same hour
# A period's digest is built only this long after its cut, so an alert whose transaction began before the
# cut and committed just after it is not missed (the digest row exists once; it is never rebuilt).
DIGEST_GRACE = timedelta(minutes=2)
KIND_SETTING = {"instant": "instantRed", "daily_digest": "dailyDigest", "weekly_digest": "weeklyNote"}


def backoff(attempts: int) -> timedelta:
    return timedelta(minutes=min(2 ** max(attempts - 1, 0), 360))


def _now(now: datetime | None) -> datetime:
    return now.astimezone(BUSINESS_TZ) if now else datetime.now(BUSINESS_TZ)


def enqueue(conn: Conn, *, kind: str, org_id: str, user_id: str, to_email: str, subject: str,
            alert_ids: list[int], key: str) -> int | None:
    row = conn.execute(
        """INSERT INTO email_outbox (org_id, user_id, kind, idempotency_key, to_email, subject, alert_ids)
           VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING RETURNING id""",
        (org_id, user_id, kind, key, to_email, subject, alert_ids),
    ).fetchone()
    return int(row["id"]) if row else None


def _alerts(conn: Conn, org_id: str, user_id: str, ids: list[int]) -> list[dict[str, Any]]:
    return conn.execute(
        """SELECT * FROM alert WHERE org_id = %s AND user_id = %s AND id = ANY(%s)
           ORDER BY CASE severity WHEN 'red' THEN 0 WHEN 'amber' THEN 1 ELSE 2 END, at, id""",
        (org_id, user_id, ids),
    ).fetchall()


# ------------------------------------------------------------------ instant (red)
def sweep_instant(now: datetime | None = None) -> int:
    """One instant email per user for red alerts not yet in one (users with instantRed on)."""
    moment = _now(now)
    made = 0
    with db.transaction() as conn:
        groups = conn.execute(
            """SELECT a.org_id, a.user_id, array_agg(a.id ORDER BY a.id) AS ids FROM alert a
               WHERE a.severity = 'red' AND NOT a.backfill AND a.emailed_at IS NULL AND a.at > %s
                 AND NOT EXISTS (SELECT 1 FROM email_outbox o WHERE o.org_id = a.org_id AND o.user_id = a.user_id
                                 AND o.kind = 'instant' AND a.id = ANY(o.alert_ids))
               GROUP BY a.org_id, a.user_id ORDER BY a.org_id, a.user_id""",
            (moment - INSTANT_WINDOW,),
        ).fetchall()
        for g in groups:
            member = live_member(conn, g["org_id"], g["user_id"])
            if member is None or not member["settings"].get("instantRed", True):
                continue
            alerts = _alerts(conn, g["org_id"], g["user_id"], g["ids"])
            made += enqueue(conn, kind="instant", org_id=g["org_id"], user_id=g["user_id"], to_email=member["email"],
                            subject=subject_for("instant", alerts, moment), alert_ids=g["ids"],
                            key=f"instant:{g['org_id']}:{g['user_id']}:{g['ids'][0]}") is not None
    return made


# ------------------------------------------------------------------ digests
def digest_cut(now: datetime, *, weekly: bool) -> datetime:
    """The latest 07:00 ET at or before `now` (for the weekly summary, the latest Monday 07:00 ET)."""
    local = now.astimezone(BUSINESS_TZ)
    cut = datetime.combine(local.date(), time(DIGEST_HOUR), BUSINESS_TZ)
    if cut > local:
        cut -= timedelta(days=1)
    if weekly:
        cut -= timedelta(days=cut.weekday())
    return datetime.combine(cut.date(), time(DIGEST_HOUR), BUSINESS_TZ)


def build_digests(now: datetime | None = None) -> dict[str, int]:
    moment = _now(now) - DIGEST_GRACE
    daily_cut, weekly_cut = digest_cut(moment, weekly=False), digest_cut(moment, weekly=True)
    out = {"daily_digest": 0, "weekly_digest": 0}
    with db.transaction() as conn:
        for m in live_members(conn):
            org_id, user_id, settings = m["org_id"], m["user_id"], m["settings"]
            if settings.get("dailyDigest", True):
                ids = [r["id"] for r in conn.execute(
                    """SELECT a.id FROM alert a WHERE a.org_id = %s AND a.user_id = %s AND NOT a.backfill
                         AND a.at >= %s AND a.at < %s AND a.emailed_at IS NULL
                         AND NOT EXISTS (SELECT 1 FROM email_outbox o WHERE o.org_id = a.org_id
                                         AND o.user_id = a.user_id AND o.kind = 'instant'
                                         AND o.status IN ('queued', 'sent') AND a.id = ANY(o.alert_ids))
                       ORDER BY a.id""",
                    (org_id, user_id, daily_cut - timedelta(days=1), daily_cut),
                ).fetchall()]
                out["daily_digest"] += _digest_row(conn, m, "daily_digest", ids, daily_cut)
            if settings.get("weeklyNote", True):
                ids = [r["id"] for r in conn.execute(
                    """SELECT id FROM alert WHERE org_id = %s AND user_id = %s AND NOT backfill
                         AND at >= %s AND at < %s ORDER BY id""",
                    (org_id, user_id, weekly_cut - timedelta(days=7), weekly_cut),
                ).fetchall()]
                out["weekly_digest"] += _digest_row(conn, m, "weekly_digest", ids, weekly_cut)
    return out


def _digest_row(conn: Conn, m: dict[str, Any], kind: str, ids: list[int], cut: datetime) -> int:
    if not ids:
        return 0
    prefix = "daily" if kind == "daily_digest" else "weekly"
    alerts = _alerts(conn, m["org_id"], m["user_id"], ids)
    made = enqueue(conn, kind=kind, org_id=m["org_id"], user_id=m["user_id"], to_email=m["email"],
                   subject=subject_for(kind, alerts, cut), alert_ids=ids,
                   key=f"{prefix}:{m['org_id']}:{m['user_id']}:{cut.date().isoformat()}")
    return int(made is not None)


# ------------------------------------------------------------------ sending
def record_provider_state(conn: Conn, cfg: EmailConfig) -> None:
    conn.execute(
        """INSERT INTO email_provider_state (id, provider, configured, from_email, reason, checked_at)
           VALUES ('alerts', %s, %s, %s, %s, now())
           ON CONFLICT (id) DO UPDATE SET provider = EXCLUDED.provider, configured = EXCLUDED.configured,
             from_email = EXCLUDED.from_email, reason = EXCLUDED.reason, checked_at = now()""",
        (cfg.provider, cfg.configured, cfg.from_email, cfg.reason),
    )


def _finish(conn: Conn, row_id: int, status: str, **cols: Any) -> None:
    sets = "".join(f", {k} = %({k})s" for k in cols)
    conn.execute(
        f"UPDATE email_outbox SET status = %(status)s, updated_at = now(){sets} WHERE id = %(id)s",
        {"status": status, "id": row_id, **cols},
    )


def _risk_week(conn: Conn, row: dict[str, Any]) -> str | None:
    """The Monday of the week a weekly summary covers, when that week's risk list is published."""
    if row["kind"] != "weekly_digest":
        return None
    week = row["idempotency_key"].rsplit(":", 1)[-1]
    found = conn.execute("SELECT 1 FROM read_model WHERE view = 'risk_list' AND key = %s LIMIT 1", (week,)).fetchone()
    return week if found else None


def _send_one(conn: Conn, row: dict[str, Any], cfg: EmailConfig, provider: Provider | None,
              moment: datetime) -> str:
    member = live_member(conn, row["org_id"], row["user_id"])
    if member is None:
        _finish(conn, row["id"], "cancelled", last_error="the member's product access is no longer live")
        return "cancelled"
    if not member["settings"].get(KIND_SETTING[row["kind"]], True):
        _finish(conn, row["id"], "cancelled", last_error=f"{KIND_SETTING[row['kind']]} is switched off")
        return "cancelled"
    if provider is None or not cfg.configured:
        _finish(conn, row["id"], "not_configured", last_error=cfg.reason or "no email provider")
        return "not_configured"
    alerts = _alerts(conn, row["org_id"], row["user_id"], list(row["alert_ids"]))
    if not alerts:
        _finish(conn, row["id"], "cancelled", last_error="its alerts no longer exist")
        return "cancelled"
    email = render(row["kind"], alerts, cfg, org_id=row["org_id"], user_id=row["user_id"], now=moment,
                   risk_list_week=_risk_week(conn, row))
    assert cfg.from_email is not None
    result = provider.send(Message(to=member["email"], sender=cfg.from_email, subject=email.subject, html=email.html,
                                   text=email.text, headers=email.headers, tag=row["kind"].replace("_", "-"),
                                   metadata={"outbox_id": str(row["id"])}), row["idempotency_key"])
    attempts = int(row["attempts"]) + 1
    common = {"attempts": attempts, "provider": provider.name, "to_email": member["email"], "subject": email.subject,
              "html": email.html, "text_body": email.text, "headers": jsonb(email.headers)}
    if result.ok:
        _finish(conn, row["id"], "sent", provider_message_id=result.message_id, sent_at=moment, last_error=None,
                **common)
        marked = conn.execute(
            "UPDATE alert SET emailed_at = %s WHERE id = ANY(%s) AND emailed_at IS NULL RETURNING *",
            (moment, list(row["alert_ids"])),
        ).fetchall()
        for alert in marked:  # the open screens show the alert as emailed (stream-patches alertsPatch)
            events.emit({"type": "alert", "alert": row_alert(alert)}, alert["org_id"], user_id=alert["user_id"],
                        conn=conn)
        return "sent"
    if result.retryable and attempts < MAX_ATTEMPTS:
        _finish(conn, row["id"], "queued", next_attempt_at=moment + backoff(attempts), last_error=result.error,
                **common)
        return "retry"
    _finish(conn, row["id"], "failed", last_error=result.error, **common)
    return "failed"


def send_due(*, limit: int = 200, cfg: EmailConfig | None = None, client: httpx.Client | None = None,
             now: datetime | None = None) -> dict[str, int]:
    """Send every due queued row (one transaction per row, locked with SKIP LOCKED)."""
    cfg = cfg or email_config()
    provider = make_provider(cfg, client)
    moment = _now(now)
    counts: Counter = Counter()
    with db.transaction() as conn:
        record_provider_state(conn, cfg)
    for _ in range(limit):
        picked: int | None = None
        try:
            with db.transaction() as conn:
                row = conn.execute(
                    """SELECT * FROM email_outbox WHERE status = 'queued' AND next_attempt_at <= %s
                       ORDER BY next_attempt_at, id LIMIT 1 FOR UPDATE SKIP LOCKED""",
                    (moment,),
                ).fetchone()
                if row is None:
                    break
                picked = int(row["id"])
                counts[_send_one(conn, row, cfg, provider, moment)] += 1
        except psycopg.OperationalError:
            raise  # the database is gone: no row can be sent or recorded
        except Exception as exc:
            if picked is None:
                raise
            counts[_crashed(picked, exc, moment)] += 1
    if counts:
        log.info("alerts.send: %s", dict(counts))
    return dict(counts)


def _crashed(row_id: int, exc: Exception, moment: datetime) -> str:
    """A row that raised: its transaction was rolled back, so count the attempt and back off (failed after
    MAX_ATTEMPTS). last_error names the error type only; the trace is in the worker log."""
    log.exception("alerts.send: outbox row %s raised %s; retried with backoff", row_id, type(exc).__name__)
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT attempts FROM email_outbox WHERE id = %s AND status = 'queued' FOR UPDATE", (row_id,),
        ).fetchone()
        if row is None:
            return "error"
        attempts = int(row["attempts"]) + 1
        status = "queued" if attempts < MAX_ATTEMPTS else "failed"
        _finish(conn, row_id, status, attempts=attempts, next_attempt_at=moment + backoff(attempts),
                last_error=f"{type(exc).__name__} while rendering or sending (worker log has the details)")
    return "retry" if status == "queued" else "failed"
