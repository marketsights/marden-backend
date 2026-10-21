"""Running the agents: the sweep (intake) and the work loop (claim jobs, run them, respect caps and budgets).

    sweep()        store uploads, queue documents, reset stale jobs, resume budget-paused jobs, apply staff
                   decisions
    work()         run queued live jobs: highest priority first, at most MARDEN_AGENT_SOURCE_CAP per source
                   at once, MARDEN_AGENT_CONCURRENCY threads, one shared run budget

Both report 'not_configured' (and queue nothing) when there is no API key and no CLI fallback.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from marden.agents.backends import make_backend
from marden.agents.config import AgentSettings, BackendStatus, detect_backend
from marden.agents.intake import claim, queue_documents, record_status, reset_stale, resume_budget, store_uploads
from marden.agents.llm import Llm, RunBudget
from marden.agents.pipeline import JobOutcome, run_job
from marden.agents.review import apply_pending

log = logging.getLogger(__name__)

WORK_SECONDS = 600  # a work run stops claiming new jobs after this long


def concurrency() -> int:
    raw = (os.environ.get("MARDEN_AGENT_CONCURRENCY") or "").strip()
    return max(1, int(raw)) if raw.isdigit() else 3


def make_llm(*, settings: AgentSettings | None = None, budget: RunBudget | None = None,
             collect: bool = False) -> tuple[Llm, BackendStatus]:
    settings = settings or AgentSettings.from_env()
    status = detect_backend(settings)
    return Llm(make_backend(status.kind, settings), settings, budget=budget, collect=collect), status


def sweep() -> dict[str, Any]:
    settings = AgentSettings.from_env()
    status = detect_backend(settings)
    record_status(status, settings.models)
    out: dict[str, Any] = {"backend": status.kind, "detail": status.detail}
    try:
        out["uploads_stored"] = store_uploads()  # raw first, whatever the backend
    except Exception as exc:  # one bad upload must not stop people's decisions or the queue
        log.exception("agents sweep: storing uploads failed")
        out["uploads_stored"], out["uploads_error"] = 0, f"{type(exc).__name__}"
    out["decisions"] = len(apply_pending())  # people's decisions never wait for a model
    if not status.configured:
        out["status"] = "not_configured"
        out["queued"] = 0
        return out
    out["stale_reset"] = reset_stale()
    out["budget_resumed"] = resume_budget(settings)
    out["queued"] = queue_documents()
    out["status"] = "ok"
    return out


def work(*, max_jobs: int | None = None, threads: int | None = None, job_ids: list[int] | None = None,
         seconds: int = WORK_SECONDS) -> dict[str, Any]:
    settings = AgentSettings.from_env()
    status = detect_backend(settings)
    if not status.configured:
        return {"status": "not_configured", "detail": status.detail, "jobs": []}
    budget = RunBudget(settings.run_tokens)
    started = time.monotonic()
    outcomes: list[JobOutcome] = []
    threads = threads or concurrency()

    def one(job_id: int) -> JobOutcome:
        llm = Llm(make_backend(status.kind, settings), settings, budget=budget)
        return run_job(job_id, llm)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        pending: dict[Any, int] = {}
        explicit = list(job_ids or [])
        while True:
            room = threads - len(pending)
            done_enough = max_jobs is not None and len(outcomes) + len(pending) >= max_jobs
            if room > 0 and not done_enough and time.monotonic() - started < seconds and budget.used < budget.limit:
                if job_ids is not None:
                    take, explicit = explicit[:room], explicit[room:]
                else:
                    take = claim(settings.cap_for, limit=room)
                for jid in take:
                    pending[pool.submit(one, jid)] = jid
            if not pending:
                break
            fut = next(as_completed(pending))
            jid = pending.pop(fut)
            try:
                outcomes.append(fut.result())
            except Exception as exc:  # a bug in one job must not stop the others; recorded on the job
                log.exception("agent job %s crashed", jid)
                from marden.agents.pipeline import finish, load_job

                outcomes.append(finish(load_job(jid), "failed", f"crash: {type(exc).__name__}: {exc}"[:500]))
    return {"status": "ok", "backend": status.kind, "tokens": budget.used,
            "jobs": [{"job_id": o.job_id, "state": o.state, "detail": o.detail, "stats": o.stats} for o in outcomes]}
