"""Source heartbeats: every collector run is recorded in `source_run` and on its `source` row.

    with heartbeat.run("sec_edgar") as hb:
        for item in items:
            hb.seen()
            if write(item).was_new:
                hb.new()

Heartbeat rows are written on their own pooled connections and committed immediately, so a
failed run is recorded even when the run's own transaction rolls back. Exceptions are recorded
and re-raised. A blocked source is recorded as a failed run and raises SourceBlocked.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from marden.core import db, events
from marden.core.jsonutil import jsonb
from marden.core.sources import SourceBlocked, UnknownSource

MAX_ERROR_CHARS = 4000


@dataclass
class RunHandle:
    run_id: int
    source_id: str
    items_seen: int = 0
    items_new: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    def seen(self, n: int = 1) -> None:
        self.items_seen += n

    def new(self, n: int = 1) -> None:
        self.items_new += n


def _error_text(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text[:MAX_ERROR_CHARS]


def _healthy(source: dict[str, Any]) -> bool:
    ok, err = source["last_ok_at"], source["last_error_at"]
    return err is None or (ok is not None and ok >= err)


def _pipeline_event(source_id: str, status: str) -> dict[str, Any]:
    return {"type": "pipeline", "source": source_id, "status": status, "at": datetime.now(UTC).isoformat()}


def _start(source_id: str) -> tuple[int, dict[str, Any]]:
    with db.transaction() as conn:
        source = conn.execute("SELECT * FROM source WHERE id = %s", (source_id,)).fetchone()
        if source is None:
            raise UnknownSource(f"source {source_id!r} has no row; call ensure_source first")
        row = conn.execute(
            "INSERT INTO source_run (source_id) VALUES (%s) RETURNING id", (source_id,)
        ).fetchone()
    assert row is not None
    return row["id"], source


def _finish_ok(handle: RunHandle, was_healthy: bool) -> None:
    with db.transaction() as conn:
        conn.execute(
            """UPDATE source_run SET finished_at = now(), ok = true, items_seen = %s, items_new = %s,
                      meta = %s WHERE id = %s""",
            (handle.items_seen, handle.items_new, jsonb(handle.meta), handle.run_id),
        )
        conn.execute("UPDATE source SET last_ok_at = now() WHERE id = %s", (handle.source_id,))
        if not was_healthy:
            events.emit(_pipeline_event(handle.source_id, "ok"), conn=conn)


def _finish_error(handle: RunHandle, error: str, was_healthy: bool) -> None:
    with db.transaction() as conn:
        conn.execute(
            """UPDATE source_run SET finished_at = now(), ok = false, items_seen = %s, items_new = %s,
                      error = %s, meta = %s WHERE id = %s""",
            (handle.items_seen, handle.items_new, error, jsonb(handle.meta), handle.run_id),
        )
        conn.execute(
            "UPDATE source SET last_error_at = now(), last_error = %s WHERE id = %s",
            (error, handle.source_id),
        )
        if was_healthy:
            events.emit(_pipeline_event(handle.source_id, "down"), conn=conn)


@contextmanager
def run(source_id: str) -> Iterator[RunHandle]:
    """Record one collection run of a source (see module docstring)."""
    run_id, source = _start(source_id)
    handle = RunHandle(run_id=run_id, source_id=source_id)
    was_healthy = _healthy(source)
    if source["blocked"]:
        exc = SourceBlocked(f"source {source_id!r} is blocked: {source['blocked_reason']}")
        _finish_error(handle, _error_text(exc), was_healthy)
        raise exc
    try:
        yield handle
    except BaseException as exc:
        _finish_error(handle, _error_text(exc), was_healthy)
        raise
    _finish_ok(handle, was_healthy)


def latest_runs(source_id: str, limit: int = 10) -> list[dict[str, Any]]:
    with db.transaction() as conn:
        return conn.execute(
            "SELECT * FROM source_run WHERE source_id = %s ORDER BY started_at DESC, id DESC LIMIT %s",
            (source_id, limit),
        ).fetchall()
