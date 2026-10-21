"""Procrastinate worker: the task registry and periodic schedules.

Only tasks that exist are registered here; collectors, extraction and models register their own
tasks on `marden.core.queue.app` in their modules, which `load_plugins()` imports before the
worker starts.
"""

from __future__ import annotations

import logging
import os

from procrastinate import JobContext, builtin_tasks, exceptions
from procrastinate.jobs import Status

from marden.core.config import ConfigError, get_settings
from marden.core.queue import QUEUES, app, make_connector
from marden.plugins import load_plugins
from marden.readmodels import rebuild_dirty, rebuild_every_view

log = logging.getLogger(__name__)

REBUILD_DIRTY_CRON = "* * * * * */30"  # every 30 s (6th field = seconds)
REBUILD_ALL_CRON = "5 10 * * *"  # daily 10:05 UTC = 05:05 EST / 06:05 EDT (spec 22: screens rebuilt 5-6 am)
PRUNE_JOBS_CRON = "17 8 * * *"  # daily 08:17 UTC
PRUNE_JOBS_AFTER_HOURS = 7 * 24
RETRY_STALLED_CRON = "*/5 * * * *"  # every 5 min
STALLED_AFTER_SECONDS = 120  # a worker that sent no heartbeat for 2 min (it sends one every 10 s) is gone
BACKUP_CRON = "23 7 * * *"  # daily 07:23 UTC = 03:23 EDT / 02:23 EST (spec 22: database backup overnight)
# SIGTERM: stop fetching, give running jobs this long, then abort them (Railway's drainingSeconds must
# be longer, or the platform kills the process first; jobs it kills are retried by queue.retry_stalled).
DEFAULT_SHUTDOWN_TIMEOUT_S = 90.0


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


@app.periodic(cron=RETRY_STALLED_CRON, periodic_id="every_5min")
@app.task(name="queue.retry_stalled", queue="default", queueing_lock="queue.retry_stalled", pass_context=True)
async def retry_stalled_task(context: JobContext, timestamp: int | None = None) -> int:
    """Requeue jobs left `doing` by a worker that died (deploy SIGKILL, crash, lost connection).

    Without this, such a job would hold its `lock` for ever: readmodels.rebuild_dirty, for one, would
    never run again. Procrastinate finds them by worker heartbeat, not by job age, so a long job on a
    live worker is never touched."""
    manager = context.app.job_manager
    stalled = list(await manager.get_stalled_jobs(seconds_since_heartbeat=STALLED_AFTER_SECONDS))
    for job in stalled:
        try:
            await manager.retry_job(job)
            log.warning("retried stalled job %s (%s): its worker stopped", job.id, job.task_name)
        except exceptions.UniqueViolation:
            # The same queueing lock is already queued again (a periodic task's next run): that job does
            # the work; this one is closed as failed so its lock is released.
            await manager.finish_job(job, status=Status.FAILED, delete_job=False)
            log.warning("closed stalled job %s (%s): its worker stopped and a newer run is queued", job.id,
                        job.task_name)
    await manager.prune_stalled_workers(seconds_since_heartbeat=STALLED_AFTER_SECONDS)
    return len(stalled)


@app.periodic(cron=BACKUP_CRON, periodic_id="nightly_backup")
@app.task(name="ops.backup_db", queue="default", queueing_lock="ops.backup_db", lock="ops.backup_db")
def backup_db_task(timestamp: int | None = None) -> str:
    """pg_dump to R2 core-backups (marden.core.backup); recorded as not_configured without R2."""
    from marden.core.backup import run_backup

    return run_backup().status


def shutdown_timeout(env: dict[str, str] | None = None) -> float:
    """MARDEN_WORKER_SHUTDOWN_TIMEOUT (seconds) or the default."""
    raw = (os.environ if env is None else env).get("MARDEN_WORKER_SHUTDOWN_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_SHUTDOWN_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError("MARDEN_WORKER_SHUTDOWN_TIMEOUT must be a number of seconds") from None
    if value <= 0:
        raise ConfigError("MARDEN_WORKER_SHUTDOWN_TIMEOUT must be positive")
    return value


def registered_task_names() -> list[str]:
    """Marden's tasks (Procrastinate's own builtin tasks excluded)."""
    return sorted(name for name in app.tasks if not name.startswith(("builtin:", "procrastinate.")))


def run_worker(*, queues: list[str] | None = None, concurrency: int = 4, wait: bool = True) -> None:
    """Run the worker on the current DATABASE_URL until stopped (or until idle if wait=False)."""
    get_settings().require_database_url()
    timeout = shutdown_timeout()
    load_plugins()
    log.info("worker starting; queues: %s; tasks: %s", ", ".join(queues or ["all"]), ", ".join(registered_task_names()))
    with app.replace_connector(make_connector()):
        app.run_worker(
            queues=queues,
            concurrency=concurrency,
            wait=wait,
            install_signal_handlers=wait,
            shutdown_graceful_timeout=timeout,
        )
    log.info("worker stopped")


__all__ = [
    "QUEUES",
    "app",
    "backup_db_task",
    "prune_jobs_task",
    "rebuild_all_task",
    "rebuild_dirty_task",
    "registered_task_names",
    "retry_stalled_task",
    "run_worker",
    "shutdown_timeout",
]
