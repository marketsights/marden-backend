"""Worker tasks for the operations agents (queue 'agents'; spec 22 "What runs when").

    agents.watchdog  daily 10:41 UTC (06:41 ET, after the 5-6 am model runs and the 10:05 UTC screen rebuild,
                     before the 7:00 ET digest): consistency checks -> review items (kind 'issue')
    agents.research  Saturdays 11:13 UTC: the week's searches for fields still not disclosed
    agents.note      Mondays 04:07 UTC (Sunday night ET, after the 03:40 UTC risk ranking): the weekly note
                     draft, waiting for a person's approval before Monday 6 am
"""

from __future__ import annotations

import logging
from typing import Any

from marden.core.queue import app

log = logging.getLogger(__name__)

WATCHDOG_CRON = "41 10 * * *"
RESEARCH_CRON = "13 11 * * 6"
NOTE_CRON = "7 4 * * 1"


@app.periodic(cron=WATCHDOG_CRON, periodic_id="agents_watchdog_daily")
@app.task(name="agents.watchdog", queue="agents", queueing_lock="agents.watchdog", lock="agents.watchdog")
def watchdog_task(timestamp: int | None = None) -> dict[str, Any]:
    from marden.agents.ops.watchdog import run

    report = run(trigger="schedule")
    log.info("agents watchdog: %s", report.get("issues"))
    return {"run_id": report["run_id"], "issues": report.get("issues")}


@app.periodic(cron=RESEARCH_CRON, periodic_id="agents_research_weekly")
@app.task(name="agents.research", queue="agents", queueing_lock="agents.research", lock="agents.research")
def research_task(timestamp: int | None = None) -> dict[str, Any]:
    from marden.agents.ops.researcher import run

    report = run(trigger="schedule")
    log.info("agents research: %s searches, finds %s", report["edgar"]["run"] + report["ferc"]["run"],
             report.get("finds"))
    return {"run_id": report["run_id"], "finds": report.get("finds"), "queued": len(report.get("queued") or [])}


@app.periodic(cron=NOTE_CRON, periodic_id="agents_note_weekly")
@app.task(name="agents.note", queue="agents", queueing_lock="agents.note", lock="agents.note")
def note_task(timestamp: int | None = None) -> dict[str, Any]:
    from marden.agents.ops.note import draft

    report = draft(trigger="schedule")
    log.info("agents note %s: %s", report.get("week"), report.get("result"))
    return {"run_id": report["run_id"], "week": report.get("week"), "result": report.get("result")}
