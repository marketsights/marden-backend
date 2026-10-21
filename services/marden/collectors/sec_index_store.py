"""Database reads and writes for the `sec_index` collector: watched CIKs, known filings, filing events."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from marden.collectors.sec_index_parse import FilingIndex, event_title, normalize_cik
from marden.core.db import Conn
from marden.core.documents import DocumentRow
from marden.core.records import EventWriter

SOURCE_NAME = "SEC EDGAR"
EVENT_KIND = "filing"
FILING_INDEX = "filing_index"


@dataclass
class Watched:
    """Watched CIK -> the entity ids that carry it (normally one)."""

    entities: dict[str, list[str]] = field(default_factory=dict)
    bad: list[str] = field(default_factory=list)  # entity ids whose cik is not a CIK

    def __len__(self) -> int:
        return len(self.entities)


def watched_ciks(conn: Conn, only: Iterable[str] | None = None) -> Watched:
    """Every entity row with a CIK (issuers, BDCs, fund filers, SPVs), optionally limited to `only`."""
    wanted = {c for c in (normalize_cik(x) for x in only or ()) if c} if only is not None else None
    out = Watched()
    for row in conn.execute("SELECT id, cik FROM entity WHERE cik IS NOT NULL ORDER BY id").fetchall():
        cik = normalize_cik(row["cik"])
        if cik is None:
            out.bad.append(row["id"])
        elif wanted is None or cik in wanted:
            out.entities.setdefault(cik, []).append(row["id"])
    return out


def index_document(conn: Conn, source_id: str, accession: str) -> DocumentRow | None:
    """The stored filing index page of an accession (any CIK folder)."""
    row = conn.execute(
        """SELECT * FROM document WHERE source_id = %s AND doc_type = %s AND external_id = %s
           ORDER BY fetched_at, id LIMIT 1""",
        (source_id, FILING_INDEX, accession),
    ).fetchone()
    return DocumentRow.from_row(row, was_new=False) if row else None


def stored_urls(conn: Conn, source_id: str, urls: Iterable[str]) -> set[str]:
    urls = list(urls)
    if not urls:
        return set()
    rows = conn.execute(
        "SELECT DISTINCT url FROM document WHERE source_id = %s AND url = ANY(%s)", (source_id, urls)
    ).fetchall()
    return {r["url"] for r in rows}


def event_subjects(conn: Conn, entity_ids: Iterable[str]) -> list[tuple[str, str]]:
    """Where a filing event goes: the filer's entities; the instruments they or their direct
    subsidiaries (entity.parent_id, e.g. borrower SPVs) issued; the sites of those instruments and
    the sites the filer develops (chain_party role 'developer'). Tenants and end users are left
    out: their filings are company-wide, not about one site."""
    ids = list(entity_ids)
    rows = conn.execute(
        """SELECT i.id, i.site_slug FROM instrument i LEFT JOIN entity e ON e.id = i.issuer_entity_id
           WHERE i.issuer_entity_id = ANY(%(ids)s) OR e.parent_id = ANY(%(ids)s) ORDER BY i.id""",
        {"ids": ids},
    ).fetchall()
    developed = conn.execute(
        "SELECT site_slug FROM chain_party WHERE role = 'developer' AND entity_id = ANY(%s)", (ids,)
    ).fetchall()
    sites = sorted({r["site_slug"] for r in rows if r["site_slug"]} | {r["site_slug"] for r in developed})
    return [("entity", e) for e in ids] + [("instrument", r["id"]) for r in rows] + [("site", s) for s in sites]


def subjects_with_event(conn: Conn, document_id: int) -> set[tuple[str, str]]:
    rows = conn.execute(
        "SELECT subject_type, subject_id FROM event WHERE kind = %s AND document_id = %s", (EVENT_KIND, document_id)
    ).fetchall()
    return {(r["subject_type"], r["subject_id"]) for r in rows}


def write_filing_events(
    conn: Conn, fi: FilingIndex, cik: str, subjects: list[tuple[str, str]], index_doc: DocumentRow
) -> int:
    """Publish one `filing` event (tag F) per subject. Returns how many were new."""
    writer = EventWriter(conn)
    title = event_title(fi, cik)
    new = 0
    for subject_type, subject_id in subjects:
        res = writer.add(
            subject_type, subject_id, fi.filing_date, EVENT_KIND, title, tag="F", source_name=SOURCE_NAME,
            source_url=index_doc.url, document_id=index_doc.id, status="published",
        )
        new += int(res.created)
    return new
