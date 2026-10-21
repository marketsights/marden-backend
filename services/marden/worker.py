"""Procrastinate worker: the task registry and periodic schedules.

Only tasks that exist are registered here; collectors, extraction and models register their own
tasks on `marden.core.queue.app` in their modules, which `load_plugins()` imports before the
worker starts.
"""

from __future__ import annotations

import logging

from procrastinate import JobContext, builtin_tasks

from marden.core.config import get_settings
from marden.core.queue import QUEUES, app, make_connector
from marden.plugins import load_plugins
from marden.readmodels import rebuild_dirty, rebuild_every_view

log = logging.getLogger(__name__)

REBUILD_DIRTY_CRON = "* * * * * */30"  # every 30 s (6th field = seconds)
REBUILD_ALL_CRON = "5 10 * * *"  # daily 10:05 UTC = 05:05 EST / 06:05 EDT (spec 22: screens rebuilt 5-6 am)
PRUNE_JOBS_CRON = "17 8 * * *"  # daily 08:17 UTC
PRUNE_JOBS_AFTER_HOURS = 7 * 24


@app.periodic(cron=REBUILD_DIRTY_CRON, periodic_id="every_30s")
@app.task(
    name="readmodels.rebuild_dirty",
    queue="readmodels",
    queueing_lock="readmodels.rebuild_dirty",
    lock="readmodels.rebuild_dirty",
)
def rebuild_dirty_task(timestamp: int | None = None) -> dict[str, int]:
    stats = rebuild_dirty()
    if stats.built or stats.errors:
        log.info("rebuild_dirty: %s", stats.summary())
    return {"built": stats.built, "changed": stats.changed, "events": stats.events, "errors": len(stats.errors)}


@app.periodic(cron=REBUILD_ALL_CRON, periodic_id="nightly")
@app.task(
    name="readmodels.rebuild_all",
    queue="readmodels",
    queueing_lock="readmodels.rebuild_all",
    lock="readmodels.rebuild_dirty",  # never overlaps a dirty rebuild
)
def rebuild_all_task(timestamp: int | None = None) -> dict[str, int]:
    results = rebuild_every_view()
    for view, stats in results.items():
        log.info("rebuild_all %s: %s", view, stats.summary())
    return {view: stats.built for view, stats in results.items()}


@app.periodic(cron=PRUNE_JOBS_CRON, periodic_id="daily")
@app.task(name="queue.prune_jobs", queue="default", queueing_lock="queue.prune_jobs", pass_context=True)
async def prune_jobs_task(context: JobContext, timestamp: int | None = None) -> None:
    """Delete succeeded jobs finished more than 7 days ago (the 30 s rebuild alone adds ~2,900 a day).
    Failed, cancelled and aborted jobs are kept for inspection."""
    await builtin_tasks.remove_old_jobs(context, max_hours=PRUNE_JOBS_AFTER_HOURS)


def registered_task_names() -> list[str]:
    """Marden's tasks (Procrastinate's own builtin tasks excluded)."""
    return sorted(name for name in app.tasks if not name.startswith(("builtin:", "procrastinate.")))


def run_worker(*, queues: list[str] | None = None, concurrency: int = 4, wait: bool = True) -> None:
    """Run the worker on the current DATABASE_URL until stopped (or until idle if wait=False)."""
    get_settings().require_database_url()
    load_plugins()
    log.info("worker starting; tasks: %s", ", ".join(registered_task_names()))
    with app.replace_connector(make_connector()):
        app.run_worker(
            queues=queues,
            concurrency=concurrency,
            wait=wait,
            install_signal_handlers=wait,
        )


__all__ = [
    "QUEUES",
    "app",
    "prune_jobs_task",
    "rebuild_all_task",
    "rebuild_dirty_task",
    "registered_task_names",
    "run_worker",
]
