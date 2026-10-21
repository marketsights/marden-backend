"""The worker: registered tasks and a real job run through Procrastinate on the test schema."""

from __future__ import annotations

from marden import worker
from marden.core.queue import app
from marden.readmodels import builder, mark_dirty
from marden.readmodels.registry import unregister


def test_only_existing_tasks_are_registered():
    assert worker.registered_task_names() == [
        "queue.prune_jobs", "readmodels.rebuild_all", "readmodels.rebuild_dirty"
    ]
    periodic = {name: task.cron for (name, _), task in app.periodic_registry.periodic_tasks.items()}
    assert periodic == {
        "readmodels.rebuild_dirty": worker.REBUILD_DIRTY_CRON,
        "readmodels.rebuild_all": worker.REBUILD_ALL_CRON,
        "queue.prune_jobs": worker.PRUNE_JOBS_CRON,
    }


def test_worker_runs_a_deferred_rebuild(committed):
    view = "test_worker_view"

    @builder(view, many=True)
    def build(conn):
        yield "all", {"built_by": "worker"}

    try:
        mark_dirty(view, "all")
        with app.open():
            job_id = worker.rebuild_dirty_task.defer(timestamp=0)
        worker.run_worker(queues=["readmodels"], concurrency=1, wait=False)
        job = committed.execute("SELECT status FROM procrastinate_jobs WHERE id = %s", (job_id,)).fetchone()
        assert job["status"] == "succeeded"
        doc = committed.execute("SELECT doc FROM read_model WHERE view = %s", (view,)).fetchone()
        assert doc["doc"] == {"built_by": "worker"}
    finally:
        unregister(view)
