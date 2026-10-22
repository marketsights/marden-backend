"""The researcher's records: research_search (every query), research_find (every hit and what became of it), and
what Marden already holds for a URL (stored, waiting for the extractor, or read)."""

from __future__ import annotations

from datetime import date
from typing import Any

from marden.core.db import Conn
from marden.core.jsonutil import jsonb

READ_STATES = frozenset({"done", "skipped", "failed"})


def last_window_end(conn: Conn, endpoint: str, query: str) -> date | None:
    """The end of the last window this exact query was searched over without failing."""
    row = conn.execute(
        """SELECT max(window_to) AS d FROM research_search WHERE endpoint = %s AND query = %s AND status = 'ok'""",
        (endpoint, query)).fetchone()
    return row["d"] if row else None


def last_searched(conn: Conn, endpoint: str) -> dict[str, Any]:
    """query -> when it was last searched (any status), for rotating a capped run through every query."""
    return {r["query"]: r["at"] for r in conn.execute(
        "SELECT query, max(created_at) AS at FROM research_search WHERE endpoint = %s GROUP BY query", (endpoint,))}


def add_search(conn: Conn, *, run_id: int, endpoint: str, query: str, since: date | None, until: date | None,
               subjects: list[dict[str, str]], gaps: list[str], status: str, document_id: int | None = None,
               hits: int | None = None, error: str | None = None) -> int:
    row = conn.execute(
        """INSERT INTO research_search (run_id, endpoint, query, window_from, window_to, subjects, gaps, document_id,
                                        hits, status, error)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (run_id, endpoint, query, since, until, jsonb(subjects), jsonb(gaps), document_id, hits, status,
         (error or "")[:1000] or None)).fetchone()
    assert row is not None
    return int(row["id"])


def add_find(conn: Conn, search_id: int, *, url: str, outcome: str, title: str | None = None, form: str | None = None,
             filed: date | None = None, reason: str | None = None, document_id: int | None = None,
             job_id: int | None = None) -> None:
    conn.execute(
        """INSERT INTO research_find (search_id, url, title, form, filed, outcome, reason, document_id, job_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (search_id, url) DO NOTHING""",
        (search_id, url, (title or "")[:300] or None, form, filed, outcome, (reason or "")[:500] or None, document_id,
         job_id))


def held(conn: Conn, url: str) -> dict[str, Any] | None:
    """Marden's newest copy of the file at this URL and its newest agent job, or None. Seed records that only
    name the URL (source kind 'seed': the archive's own JSON row, not the file) are not copies."""
    return conn.execute(
        """SELECT d.id, d.source_id, d.extract_state, j.id AS job_id, j.state AS job_state
           FROM document d JOIN source s ON s.id = d.source_id AND s.kind <> 'seed'
           LEFT JOIN LATERAL (SELECT id, state FROM agent_job WHERE document_id = d.id
                              ORDER BY id DESC LIMIT 1) j ON true
           WHERE d.url = %s ORDER BY d.id DESC LIMIT 1""", (url,)).fetchone()


def outcome_for_held(doc: dict[str, Any]) -> tuple[str, str]:
    """What a hit Marden already holds is: read, waiting, or stored without being queued (then it is queued)."""
    if doc["job_id"] is not None and doc["job_state"] not in ("queued", "running"):
        return "already_read", f"read by the agents (job {doc['job_id']}: {doc['job_state']})"
    if doc["extract_state"] in READ_STATES:
        return "already_read", f"stored by {doc['source_id']} and marked {doc['extract_state']}"
    if doc["extract_state"] == "queued":
        return "already_stored", f"stored by {doc['source_id']}, waiting for the extractor"
    return "queued", f"stored by {doc['source_id']} without extraction; now queued for the extractor"


def queue_held(conn: Conn, document_id: int, research: dict[str, Any]) -> None:
    """Hand a stored document to the intake: extract_state 'queued' (the sweep makes its job), with why."""
    conn.execute(
        """UPDATE document SET extract_state = 'queued',
                  meta = meta || jsonb_build_object('research', %s::jsonb)
           WHERE id = %s AND extract_state = 'none'""", (jsonb(research), document_id))
