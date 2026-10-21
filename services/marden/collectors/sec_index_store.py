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


def entities_with_event(conn: Conn, document_id: int, entity_ids: Iterable[str]) -> set[str]:
    rows = conn.execute(
        """SELECT DISTINCT subject_id FROM event WHERE subject_type = 'entity' AND kind = %s
           AND document_id = %s AND subject_id = ANY(%s)""",
        (EVENT_KIND, document_id, list(entity_ids)),
    ).fetchall()
    return {r["subject_id"] for r in rows}


def linked_subjects(conn: Conn, entity_ids: Iterable[str]) -> tuple[list[str], list[str]]:
    """Instruments issued by these entities, and the financed sites of those instruments."""
    rows = conn.execute(
        "SELECT id, site_slug FROM instrument WHERE issuer_entity_id = ANY(%s) ORDER BY id", (list(entity_ids),)
    ).fetchall()
    instruments = [r["id"] for r in rows]
    sites = sorted({r["site_slug"] for r in rows if r["site_slug"]})
    return instruments, sites


def write_filing_events(
    conn: Conn, fi: FilingIndex, cik: str, entity_ids: list[str], index_doc: DocumentRow
) -> tuple[int, int]:
    """Publish one `filing` event (tag F) per entity, instrument and site. Returns (new, seen)."""
    writer = EventWriter(conn)
    title = event_title(fi, cik)
    instruments, sites = linked_subjects(conn, entity_ids)
    subjects = [("entity", e) for e in entity_ids] + [("instrument", i) for i in instruments]
    subjects += [("site", s) for s in sites]
    new = 0
    for subject_type, subject_id in subjects:
        res = writer.add(
            subject_type, subject_id, fi.filing_date, EVENT_KIND, title, tag="F", source_name=SOURCE_NAME,
            source_url=index_doc.url, document_id=index_doc.id, status="published",
        )
        new += int(res.created)
    return new, len(subjects)
