"""The worker: registered tasks and a real job run through Procrastinate on the test schema."""

from __future__ import annotations

from marden import worker
from marden.core.queue import app
from marden.plugins import load_plugins
from marden.readmodels import builder, mark_dirty


def _core(name: str) -> bool:
    """Tasks worker.py registers itself. Collector tasks ('collect.*'), model tasks ('models.*'), alert tasks
    ('alerts.*') and agent tasks ('agents.*') register in their own modules (imported by load_plugins) and are
    checked by their own tests."""
    return not name.startswith(("collect.", "models.", "alerts.", "agents."))


def test_only_existing_tasks_are_registered():
    assert [n for n in worker.registered_task_names() if _core(n)] == [
        "ops.backup_db", "queue.prune_jobs", "queue.retry_stalled", "readmodels.rebuild_all", "readmodels.rebuild_dirty"
    ]
    for name in worker.registered_task_names():
        if not _core(name):
            expected = {"models": "models", "alerts": "alerts", "agents": "agents"}.get(name.split(".")[0], "collect")
            if name == "models.stress_custom":  # the Stress screen waits on it: the worker's reserved lane
                expected = worker.STRESS_QUEUE
            assert app.tasks[name].queue == expected, name
    periodic = {name: task.cron for (name, _), task in app.periodic_registry.periodic_tasks.items() if _core(name)}
    assert periodic == {
        "readmodels.rebuild_dirty": worker.REBUILD_DIRTY_CRON,
        "readmodels.rebuild_all": worker.REBUILD_ALL_CRON,
        "queue.prune_jobs": worker.PRUNE_JOBS_CRON,
        "queue.retry_stalled": worker.RETRY_STALLED_CRON,
        "ops.backup_db": worker.BACKUP_CRON,
    }


def test_worker_runs_a_deferred_rebuild(committed, take_view):
    load_plugins()  # as run_worker does, so the real track_record builder is registered before we take the view
    view = take_view("track_record")

    @builder(view, many=True)
    def build(conn):
        yield "all", {"built_by": "worker"}

    mark_dirty(view, "all")
    with app.open():
        job_id = worker.rebuild_dirty_task.defer(timestamp=0)
    worker.run_worker(queues=["readmodels"], concurrency=1, wait=False)
    job = committed.execute("SELECT status FROM procrastinate_jobs WHERE id = %s", (job_id,)).fetchone()
    assert job["status"] == "succeeded"
    doc = committed.execute("SELECT doc FROM read_model WHERE view = %s", (view,)).fetchone()
    assert doc["doc"] == {"built_by": "worker"}
