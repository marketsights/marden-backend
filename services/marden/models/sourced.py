"""Shared pieces of the mark-gap, recovery and GPU models: Sourced<T> values and one stored run.

    out = run_outputs("recovery", VERSION, day, compute, trigger="manual")

`m_value` makes a Marden model value (tag M, the method version and, for uncalibrated odds and ranges,
the 'model v0' label in the method version, 00-global "Model labels"); `fact_value` passes a fact row
through as the API's Sourced<T> (its own tag, source and as-of). `run_outputs` wraps one model run: it
records `model_run`, writes one `model_output` per subject for the as-of day, and on a live run (today)
marks the read models and models that read the output (`mark_dirty_for(subject_type, id,
'@model:<name>')`) plus any extra views the model names. A past as-of day is a backtest: inputs are read
point in time by the model (marden.models.pit) and nothing is marked.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.core import db
from marden.core.db import Conn, Row
from marden.core.jsonutil import canonical_json, hash_parts, to_jsonable
from marden.models import pit, runs
from marden.readmodels.context import business_today
from marden.readmodels.deps import mark_dirty, mark_dirty_for

UNCALIBRATED = "model v0"  # 00-global: odds and ranges carry it until the backtest calibrates them
METHOD_DOCS = "https://marden.ai/methods/"


def method_version(model: str, version: str) -> str:
    """'recovery', '0.1' -> 'recovery v0.1'."""
    return f"{model} v{version}"


def m_value(value: Any, as_of: date, method: str, *, source_name: str = "Marden model",
            uncalibrated: bool = False) -> dict[str, Any]:
    """Sourced<T> for a value the model computed (null = n/d: an input is missing)."""
    return {
        "value": value,
        "tag": "M",
        "sourceName": source_name,
        "asOf": as_of.isoformat(),
        "methodVersion": f"{method} ({UNCALIBRATED})" if uncalibrated else method,
    }


def fact_value(row: Row | None, value: Any = None, *, as_of: date | None = None, tag: str = "F") -> dict[str, Any]:
    """A fact row as Sourced<T> (optionally with a value derived from it); n/d when there is no row."""
    if row is None:
        return {"value": None, "tag": tag, "asOf": (as_of or business_today()).isoformat()}
    out: dict[str, Any] = {
        "value": row["value"] if value is None else value,
        "tag": str(row["tag"]).strip(),
        "asOf": row["valid_from"].isoformat(),
    }
    if row.get("source_url"):
        out["sourceUrl"] = row["source_url"]
    if row.get("source_name"):
        out["sourceName"] = row["source_name"]
    if row.get("method_version"):
        out["methodVersion"] = row["method_version"]
    return out


def num(value: Any) -> float | None:
    """A JSON / SQL number as float (bools and text are not numbers)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value) if isinstance(value, int | float) or hasattr(value, "as_tuple") else None
    except (TypeError, ValueError):
        return None


def rounded(value: float | None, digits: int) -> float | None:
    return None if value is None else round(float(value), digits)


@dataclass
class Batch:
    """The outputs of one run: subject_type, subject_id -> output, and the views to mark after it."""

    outputs: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    views: set[tuple[str, str]] = field(default_factory=set)
    inputs: list[Any] = field(default_factory=list)

    def add(self, subject_type: str, subject_id: str, output: dict[str, Any]) -> None:
        self.outputs[(subject_type, subject_id)] = output


def _changed(conn: Conn, model: str, subject_type: str, subject_id: str, day: date, output: dict[str, Any]) -> bool:
    prev = runs.latest_output(conn, model, subject_type, subject_id, day)
    return prev is None or canonical_json(prev["output"]) != canonical_json(to_jsonable(output))


def run_outputs(model: str, version: str, as_of: date | None, compute: Callable[[Conn, date], Batch], *,
                trigger: str, mark: bool = True) -> dict[str, Any]:
    """Run `compute(conn, day)` inside one transaction and store its outputs. Returns {run_id, as_of,
    outputs: {subject_id: output}, changed: [subject ids]}."""
    day = as_of or business_today()
    if day > business_today():
        raise ValueError(f"{model} as of {day.isoformat()} is after today")
    live = mark and day == business_today()
    run_id = runs.start_run(model, version, trigger=trigger)
    try:
        with db.transaction() as conn:
            batch = compute(conn, day)
            changed: list[str] = []
            for (subject_type, subject_id), output in sorted(batch.outputs.items()):
                is_new = _changed(conn, model, subject_type, subject_id, day, output)
                runs.write_output(conn, run_id, model, version, subject_type, subject_id, day, output)
                if is_new:
                    changed.append(subject_id)
                if live:
                    mark_dirty_for(subject_type, subject_id, f"@model:{model}", conn=conn,
                                   reason=f"model:{model}@{run_id}")
            if live:
                for view, key in sorted(batch.views):
                    mark_dirty(view, key, conn=conn, reason=f"model:{model}@{run_id}")
        digest = hash_parts(model, version, day, batch.inputs)
    except Exception as exc:
        runs.finish_run(run_id, ok=False, error=f"{type(exc).__name__}: {exc}")
        raise
    runs.finish_run(run_id, ok=True, inputs_hash=digest)
    return {
        "run_id": run_id,
        "as_of": day.isoformat(),
        "outputs": {sid: out for (_, sid), out in sorted(batch.outputs.items())},
        "changed": changed,
    }


def stored_outputs(conn: Conn, model: str, subject_type: str, ids: Iterable[str] | None, day: date) -> dict[str, Row]:
    """The newest output per subject dated on or before `day` (what a run on `day` may read)."""
    rows = conn.execute(
        """SELECT DISTINCT ON (subject_id) subject_id, as_of, version, output FROM model_output
            WHERE model = %s AND subject_type = %s AND (%s::text[] IS NULL OR subject_id = ANY(%s)) AND as_of <= %s
            ORDER BY subject_id, as_of DESC, created_at DESC, id DESC""",
        (model, subject_type, None if ids is None else list(ids), None if ids is None else list(ids), day),
    ).fetchall()
    pit.note_outputs(model, subject_type, rows)
    return {r["subject_id"]: r for r in rows}
