"""One document through the agents: read -> route -> extract -> check -> verify -> link -> publish.

    outcome = run_job(job_id, llm)          # 'published' | 'needs_review' | 'skipped' | 'failed' | 'budget' |
                                            # 'waiting' (batch planning: model calls still pending)

Every stage is recorded on agent_job (stage, route, areas, stats, error), and every model call is cached, so a
job can be re-run at any time: it costs nothing until it reaches a call not made before. The document's
extract_state follows the job: done (published or needs review), skipped, failed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from marden.agents import extract, router, verify
from marden.agents.config import PROMPT_VERSION
from marden.agents.extract import Candidate
from marden.agents.linking import link
from marden.agents.llm import BudgetExceeded, Llm, LlmError
from marden.agents.publish import publish_candidates
from marden.agents.sections import Section, split
from marden.agents.text import DocText, UnreadableDocument, read_text
from marden.agents.validators import validate
from marden.core import db, documents
from marden.core.jsonutil import jsonb

log = logging.getLogger(__name__)


@dataclass
class JobOutcome:
    job_id: int
    state: str
    detail: str = ""
    stats: dict[str, Any] | None = None


def load_job(job_id: int) -> dict[str, Any]:
    with db.transaction() as conn:
        job = conn.execute("SELECT * FROM agent_job WHERE id = %s", (job_id,)).fetchone()
    if job is None:
        raise KeyError(f"agent_job {job_id} does not exist")
    return job


def set_job(job_id: int, **cols: Any) -> None:
    cols["updated_at"] = "now()"
    parts, params = [], []
    for k, v in cols.items():
        if v == "now()":
            parts.append(f"{k} = now()")
            continue
        parts.append(f"{k} = %s")
        params.append(jsonb(v) if k in ("route", "stats", "links") else v)
    with db.transaction() as conn:
        conn.execute(f"UPDATE agent_job SET {', '.join(parts)} WHERE id = %s", (*params, job_id))


def finish(job: dict[str, Any], state: str, detail: str = "", stats: dict[str, Any] | None = None) -> JobOutcome:
    doc_state = {"published": "done", "needs_review": "done", "skipped": "skipped", "failed": "failed"}.get(state)
    set_job(job["id"], state=state, error=detail[:2000] or None, stats=stats or job.get("stats") or {},
            finished_at="now()")
    if doc_state:
        documents.set_extract_state(job["document_id"], doc_state)
    log.info("agent job %s: %s %s", job["id"], state, (stats or {}).get("summary", ""))
    return JobOutcome(job["id"], state, detail, stats)


def chosen_sections(sections: list[Section], ids: list[str], max_sections: int) -> list[Section]:
    want = set(ids)
    picked = [s for s in sections if s.id in want] or sections
    return picked[:max_sections]


def _extract_all(llm: Llm, job: dict[str, Any], doc: Any, text: DocText, data: bytes, sections: list[Section],
                 route: router.Route) -> tuple[list[Candidate], list[dict[str, Any]], str, bool]:
    cands: list[Candidate] = []
    subjects: list[dict[str, Any]] = []
    summaries: list[str] = []
    complete = True
    for area, ids in route.areas.items():
        for section in chosen_sections(sections, ids, llm.settings.max_sections):
            out = extract.extract(llm, area, doc, text, section, data, job["id"])
            if out is None:
                complete = False
                continue
            got, subs, summary = out
            cands += got
            subjects += subs
            if summary:
                summaries.append(summary)
    return cands, subjects, " ".join(summaries)[:1500] or route.summary, complete


def run_job(job_id: int, llm: Llm) -> JobOutcome:
    job = load_job(job_id)
    doc = documents.get_document(job["document_id"])
    if doc is None:
        return finish(job, "failed", "document row is gone")
    if not llm.collect:
        set_job(job_id, state="running", attempts=int(job["attempts"]) + 1, started_at="now()")
    try:
        data = doc.read()
        text = read_text(data, doc.content_type, doc.url)
    except (UnreadableDocument, KeyError) as exc:
        return finish(job, "failed", f"unreadable: {exc}")
    sections = split(text, llm.settings.section_chars)
    try:
        route = router.route(llm, doc, text, sections, data, job_id)
        if route is None:
            return JobOutcome(job_id, "waiting", "router")
        set_job(job_id, stage="routed", route=route.as_json(), areas=list(route.areas))
        if not route.areas:
            return finish(job, "skipped", f"no specialist area applies ({route.doc_kind})",
                          {"summary": "no area", "doc_kind": route.doc_kind})
        if text.scanned and llm.backend is not None and llm.backend.view != "native":
            return finish(job, "failed", "scanned document: needs the Anthropic API (native PDF reading)")
        cands, subjects, summary, complete = _extract_all(llm, job, doc, text, data, sections, route)
        if not complete:
            return JobOutcome(job_id, "waiting", "extract")
        set_job(job_id, stage="extracted")
        validate(cands, text)
        if not verify.verify(llm, cands, text, job_id, doc.sha256, extract.doc_metadata(doc, text)):
            return JobOutcome(job_id, "waiting", "verify")
        set_job(job_id, stage="verified")
        if not link(llm, job, doc, text, cands, subjects, summary):
            return JobOutcome(job_id, "waiting", "link")
        set_job(job_id, stage="linked")
        if llm.collect:
            return JobOutcome(job_id, "waiting", "ready")
        stats = publish_candidates(job, doc, cands)
    except BudgetExceeded as exc:
        set_job(job_id, state="budget", error=str(exc))
        return JobOutcome(job_id, "budget", str(exc))
    except (LlmError, ValueError) as exc:
        return finish(job, "failed", f"{type(exc).__name__}: {exc}")
    stats["areas"] = list(route.areas)
    stats["doc_kind"] = route.doc_kind
    with db.transaction() as conn:
        used = conn.execute(
            """SELECT coalesce(sum(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens), 0) AS n,
                      coalesce(sum(cost_usd), 0)::float AS cost FROM agent_call WHERE job_id = %s""",
            (job_id,)).fetchone()
    stats["tokens"], stats["cost_usd"] = int(used["n"]), round(float(used["cost"]), 4)
    stats["summary"] = (f"{stats['published']} published, {stats['review']} to review, {stats['rejected']} rejected"
                        + (f", {stats['duplicate']} already published" if stats.get("duplicate") else ""))
    set_job(job_id, stage="published")
    state = "needs_review" if stats["review"] else "published"
    return finish(job, state, "", stats)


def enqueue_document(document_id: int, *, priority: int = 50, reason: str = "manual",
                     links: dict[str, str] | None = None, mode: str = "live") -> int:
    """Queue one document for the current prompt version (idempotent); returns the job id."""
    doc = documents.get_document(document_id)
    if doc is None:
        raise KeyError(f"document {document_id} does not exist")
    with db.transaction() as conn:
        row = conn.execute(
            """INSERT INTO agent_job (document_id, sha256, source_id, prompt_version, priority, priority_reason,
                                      links, mode)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (document_id, prompt_version) DO UPDATE SET document_id = EXCLUDED.document_id
               RETURNING id""",
            (doc.id, doc.sha256, doc.source_id, PROMPT_VERSION, priority, reason,
             jsonb(links or (doc.meta or {}).get("links") or {}), mode),
        ).fetchone()
        if doc.extract_state != "queued":
            conn.execute("UPDATE document SET extract_state = 'queued' WHERE id = %s AND extract_state <> 'queued'",
                         (doc.id,))
    assert row is not None
    return int(row["id"])
