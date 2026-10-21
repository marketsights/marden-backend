"""Worker tasks for the agents (queue 'agents'): continuous intake, processing, batches and the R2 inbox.

    agents.sweep        every 30 s   store uploads, apply staff decisions, queue documents (not configured:
                                     queues nothing and says so in agent_status)
    agents.work         every minute run queued jobs, priority first, per-source caps, shared token budget
    agents.batch_poll   every 5 min  apply ended Message Batches and submit the next stage of backfills
    agents.r2_inbox     every 10 min ingest new objects under inbox/ in the raw bucket (R2 only)
"""

from __future__ import annotations

import logging
from typing import Any

from marden.core.queue import app

log = logging.getLogger(__name__)

SWEEP_CRON = "* * * * * */30"
WORK_CRON = "* * * * *"
BATCH_CRON = "*/5 * * * *"
INBOX_CRON = "*/10 * * * *"


@app.periodic(cron=SWEEP_CRON, periodic_id="agents_sweep")
@app.task(name="agents.sweep", queue="agents", queueing_lock="agents.sweep", lock="agents.sweep")
def sweep_task(timestamp: int | None = None) -> dict[str, Any]:
    from marden.agents.runner import sweep

    out = sweep()
    if out.get("queued") or out.get("uploads_stored") or out.get("decisions"):
        log.info("agents sweep: %s", out)
    return out


@app.periodic(cron=WORK_CRON, periodic_id="agents_work")
@app.task(name="agents.work", queue="agents", queueing_lock="agents.work", lock="agents.work")
def work_task(timestamp: int | None = None) -> dict[str, Any]:
    from marden.agents.runner import work

    out = work()
    jobs = out.get("jobs") or []
    if jobs:
        log.info("agents work: %d jobs, %d tokens", len(jobs), out.get("tokens", 0))
    return {"status": out["status"], "jobs": len(jobs), "tokens": out.get("tokens", 0)}


@app.periodic(cron=BATCH_CRON, periodic_id="agents_batch")
@app.task(name="agents.batch_poll", queue="agents", queueing_lock="agents.batch_poll", lock="agents.batch_poll")
def batch_poll_task(timestamp: int | None = None) -> dict[str, Any]:
    from marden.agents.batch import poll

    return poll()


@app.periodic(cron=INBOX_CRON, periodic_id="agents_inbox")
@app.task(name="agents.r2_inbox", queue="agents", queueing_lock="agents.r2_inbox", lock="agents.r2_inbox")
def r2_inbox_task(timestamp: int | None = None) -> dict[str, Any]:
    from marden.agents.intake import r2_inbox

    return r2_inbox()
