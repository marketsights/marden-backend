"""`model_run` and `model_output` rows: one run per invocation, one output per (model, version, subject, as_of).

    run_id = start_run("cushion", "cushion v0.1", trigger="schedule")
    write_output(conn, run_id, "cushion", "cushion v0.1", "site", "jupiter", as_of, {...})
    finish_run(run_id, ok=True, inputs_hash=...)

The run row is committed when it starts, so a run that crashes is still recorded (ok = false with the
error). An output for a day that already has one is replaced (the history is one output per as_of day,
like read-model snapshots); outputs of other days are never touched.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from marden.core import db
from marden.core.db import Conn, Row
from marden.core.jsonutil import jsonb, to_jsonable


def start_run(model: str, version: str, *, trigger: str, conn: Conn | None = None) -> int:
    with db.transaction(conn) as c:
        row = c.execute(
            "INSERT INTO model_run (model, version, trigger) VALUES (%s, %s, %s) RETURNING id",
            (model, version, trigger),
        ).fetchone()
    assert row is not None
    return int(row["id"])


def finish_run(
    run_id: int, *, ok: bool, error: str | None = None, inputs_hash: str | None = None, conn: Conn | None = None
) -> None:
    with db.transaction(conn) as c:
        c.execute(
            "UPDATE model_run SET finished_at = now(), ok = %s, error = %s, inputs_hash = %s WHERE id = %s",
            (ok, error, inputs_hash, run_id),
        )


def write_output(
    conn: Conn,
    run_id: int,
    model: str,
    version: str,
    subject_type: str,
    subject_id: str,
    as_of: date,
    output: dict[str, Any],
) -> int:
    """Upsert the output of one subject for one as_of day. Returns the model_output id."""
    row = conn.execute(
        """INSERT INTO model_output (run_id, model, version, subject_type, subject_id, as_of, output)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (model, version, subject_type, subject_id, as_of)
           DO UPDATE SET run_id = EXCLUDED.run_id, output = EXCLUDED.output, created_at = now()
           RETURNING id""",
        (run_id, model, version, subject_type, subject_id, as_of, jsonb(to_jsonable(output))),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def output_on(conn: Conn, model: str, subject_type: str, subject_id: str, day: date) -> Row | None:
    """The output stored for exactly that day (any version; the newest written wins)."""
    return conn.execute(
        """SELECT * FROM model_output WHERE model = %s AND subject_type = %s AND subject_id = %s AND as_of = %s
           ORDER BY created_at DESC, id DESC LIMIT 1""",
        (model, subject_type, subject_id, day),
    ).fetchone()


def latest_output(conn: Conn, model: str, subject_type: str, subject_id: str, on_or_before: date) -> Row | None:
    """The newest output dated on or before `on_or_before` (what a snapshot of that day shows)."""
    return conn.execute(
        """SELECT * FROM model_output WHERE model = %s AND subject_type = %s AND subject_id = %s AND as_of <= %s
           ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1""",
        (model, subject_type, subject_id, on_or_before),
    ).fetchone()


def outputs_between(
    conn: Conn, model: str, subject_type: str, subject_id: str, start: date, end: date
) -> list[Row]:
    """One output per as_of day in [start, end], oldest first (the newest written per day)."""
    return conn.execute(
        """SELECT DISTINCT ON (as_of) * FROM model_output
           WHERE model = %s AND subject_type = %s AND subject_id = %s AND as_of BETWEEN %s AND %s
           ORDER BY as_of, created_at DESC, id DESC""",
        (model, subject_type, subject_id, start, end),
    ).fetchall()
