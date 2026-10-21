"""Backfills through the Message Batches API (half price; results within 24 h, usually within an hour).

A batch job advances one stage per batch: the pipeline runs in planning mode (marden.agents.llm collect
mode), every request it would make that is not cached yet is submitted as one batch, and the results are
written to agent_call under each request's hash. The next planning pass then finds those answers cached and
either needs the next stage's requests (another batch) or is complete, and the job is run live on cache hits
to publish. Refusal fallbacks are not available on the Batches API, so a declined request is recorded as a
refusal and the job fails over to review honestly.
"""

from __future__ import annotations

import logging
from typing import Any

from marden.agents.backends import ApiBackend
from marden.agents.config import AgentSettings, cost_usd, detect_backend
from marden.agents.intake import queue_documents
from marden.agents.llm import Llm, LlmRequest, LlmResult
from marden.agents.pipeline import run_job
from marden.core import db
from marden.core.jsonutil import jsonb

log = logging.getLogger(__name__)
MAX_REQUESTS = 5_000


def _jobs(limit: int) -> list[int]:
    with db.transaction() as conn:
        rows = conn.execute(
            """SELECT id FROM agent_job WHERE state = 'queued' AND mode = 'batch'
               ORDER BY priority DESC, id LIMIT %s""", (limit,)).fetchall()
    return [int(r["id"]) for r in rows]


def mark_batch(limit: int = 500) -> int:
    """Move queued backfill documents (priority <= 20) to batch mode."""
    queue_documents()
    with db.transaction() as conn:
        rows = conn.execute(
            """UPDATE agent_job SET mode = 'batch', updated_at = now() WHERE id IN (
                 SELECT id FROM agent_job WHERE state = 'queued' AND mode = 'live' AND priority <= 20
                 ORDER BY id LIMIT %s) RETURNING id""", (limit,)).fetchall()
    return len(rows)


def plan(job_ids: list[int], settings: AgentSettings, backend: ApiBackend) -> tuple[list[Any], list[int]]:
    """Requests still needed, deduplicated by hash, and the jobs that are ready to publish."""
    pending: dict[str, tuple[LlmRequest, str, str]] = {}
    ready: list[int] = []
    for job_id in job_ids:
        llm = Llm(backend, settings, collect=True)
        out = run_job(job_id, llm)
        if out.state == "waiting" and llm.pending:
            for h, req, model, effort in llm.pending:
                pending.setdefault(h, (req, model, effort))
        elif out.state == "waiting":
            ready.append(job_id)
    return [(h, *v) for h, v in pending.items()], ready


def submit(requests: list[Any], backend: ApiBackend, stage: str = "plan") -> str | None:
    if not requests:
        return None
    requests = requests[:MAX_REQUESTS]
    items, ids = [], {}
    for i, (h, req, model, effort) in enumerate(requests):
        cid = f"r{i:05d}"
        params = backend.params(req, model, effort)
        items.append({"custom_id": cid, "params": params})
        ids[cid] = {"hash": h, "agent": req.agent, "job_id": req.job_id, "section": req.section,
                    "sha": req.document_sha256, "model": model}
    batch = backend.client.messages.batches.create(requests=items)
    with db.transaction() as conn:
        conn.execute("INSERT INTO agent_batch (id, stage, request_count, custom_ids) VALUES (%s, %s, %s, %s)",
                     (batch.id, stage, len(items), jsonb(ids)))
    log.info("agent batch %s submitted: %d requests", batch.id, len(items))
    return str(batch.id)


def apply_results(batch_id: str, backend: ApiBackend, settings: AgentSettings) -> dict[str, int]:
    with db.transaction() as conn:
        row = conn.execute("SELECT custom_ids FROM agent_batch WHERE id = %s", (batch_id,)).fetchone()
    ids: dict[str, Any] = row["custom_ids"] if row else {}
    llm = Llm(backend, settings)
    counts = {"succeeded": 0, "errored": 0, "other": 0}
    for result in backend.client.messages.batches.results(batch_id):
        meta = ids.get(result.custom_id)
        if meta is None:
            continue
        if result.result.type != "succeeded":
            counts["errored" if result.result.type == "errored" else "other"] += 1
            continue
        msg = result.result.message
        usage = ApiBackend.usage(msg)
        req = LlmRequest(agent=meta["agent"], role="", system="", tool={}, content=[], job_id=meta["job_id"],
                         document_sha256=meta["sha"], section=meta["section"])
        tool_name = {"router": "route_document", "verifier": "record_verdicts", "linker": "record_links"}.get(
            meta["agent"], "record_facts")
        res = LlmResult(ApiBackend.tool_input(msg, tool_name), msg.stop_reason, meta["model"], "batch", usage,
                        cost=cost_usd(meta["model"], usage, batch=True))
        llm.record(req, res, batch_id=batch_id, request_hash=meta["hash"])
        counts["succeeded"] += 1
    with db.transaction() as conn:
        conn.execute("UPDATE agent_batch SET status = 'applied', applied_at = now() WHERE id = %s", (batch_id,))
    return counts


def poll() -> dict[str, Any]:
    """Apply ended batches, then plan the next stage for batch jobs (or publish the ready ones)."""
    settings = AgentSettings.from_env()
    status = detect_backend(settings)
    if status.kind != "api":
        return {"status": "not_configured", "detail": "the Batch API needs ANTHROPIC_API_KEY"}
    backend = ApiBackend(settings)
    applied = {}
    with db.transaction() as conn:
        open_ids = [r["id"] for r in conn.execute("SELECT id FROM agent_batch WHERE status = 'in_progress'").fetchall()]
    for bid in open_ids:
        b = backend.client.messages.batches.retrieve(bid)
        if b.processing_status == "ended":
            with db.transaction() as conn:
                conn.execute("UPDATE agent_batch SET status = 'ended', ended_at = now() WHERE id = %s", (bid,))
            applied[bid] = apply_results(bid, backend, settings)
    if open_ids and len(applied) < len(open_ids):
        return {"status": "waiting", "applied": applied}
    return {"status": "ok", "applied": applied, **advance(settings, backend)}


def advance(settings: AgentSettings, backend: ApiBackend, limit: int = 500) -> dict[str, Any]:
    jobs = _jobs(limit)
    requests, ready = plan(jobs, settings, backend)
    published = []
    for job_id in ready:
        out = run_job(job_id, Llm(backend, settings))
        published.append({"job_id": job_id, "state": out.state})
    batch_id = submit(requests, backend)
    return {"jobs": len(jobs), "requests": len(requests), "batch_id": batch_id, "finished": published}


def backfill(limit: int = 500) -> dict[str, Any]:
    """Queue old documents in batch mode and submit the first batch."""
    settings = AgentSettings.from_env()
    status = detect_backend(settings)
    if status.kind != "api":
        return {"status": "not_configured",
                "detail": f"the Batch API needs ANTHROPIC_API_KEY ({status.detail}); use `agents run` instead"}
    moved = mark_batch(limit)
    return {"status": "ok", "moved_to_batch": moved, **advance(settings, ApiBackend(settings), limit)}
