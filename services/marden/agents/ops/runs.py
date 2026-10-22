"""One `agent_ops_run` row per researcher, watchdog or note-writer run: started, then finished with its report.

    run_id = start("watchdog", trigger="schedule")
    finish(run_id, "ok", report={...}, model_status="api")

The row is committed when the run starts, so a run that crashes is still recorded (status 'failed').
`model_status` says honestly whether the run's model calls were made: 'api' | 'cli' | 'not_configured' |
'not_used' (the run needs none). Reports hold ids, counts, queries and code-written text, never a secret.
"""

from __future__ import annotations

from typing import Any

from marden.agents.backends import make_backend
from marden.agents.config import AgentSettings, BackendStatus, detect_backend
from marden.agents.llm import Llm, RunBudget
from marden.core import db
from marden.core.jsonutil import jsonb

ROLES = frozenset({"researcher", "watchdog", "note_writer"})
STATUSES = frozenset({"ok", "partial", "failed"})


def start(role: str, *, trigger: str = "manual") -> int:
    if role not in ROLES:
        raise ValueError(f"unknown ops role {role!r}")
    with db.transaction() as conn:
        row = conn.execute("INSERT INTO agent_ops_run (role, trigger) VALUES (%s, %s) RETURNING id",
                           (role, trigger)).fetchone()
    assert row is not None
    return int(row["id"])


def finish(run_id: int, status: str, *, report: dict[str, Any], model_status: str, detail: str = "") -> None:
    if status not in STATUSES:
        raise ValueError(f"unknown run status {status!r}")
    with db.transaction() as conn:
        conn.execute(
            """UPDATE agent_ops_run SET finished_at = now(), status = %s, report = %s, model_status = %s,
                      detail = %s WHERE id = %s""",
            (status, jsonb(report), model_status, detail[:1000] or None, run_id))


def last_run(role: str, *, ok_only: bool = True) -> dict[str, Any] | None:
    with db.transaction() as conn:
        return conn.execute(
            f"""SELECT * FROM agent_ops_run WHERE role = %s {"AND status IN ('ok', 'partial')" if ok_only else ""}
                ORDER BY started_at DESC, id DESC LIMIT 1""", (role,)).fetchone()


def writer_llm(llm: Llm | None = None) -> tuple[Llm | None, BackendStatus]:
    """The model client for prose (titles, note) and the backend status to report; None when not configured."""
    if llm is not None:
        kind = getattr(llm.backend, "name", "api") if llm.backend is not None else "not_configured"
        return llm, BackendStatus(kind, "given")
    settings = AgentSettings.from_env()
    status = detect_backend(settings)
    if not status.configured:
        return None, status
    return Llm(make_backend(status.kind, settings), settings, budget=RunBudget(settings.run_tokens)), status
