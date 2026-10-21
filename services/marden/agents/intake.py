"""Intake: every document that needs reading reaches the agents, continuously and once.

Sources of documents:
- collectors: a document stored with extract_state 'queued' (8-K, EX-10, FERC issuances, permits, agendas);
  a trigger NOTIFYs 'marden_agents' and the sweep (every 30 s) queues it;
- staff uploads: POST /api/documents stores the file in agent_upload; the sweep moves it to the raw store
  (R2 or local) as a `document` of source 'upload' and queues it;
- the CLI: `python -m marden agents ingest <file|dir|url>`;
- the R2 inbox: objects under inbox/ in the raw bucket, when R2 is configured.
Nothing is queued while the agents are not configured (no API key and no CLI fallback): documents keep
extract_state 'queued' and are picked up once a backend exists. Priority: documents about watched subjects
or 8-K current reports jump the line; uploads come next; old documents are backfill.
"""

from __future__ import annotations

import logging
import mimetypes
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from marden.agents.config import PROMPT_VERSION, AgentSettings, BackendStatus
from marden.agents.llm import day_tokens_used
from marden.agents.text import sniff
from marden.core import db, documents
from marden.core.config import get_settings
from marden.core.jsonutil import jsonb
from marden.core.sources import declare_source, upsert_spec

log = logging.getLogger(__name__)

UPLOAD_SOURCE = declare_source(
    "upload", "Staff upload", "manual", None, None,
    "Files staff upload (POST /api/documents or the CLI) from records they obtained; stored raw first.")
URL_SOURCE = declare_source(
    "agent_url", "Staff-requested document", "manual", None, None,
    "Single public documents staff ask the agents to read (python -m marden agents ingest <url>); fetched "
    "politely with the declared User-Agent and per-host rate limits; bot protection is never bypassed.")
INBOX_SOURCE = declare_source(
    "r2_inbox", "R2 inbox", "manual", None, None,
    "Files dropped under inbox/ in the raw R2 bucket by staff; read once per key and etag.")
CONTENT_TYPES = {"pdf": "application/pdf", "html": "text/html", "txt": "text/plain",
                 "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                 "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}
ACCEPTED_KINDS = frozenset({"pdf", "docx", "html", "txt", "xlsx", "pptx"})
STALE_RUNNING = timedelta(minutes=45)
BUDGET_RESUMES = 5  # a job paused this many times by a budget stays paused (status 'budget') for a person
SWEEP_LIMIT = 500

PRIORITY_SQL = """
CASE
  WHEN EXISTS (SELECT 1 FROM watchlist_item w JOIN entity e ON d.meta->>'cik' IS NOT NULL
                 AND ltrim(e.cik, '0') = ltrim(d.meta->>'cik', '0')
               WHERE (w.kind = 'instrument' AND w.ref_id IN (SELECT id FROM instrument WHERE issuer_entity_id = e.id))
                  OR (w.kind = 'site' AND w.ref_id IN (SELECT site_slug FROM chain_party WHERE entity_id = e.id)))
    OR EXISTS (SELECT 1 FROM watchlist_item w WHERE (w.kind = 'site' AND w.ref_id = d.meta->'links'->>'site')
                 OR (w.kind = 'instrument' AND w.ref_id = d.meta->'links'->>'instrument')) THEN 90
  WHEN d.doc_type IN ('8-K', '8-K/A') THEN 80
  WHEN d.source_id IN ('upload', 'agent_url', 'r2_inbox') THEN 70
  WHEN coalesce(d.doc_date, d.fetched_at::date) < current_date - 30 THEN 20
  ELSE 50
END"""
REASON_SQL = """CASE WHEN {p} = 90 THEN 'watched subject' WHEN {p} = 80 THEN 'current report'
                     WHEN {p} = 70 THEN 'staff upload' WHEN {p} = 20 THEN 'backfill' ELSE 'routine' END"""


def ensure_intake_sources(conn: db.Conn | None = None) -> None:
    for spec in (UPLOAD_SOURCE, URL_SOURCE, INBOX_SOURCE):
        upsert_spec(spec, conn=conn)


def record_status(status: BackendStatus, models: dict[str, str]) -> None:
    with db.transaction() as conn:
        conn.execute(
            """INSERT INTO agent_status (id, backend, detail, models, checked_at) VALUES (true, %s, %s, %s, now())
               ON CONFLICT (id) DO UPDATE SET backend = EXCLUDED.backend, detail = EXCLUDED.detail,
                 models = EXCLUDED.models, checked_at = now()""",
            (status.kind, status.detail, jsonb(models if status.configured else {})),
        )


def queue_documents(limit: int = SWEEP_LIMIT) -> int:
    """agent_job rows for queued documents that have none for this prompt version (idempotent)."""
    with db.transaction() as conn:
        rows = conn.execute(
            f"""INSERT INTO agent_job (document_id, sha256, source_id, prompt_version, priority, priority_reason, links)
                SELECT d.id, d.sha256, d.source_id, %s, p.priority, {REASON_SQL.format(p='p.priority')},
                       coalesce(d.meta->'links', '{{}}'::jsonb)
                FROM document d CROSS JOIN LATERAL (SELECT {PRIORITY_SQL} AS priority) p
                WHERE d.extract_state = 'queued'
                  AND NOT EXISTS (SELECT 1 FROM agent_job j WHERE j.document_id = d.id AND j.prompt_version = %s)
                ORDER BY p.priority DESC, d.id
                LIMIT %s
                ON CONFLICT (document_id, prompt_version) DO NOTHING
                RETURNING id""",
            (PROMPT_VERSION, PROMPT_VERSION, limit),
        ).fetchall()
    return len(rows)


def reset_stale(now: datetime | None = None) -> int:
    """Jobs left 'running' by a worker that died go back to the queue."""
    cutoff = (now or datetime.now(UTC)) - STALE_RUNNING
    with db.transaction() as conn:
        rows = conn.execute(
            "UPDATE agent_job SET state = 'queued', updated_at = now() WHERE state = 'running' AND updated_at < %s "
            "RETURNING id", (cutoff,)).fetchall()
    return len(rows)


def resume_budget(settings: AgentSettings) -> int:
    """Jobs paused by a token budget go back to the queue when there is room again: a run budget resets with
    the next run, the daily budget at 00:00 UTC. Model calls already made are cached, so a resumed job
    continues where it stopped. A job paused BUDGET_RESUMES times (it needs more than one run's budget)
    stays paused and visible in the status API."""
    if day_tokens_used() >= settings.day_tokens:
        return 0
    with db.transaction() as conn:
        rows = conn.execute(
            """UPDATE agent_job SET state = 'queued', updated_at = now()
               WHERE state = 'budget' AND attempts < %s RETURNING id""", (BUDGET_RESUMES,)).fetchall()
    return len(rows)


def ingest_bytes(data: bytes, *, source_id: str, url: str, filename: str | None, title: str | None = None,
                 doc_date: str | None = None, links: dict[str, str] | None = None, filed: bool | None = None,
                 meta: dict[str, Any] | None = None, conn: db.Conn | None = None) -> documents.DocumentRow:
    kind = sniff(data, mimetypes.guess_type(filename or "")[0])
    if kind not in ACCEPTED_KINDS:
        raise ValueError(f"{filename or url}: not a PDF, DOCX, PPTX, HTML, TXT or XLSX file")
    extra = {**(meta or {}), "filename": filename, "links": links or {}}
    if filed is not None:
        extra["filed"] = filed
    return documents.record_document(source_id, url, data, content_type=CONTENT_TYPES[kind], doc_type=f"upload_{kind}",
                                     title=title or filename, doc_date=doc_date, meta=extra,
                                     extract_state="queued", conn=conn)


def store_uploads(limit: int = 50) -> int:
    """agent_upload rows (from the API) -> raw store + document; the staged bytes are cleared."""
    stored = 0
    with db.transaction() as conn:
        ensure_intake_sources(conn)
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM agent_upload WHERE status = 'pending' ORDER BY id LIMIT %s", (limit,)).fetchall()]
    for upload_id in ids:
        with db.transaction() as conn:
            up = conn.execute("SELECT * FROM agent_upload WHERE id = %s AND status = 'pending' FOR UPDATE SKIP LOCKED",
                              (upload_id,)).fetchone()
            if up is None:
                continue
            try:
                doc = ingest_bytes(
                    bytes(up["data"]), source_id="upload", url=up["source_url"] or f"upload://{up['id']}/{up['filename']}",
                    filename=up["filename"], title=up["title"], doc_date=up["doc_date"], links=up["links"],
                    filed=up["filed"], meta={"upload_id": up["id"], "uploaded_by": up["uploaded_by"],
                                             "source_url": up["source_url"]}, conn=conn)
            except (ValueError, OSError) as exc:
                conn.execute("UPDATE agent_upload SET status = 'failed', error = %s WHERE id = %s",
                             (str(exc)[:500], upload_id))
                continue
            conn.execute(
                """UPDATE agent_upload SET status = 'stored', document_id = %s, data = NULL, stored_at = now()
                   WHERE id = %s""", (doc.id, upload_id))
            stored += 1
    return stored


def ingest_path(path: Path, *, links: dict[str, str] | None = None, filed: bool | None = None,
                title: str | None = None) -> list[documents.DocumentRow]:
    """A local file, or every file under a directory, stored raw and queued."""
    with db.transaction() as conn:
        ensure_intake_sources(conn)
    files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
    out = []
    for f in files:
        data = f.read_bytes()
        try:
            out.append(ingest_bytes(data, source_id="upload", url=f"upload://cli/{f.name}", filename=f.name,
                                    title=title if len(files) == 1 else None, links=links, filed=filed,
                                    meta={"uploaded_by": "cli"}))
        except ValueError as exc:
            log.warning("skipped %s: %s", f.name, exc)
    return out


def ingest_url(url: str, *, links: dict[str, str] | None = None, filed: bool | None = None,
               title: str | None = None) -> documents.DocumentRow:
    from marden.core.http import fetch_document

    with db.transaction() as conn:
        ensure_intake_sources(conn)
    meta: dict[str, Any] = {"links": links or {}, "source_url": url}
    if filed is not None:
        meta["filed"] = filed
    return fetch_document("agent_url", url, doc_type="url_document", title=title, meta=meta, extract_state="queued")


def r2_inbox(limit: int = 100) -> dict[str, Any]:
    """Ingest new objects under inbox/ in the raw bucket (R2 only)."""
    settings = get_settings()
    if not settings.has_r2():
        return {"status": "not_configured", "detail": "R2 is not configured (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
                                                       "R2_SECRET_ACCESS_KEY)"}
    from marden.core.rawstore import R2Store

    store = R2Store(settings)
    listing = store.client.list_objects_v2(Bucket=store.bucket, Prefix="inbox/", MaxKeys=limit)
    new = 0
    with db.transaction() as conn:
        ensure_intake_sources(conn)
    for obj in listing.get("Contents") or []:
        key, etag = obj["Key"], str(obj.get("ETag") or "").strip('"')
        if key.endswith("/"):
            continue
        with db.transaction() as conn:
            if conn.execute("SELECT 1 FROM agent_inbox WHERE key = %s AND etag = %s", (key, etag)).fetchone():
                continue
            data = store.client.get_object(Bucket=store.bucket, Key=key)["Body"].read()
            try:
                doc = ingest_bytes(data, source_id="r2_inbox", url=f"r2://{store.bucket}/{key}",
                                   filename=key.rsplit("/", 1)[-1], meta={"r2_key": key}, conn=conn)
                doc_id: int | None = doc.id
            except ValueError as exc:
                log.warning("inbox object skipped: %s", exc)
                doc_id = None
            conn.execute(
                """INSERT INTO agent_inbox (key, etag, document_id) VALUES (%s, %s, %s)
                   ON CONFLICT (key) DO UPDATE SET etag = EXCLUDED.etag, document_id = EXCLUDED.document_id,
                     seen_at = now()""", (key, etag, doc_id))
            new += 1
    return {"status": "ok", "new": new}


def claim(source_caps: Any, limit: int = 1, mode: str = "live") -> list[int]:
    """Take the next queued jobs by priority, never more than a source's cap running at once."""
    with db.transaction() as conn:
        running = {r["source_id"]: int(r["n"]) for r in conn.execute(
            "SELECT source_id, count(*) AS n FROM agent_job WHERE state = 'running' GROUP BY 1").fetchall()}
        rows = conn.execute(
            """SELECT id, source_id FROM agent_job WHERE state = 'queued' AND mode = %s
               ORDER BY priority DESC, created_at, id LIMIT 200 FOR UPDATE SKIP LOCKED""", (mode,)).fetchall()
        taken: list[int] = []
        for r in rows:
            if len(taken) >= limit:
                break
            if running.get(r["source_id"], 0) >= source_caps(r["source_id"]):
                continue
            conn.execute("UPDATE agent_job SET state = 'running', updated_at = now() WHERE id = %s", (r["id"],))
            running[r["source_id"]] = running.get(r["source_id"], 0) + 1
            taken.append(int(r["id"]))
    return taken
