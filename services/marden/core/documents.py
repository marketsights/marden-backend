"""The `document` table: one row per (source, url, sha256), written right after the raw bytes are stored."""

from __future__ import annotations

import builtins
from dataclasses import dataclass, fields
from datetime import date, datetime
from typing import Any

from marden.core import db, rawstore
from marden.core.db import Conn, Row
from marden.core.jsonutil import jsonb, sha256_hex

EXTRACT_STATES = frozenset({"none", "queued", "done", "failed", "skipped"})


@dataclass(frozen=True)
class DocumentRow:
    id: int
    source_id: str
    url: str
    sha256: str
    fetched_at: datetime
    content_type: str | None
    bytes: int | None
    storage_key: str
    title: str | None
    doc_type: str | None
    doc_date: date | None
    external_id: str | None
    meta: dict[str, Any]
    extract_state: str
    was_new: bool = False

    @classmethod
    def from_row(cls, row: Row, *, was_new: bool) -> DocumentRow:
        names = {f.name for f in fields(cls)} - {"was_new"}
        return cls(**{k: row[k] for k in names}, was_new=was_new)

    def read(self) -> builtins.bytes:  # `bytes` is a column name on this class
        """The stored raw bytes (sha256-verified)."""
        return rawstore.get(self.storage_key)


def _coerce_date(value: date | str | None) -> date | None:
    if value is None or (isinstance(value, date) and not isinstance(value, datetime)):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(value)


_INSERT = """
INSERT INTO document (source_id, url, sha256, content_type, bytes, storage_key, title, doc_type,
                      doc_date, external_id, meta, extract_state)
VALUES (%(source_id)s, %(url)s, %(sha256)s, %(content_type)s, %(bytes)s, %(storage_key)s, %(title)s,
        %(doc_type)s, %(doc_date)s, %(external_id)s, %(meta)s, %(extract_state)s)
ON CONFLICT (source_id, url, sha256) DO NOTHING
RETURNING *
"""


def record_document(
    source_id: str,
    url: str,
    data: bytes,
    *,
    content_type: str | None,
    doc_type: str | None = None,
    title: str | None = None,
    doc_date: date | str | None = None,
    external_id: str | None = None,
    meta: dict[str, Any] | None = None,
    extract_state: str = "none",
    conn: Conn | None = None,
) -> DocumentRow:
    """Raw first: store the bytes, then insert the document row (idempotent on source/url/sha256)."""
    if extract_state not in EXTRACT_STATES:
        raise ValueError(f"extract_state must be one of {sorted(EXTRACT_STATES)}")
    if not url:
        raise ValueError("document url is required")
    storage_key = rawstore.put(data, source_id=source_id, url=url, content_type=content_type)
    params = {
        "source_id": source_id,
        "url": url,
        "sha256": sha256_hex(data),
        "content_type": content_type,
        "bytes": len(data),
        "storage_key": storage_key,
        "title": title,
        "doc_type": doc_type,
        "doc_date": _coerce_date(doc_date),
        "external_id": external_id,
        "meta": jsonb(meta or {}),
        "extract_state": extract_state,
    }
    with db.transaction(conn) as c:
        row = c.execute(_INSERT, params).fetchone()
        if row is not None:
            return DocumentRow.from_row(row, was_new=True)
        existing = c.execute(
            "SELECT * FROM document WHERE source_id = %s AND url = %s AND sha256 = %s",
            (source_id, url, params["sha256"]),
        ).fetchone()
    assert existing is not None
    return DocumentRow.from_row(existing, was_new=False)


def latest_document(source_id: str, url: str, *, conn: Conn | None = None) -> DocumentRow | None:
    """The most recently fetched version of a URL for a source."""
    with db.transaction(conn) as c:
        row = c.execute(
            "SELECT * FROM document WHERE source_id = %s AND url = %s ORDER BY fetched_at DESC, id DESC LIMIT 1",
            (source_id, url),
        ).fetchone()
    return DocumentRow.from_row(row, was_new=False) if row else None


def get_document(document_id: int, *, conn: Conn | None = None) -> DocumentRow | None:
    with db.transaction(conn) as c:
        row = c.execute("SELECT * FROM document WHERE id = %s", (document_id,)).fetchone()
    return DocumentRow.from_row(row, was_new=False) if row else None


def set_extract_state(document_id: int, state: str, *, conn: Conn | None = None) -> None:
    if state not in EXTRACT_STATES:
        raise ValueError(f"extract_state must be one of {sorted(EXTRACT_STATES)}")
    with db.transaction(conn) as c:
        c.execute("UPDATE document SET extract_state = %s WHERE id = %s", (state, document_id))
