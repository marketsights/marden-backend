"""The alert engine: evaluate triggers, fan findings out to watching users, write alerts and live events.

    stats = scan()                                   # live: the last LIVE_DAYS, alerts + stream events
    stats = scan(since=today - 30, dry_run=True)     # backfill: alerts only (no stream, never emailed)

Each trigger is one transaction: its alert_trigger row (the rerun guard), its alerts (unique per org, user
and dedupe key), the 'alert' stream event per new alert (scoped to that user) and, for a model change, the
engine's alert_state. A material event that repeats a matter already alerted to the user (same subject and
rule within 7 days: five articles on one force majeure notice) is added to that alert's sources instead of
becoming a new alert. Instant emails are queued afterwards from the red alerts (marden.alerts.outbox).
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from marden.alerts import triggers as tr
from marden.alerts.fanout import Recipient, recipients
from marden.alerts.rules import Finding
from marden.core import db, events
from marden.core.db import Conn
from marden.core.jsonutil import jsonb
from marden.readmodels.context import BUSINESS_TZ, business_today

log = logging.getLogger(__name__)

LIVE_DAYS = 3  # a live scan looks back this far for triggers it has not evaluated
MATTER_DAYS = 7


@dataclass
class ScanStats:
    dry_run: bool
    start: date
    today: date
    triggers: int = 0
    material: int = 0
    alerts: int = 0
    coalesced: int = 0
    stream_events: int = 0
    findings_by_rule: Counter = field(default_factory=Counter)
    alerts_by_rule: Counter = field(default_factory=Counter)
    triggers_by_kind: Counter = field(default_factory=Counter)

    def summary(self) -> dict[str, Any]:
        return {
            "dryRun": self.dry_run, "window": [self.start.isoformat(), self.today.isoformat()],
            "triggers": self.triggers, "material": self.material, "alerts": self.alerts,
            "coalesced": self.coalesced, "streamEvents": self.stream_events,
            "triggersByKind": dict(sorted(self.triggers_by_kind.items())),
            "findingsByRule": dict(sorted(self.findings_by_rule.items())),
            "alertsByRule": dict(sorted(self.alerts_by_rule.items())),
        }


def scan(*, since: date | None = None, today: date | None = None, dry_run: bool = False) -> ScanStats:
    today = today or business_today()
    start = since or today - timedelta(days=LIVE_DAYS)
    stats = ScanStats(dry_run=dry_run, start=start, today=today)
    cache: dict[tuple[str, str], list[Recipient]] = {}
    for load in (tr.event_triggers, tr.fact_triggers, tr.model_triggers):
        with db.connection() as conn:
            pending = list(load(conn, start, today))
        for trigger in pending:
            _process(trigger, stats, cache, dry_run)
    if stats.alerts or stats.coalesced:
        log.info("alerts.scan: %s", stats.summary())
    return stats


def _alert_at(trigger: tr.Trigger, dry_run: bool) -> datetime | None:
    """Live alerts are timed when they fire (now()); backfilled ones on the day the change happened."""
    return datetime.combine(trigger.occurred_on, time(0), BUSINESS_TZ) if dry_run else None


def _process(trigger: tr.Trigger, stats: ScanStats, cache: dict[tuple[str, str], list[Recipient]],
             dry_run: bool) -> None:
    with db.transaction() as conn:
        if trigger.findings or trigger.kind != "model":
            row = conn.execute(
                """INSERT INTO alert_trigger (trigger_key, kind, subject_type, subject_id, occurred_on, material,
                                              codes, dry_run)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (trigger_key) DO NOTHING RETURNING trigger_key""",
                (trigger.key, trigger.kind, trigger.subject_type, trigger.subject_id, trigger.occurred_on,
                 trigger.material, trigger.codes, dry_run),
            ).fetchone()
            if row is None:  # evaluated by an earlier scan: keep the engine's state moving, alert nobody
                _save_state(conn, trigger)
                return
        stats.triggers += 1
        stats.triggers_by_kind[trigger.kind] += 1
        if trigger.findings:
            stats.material += 1
        made = 0
        for finding in trigger.findings:
            stats.findings_by_rule[finding.code] += 1
            key = (trigger.subject_type, trigger.subject_id)
            if key not in cache:
                cache[key] = recipients(conn, *key)
            for person in cache[key]:
                if not person.wants(finding.alert_types, finding.threshold_pts):
                    continue
                made += _deliver(conn, trigger, finding, person, stats, dry_run)
        if made:
            conn.execute("UPDATE alert_trigger SET alerts = %s WHERE trigger_key = %s", (made, trigger.key))
        _save_state(conn, trigger)


def _save_state(conn: Conn, trigger: tr.Trigger) -> None:
    if trigger.state is None:
        return
    s = trigger.state
    conn.execute(
        """INSERT INTO alert_state (model, subject_type, subject_id, as_of, output_hash, state)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (model, subject_type, subject_id) DO UPDATE
           SET as_of = EXCLUDED.as_of, output_hash = EXCLUDED.output_hash, state = EXCLUDED.state,
               evaluated_at = now()""",
        (s.model, s.subject_type, s.subject_id, s.as_of, s.output_hash, jsonb(s.state)),
    )


def _source_entry(trigger: tr.Trigger, finding: Finding) -> list[dict[str, Any]]:
    return [{**s, "key": s.get("key") or trigger.key} for s in finding.sources] or [{"key": trigger.key}]


def _coalesce(conn: Conn, trigger: tr.Trigger, finding: Finding, person: Recipient) -> bool:
    """Add a repeat of an alerted matter to the earlier alert's sources. True when it joined one."""
    if not trigger.coalesce:
        return False
    found = conn.execute(
        """SELECT id FROM alert WHERE org_id = %s AND user_id = %s AND subject_type = %s AND subject_id = %s
             AND rule_code = %s AND occurred_on BETWEEN %s AND %s AND trigger_key <> %s
           ORDER BY occurred_on DESC, id DESC LIMIT 1""",
        (person.org_id, person.user_id, trigger.subject_type, trigger.subject_id, finding.code,
         trigger.occurred_on - timedelta(days=MATTER_DAYS), trigger.occurred_on + timedelta(days=MATTER_DAYS),
         trigger.key),
    ).fetchone()
    if found is None:
        return False
    added = _source_entry(trigger, finding)
    conn.execute(
        """UPDATE alert SET sources = sources || %s WHERE id = %s AND NOT sources @> %s""",
        (jsonb(added), found["id"], jsonb([{"key": added[0]["key"]}])),
    )
    return True


def _body(finding: Finding, person: Recipient) -> str:
    if not person.via:
        return finding.body
    return f"{finding.body}\nOn your watchlist: {', '.join(person.via)}."


def _deliver(conn: Conn, trigger: tr.Trigger, finding: Finding, person: Recipient, stats: ScanStats,
             dry_run: bool) -> int:
    if _coalesce(conn, trigger, finding, person):
        stats.coalesced += 1
        return 0
    kind = next((k for k in finding.alert_types if (person.settings.get("types") or {}).get(k, True)),
                finding.alert_types[0])
    dedupe = f"{trigger.key}:{finding.part}" if finding.part else trigger.key
    row = conn.execute(
        """INSERT INTO alert (org_id, user_id, at, kind, headline, body, tag, rule_label, rule_code, ref_url,
                              subject_type, subject_id, event_id, severity, facts, sources, occurred_on,
                              trigger_key, dedupe_key, backfill)
           VALUES (%s, %s, coalesce(%s, now()), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (org_id, user_id, dedupe_key) DO NOTHING RETURNING id, at""",
        (person.org_id, person.user_id, _alert_at(trigger, dry_run), kind, finding.headline, _body(finding, person),
         finding.tag, finding.rule_label, finding.code, finding.ref_url, trigger.subject_type, trigger.subject_id,
         trigger.event_id, finding.severity, jsonb(finding.facts), jsonb(_source_entry(trigger, finding)),
         trigger.occurred_on, trigger.key, dedupe, dry_run),
    ).fetchone()
    if row is None:
        return 0
    stats.alerts += 1
    stats.alerts_by_rule[finding.code] += 1
    if not dry_run:
        events.emit({"type": "alert", "alert": stream_alert(row["id"], row["at"], finding)},
                    person.org_id, user_id=person.user_id, conn=conn)
        stats.stream_events += 1
    return 1


def stream_alert(alert_id: int, at: datetime, finding: Finding) -> dict[str, Any]:
    """The Alert (handoff/api/types.ts) a new alert row is served as."""
    return {"id": str(alert_id), "at": at.isoformat(), "headline": finding.headline, "tag": finding.tag,
            "ruleLabel": finding.rule_label, "refUrl": finding.ref_url, "read": False, "emailed": False}
