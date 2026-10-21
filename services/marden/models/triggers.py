"""Model triggers: which published inputs re-run which model, and the task that runs the marked keys.

    model_depends("cushion", "site", "", same)                  # any site fact or event -> that site
    model_depends("cushion", "plant", "expected_cod", plant_sites)
    register_runner("cushion", run_keys)                        # fn(keys, trigger) -> summary dict

When a writer publishes a fact, event or price (`marden.core.facts` / `records`), the read-model
dependency hook (`marden.readmodels.deps.mark_dirty_for`) calls `mark_models_for`, which upserts
`model_dirty(model, key)` in the writer's transaction for every matching rule (field prefix '' matches
every field, including the pseudo-fields '@event' and '@price'). `models.run_dirty` (queue 'models',
every minute) runs each model's runner on its marked keys as of today and deletes the marks it consumed;
a mark made while the run was going stays for the next pass. Spec 22: events trigger work immediately,
the clock (each model's own schedule) is the backstop.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from marden.core import db
from marden.core.db import Conn
from marden.core.queue import app

log = logging.getLogger(__name__)

KeyFn = Callable[[Conn, str], Iterable[str]]
Runner = Callable[[list[str], str], dict[str, Any]]
RUN_DIRTY_CRON = "* * * * *"
RUN_DIRTY_LIMIT = 500


def same(conn: Conn, subject_id: str) -> list[str]:
    return [subject_id]


def everything(conn: Conn, subject_id: str) -> list[str]:
    """Key '*': the input feeds every subject of the model (e.g. a slip-history series)."""
    return ["*"]


@dataclass(frozen=True)
class ModelDep:
    model: str
    subject_type: str
    field_prefix: str
    keys: KeyFn


_DEPS: list[ModelDep] = []
_RUNNERS: dict[str, Runner] = {}


def model_depends(model: str, subject_type: str, field_prefix: str, keys: KeyFn = same) -> None:
    dep = ModelDep(model, subject_type, field_prefix, keys)
    if dep not in _DEPS:
        _DEPS.append(dep)


def model_dependencies(model: str | None = None) -> list[ModelDep]:
    return [d for d in _DEPS if model is None or d.model == model]


def register_runner(model: str, runner: Runner) -> None:
    existing = _RUNNERS.get(model)
    if existing is not None and existing is not runner:
        raise ValueError(f"model {model!r} already has a runner")
    _RUNNERS[model] = runner


def registered_models() -> list[str]:
    return sorted(_RUNNERS)


def _upsert(conn: Conn, pairs: Iterable[tuple[str, str]], reason: str | None) -> None:
    ordered = sorted(set(pairs))  # one lock order across writers avoids deadlocks
    if not ordered:
        return
    conn.execute(
        """INSERT INTO model_dirty (model, key, marked_at, reason)
           SELECT m, k, clock_timestamp(), %s FROM unnest(%s::text[], %s::text[]) AS t(m, k)
           ON CONFLICT (model, key) DO UPDATE SET marked_at = EXCLUDED.marked_at, reason = EXCLUDED.reason""",
        (reason, [p[0] for p in ordered], [p[1] for p in ordered]),
    )


def mark_model(model: str, key: str, *, conn: Conn | None = None, reason: str | None = None) -> None:
    with db.transaction(conn) as c:
        _upsert(c, [(model, key)], reason)


def model_targets(conn: Conn, subject_type: str, subject_id: str, field: str) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for dep in _DEPS:
        if dep.subject_type == subject_type and field.startswith(dep.field_prefix):
            out.update((dep.model, k) for k in dep.keys(conn, subject_id) if k is not None)
    return out


def mark_models_for(conn: Conn, subject_type: str, subject_id: str, field: str, reason: str | None = None) -> None:
    """The writers' hook (via marden.readmodels.deps): mark every model whose inputs include this value."""
    if not _DEPS:
        return
    _upsert(conn, model_targets(conn, subject_type, subject_id, field), reason)


def dirty_marks(conn: Conn, model: str, limit: int = RUN_DIRTY_LIMIT) -> list[tuple[str, datetime]]:
    rows = conn.execute(
        "SELECT key, marked_at FROM model_dirty WHERE model = %s ORDER BY marked_at LIMIT %s", (model, limit)
    ).fetchall()
    return [(r["key"], r["marked_at"]) for r in rows]


def clear_marks(conn: Conn, model: str, marks: list[tuple[str, datetime]]) -> None:
    """Delete the consumed marks; a key re-marked after it was read keeps its newer mark."""
    if not marks:
        return
    conn.execute(
        """DELETE FROM model_dirty d USING unnest(%s::text[], %s::timestamptz[]) AS t(k, m)
           WHERE d.model = %s AND d.key = t.k AND d.marked_at <= t.m""",
        ([k for k, _ in marks], [m for _, m in marks], model),
    )


def run_dirty(models: list[str] | None = None, limit: int = RUN_DIRTY_LIMIT) -> dict[str, Any]:
    """Run every registered model on its marked keys. A failing model keeps its marks (retried next pass)."""
    summary: dict[str, Any] = {}
    for model in models or registered_models():
        with db.transaction() as conn:
            marks = dirty_marks(conn, model, limit)
        if not marks:
            continue
        keys = sorted({k for k, _ in marks})
        try:
            summary[model] = _RUNNERS[model](keys, f"dirty:{len(keys)}")
        except Exception as exc:  # recorded on the model's own run row; marks stay
            log.exception("model %s failed on dirty keys", model)
            summary[model] = {"error": str(exc)}
            continue
        with db.transaction() as conn:
            clear_marks(conn, model, marks)
    return summary


@app.periodic(cron=RUN_DIRTY_CRON, periodic_id="models_dirty")
@app.task(name="models.run_dirty", queue="models", queueing_lock="models.run_dirty", lock="models.run_dirty")
def run_dirty_task(timestamp: int | None = None) -> dict[str, Any]:
    out = run_dirty()
    if out:
        log.info("models.run_dirty: %s", out)
    return out
