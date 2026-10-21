"""Worker tasks (queue 'alerts') and the `python -m marden alerts ...` commands.

    alerts.scan     every minute: evaluate new triggers -> alerts + stream events, queue instant red emails,
                    send what is due (so an approved event or a model run reaches a watcher within ~1 minute)
    alerts.digests  every 15 minutes (07:03, 07:18, ... ET): build the daily (07:00 ET) and weekly (Monday)
                    digests once each, send

    python -m marden alerts scan [--since-days 30] [--dry-run]   # backfill: alerts only, never emailed
    python -m marden alerts send | digest | status
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from typing import Any

from marden import cli
from marden.alerts import engine, outbox
from marden.alerts.settings import email_config
from marden.core import db
from marden.core.queue import app
from marden.readmodels.context import business_today

SCAN_CRON = "* * * * *"
DIGEST_CRON = "3,18,33,48 * * * *"  # just past each 07:00 ET cut + outbox.DIGEST_GRACE

cli.group("alerts", help="Alert engine and email delivery")


@app.periodic(cron=SCAN_CRON, periodic_id="alerts_scan")
@app.task(name="alerts.scan", queue="alerts", queueing_lock="alerts.scan", lock="alerts.scan")
def scan_task(timestamp: int | None = None) -> dict[str, Any]:
    stats = engine.scan()
    instant = outbox.sweep_instant()
    sent = outbox.send_due()
    return {"alerts": stats.alerts, "coalesced": stats.coalesced, "instantQueued": instant, "send": sent}


@app.periodic(cron=DIGEST_CRON, periodic_id="alerts_digests")
@app.task(name="alerts.digests", queue="alerts", queueing_lock="alerts.digests", lock="alerts.digests")
def digests_task(timestamp: int | None = None) -> dict[str, Any]:
    built = outbox.build_digests()
    return {"built": built, "send": outbox.send_due()}


def _scan_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--since-days", type=int, default=None, help=f"window in days (default {engine.LIVE_DAYS})")
    p.add_argument("--today", type=date.fromisoformat, default=None, help="end of the window (YYYY-MM-DD)")
    p.add_argument("--dry-run", action="store_true", help="write alerts only: no stream events, never emailed")


@cli.action("alerts", "scan", help="Evaluate events, ratings and model outputs; write alerts", configure=_scan_args)
def scan_command(args: argparse.Namespace) -> int:
    today = args.today or business_today()
    since = today - timedelta(days=args.since_days) if args.since_days is not None else None
    stats = engine.scan(since=since, today=today, dry_run=args.dry_run)
    out = stats.summary()
    if not args.dry_run:
        out["instantQueued"] = outbox.sweep_instant()
    print(json.dumps(out, indent=2))
    return 0


@cli.action("alerts", "send", help="Send due emails from the outbox")
def send_command(args: argparse.Namespace) -> int:
    print(json.dumps(outbox.send_due(), indent=2))
    return 0


def _digest_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--now", type=datetime.fromisoformat, default=None, help="build as of this time (ISO)")


@cli.action("alerts", "digest", help="Build the daily and weekly digests that are due", configure=_digest_args)
def digest_command(args: argparse.Namespace) -> int:
    print(json.dumps(outbox.build_digests(args.now), indent=2))
    return 0


@cli.action("alerts", "status", help="Email configuration (variable names only) and outbox counts")
def status_command(args: argparse.Namespace) -> int:
    cfg = email_config()
    with db.connection() as conn:
        counts = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, count(*)::int AS n FROM email_outbox GROUP BY status ORDER BY status").fetchall()}
        alerts = conn.execute("SELECT count(*)::int AS n, count(*) FILTER (WHERE backfill)::int AS backfill FROM alert"
                              ).fetchone()
    print(json.dumps({"provider": cfg.provider, "configured": cfg.configured, "reason": cfg.reason,
                      "fromEmail": cfg.from_email, "outbox": counts, "alerts": alerts}, indent=2))
    return 0
