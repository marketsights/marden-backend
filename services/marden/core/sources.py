"""Source declarations and the `source` table.

Each collector declares its source once at import time:

    SOURCE = declare_source("sec_edgar", "SEC EDGAR", "api", "https://www.sec.gov", 10,
                            "SEC fair access: declared User-Agent, < 10 req/s")

and the declaration is upserted into `source` with `ensure_source` / `sync_registered`.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from marden.core import db
from marden.core.db import Conn, Row

SOURCE_KINDS = frozenset({"api", "portal", "feed", "seed", "press", "model", "manual"})
_ID = re.compile(r"^[a-z0-9][a-z0-9_\-]*$")


class SourceError(RuntimeError):
    pass


class UnknownSource(SourceError):
    pass


class SourceBlocked(SourceError):
    """The source's terms forbid automated collection (`source.blocked`)."""


@dataclass(frozen=True)
class SourceSpec:
    id: str
    name: str
    kind: str
    base_url: str | None
    cadence_minutes: int | None
    terms_note: str
    blocked: bool = False
    blocked_reason: str | None = None

    def __post_init__(self) -> None:
        if not _ID.match(self.id):
            raise ValueError(f"source id must be lowercase snake/kebab case: {self.id!r}")
        if not self.name.strip():
            raise ValueError("source name is required")
        if self.kind not in SOURCE_KINDS:
            raise ValueError(f"source kind must be one of {sorted(SOURCE_KINDS)}: {self.kind!r}")
        if self.cadence_minutes is not None and self.cadence_minutes <= 0:
            raise ValueError("cadence_minutes must be positive or None (on demand)")
        if not self.terms_note or not self.terms_note.strip():
            raise ValueError(f"source {self.id}: terms_note is required (terms checked before collecting)")
        if self.blocked and not (self.blocked_reason and self.blocked_reason.strip()):
            raise ValueError(f"source {self.id}: blocked sources need a blocked_reason")


_REGISTRY: dict[str, SourceSpec] = {}


def declare_source(
    id: str,
    name: str,
    kind: str,
    base_url: str | None,
    cadence_minutes: int | None,
    terms_note: str,
    blocked: bool = False,
    blocked_reason: str | None = None,
) -> SourceSpec:
    """Register a source declaration (no DB write). Re-declaring identically is allowed."""
    spec = SourceSpec(id, name, kind, base_url, cadence_minutes, terms_note, blocked, blocked_reason)
    existing = _REGISTRY.get(id)
    if existing is not None and existing != spec:
        raise SourceError(f"source {id!r} is declared twice with different settings")
    _REGISTRY[id] = spec
    return spec


def registered_sources() -> list[SourceSpec]:
    return sorted(_REGISTRY.values(), key=lambda s: s.id)


def get_spec(source_id: str) -> SourceSpec:
    try:
        return _REGISTRY[source_id]
    except KeyError:
        raise UnknownSource(f"source {source_id!r} is not declared") from None


_UPSERT = """
INSERT INTO source (id, name, kind, base_url, cadence_minutes, terms_note, blocked, blocked_reason)
VALUES (%(id)s, %(name)s, %(kind)s, %(base_url)s, %(cadence_minutes)s, %(terms_note)s,
        %(blocked)s, %(blocked_reason)s)
ON CONFLICT (id) DO UPDATE SET
  name = EXCLUDED.name, kind = EXCLUDED.kind, base_url = EXCLUDED.base_url,
  cadence_minutes = EXCLUDED.cadence_minutes, terms_note = EXCLUDED.terms_note,
  blocked = EXCLUDED.blocked, blocked_reason = EXCLUDED.blocked_reason
RETURNING *
"""


def ensure_source(
    id: str,
    name: str,
    kind: str,
    base_url: str | None,
    cadence_minutes: int | None,
    terms_note: str,
    blocked: bool = False,
    blocked_reason: str | None = None,
    *,
    conn: Conn | None = None,
) -> Row:
    """Upsert a `source` row (declaration fields only; heartbeat columns are left alone)."""
    spec = declare_source(id, name, kind, base_url, cadence_minutes, terms_note, blocked, blocked_reason)
    return upsert_spec(spec, conn=conn)


def upsert_spec(spec: SourceSpec, *, conn: Conn | None = None) -> Row:
    with db.transaction(conn) as c:
        row = c.execute(_UPSERT, asdict(spec)).fetchone()
    assert row is not None
    return row


def sync_registered(*, conn: Conn | None = None) -> list[Row]:
    """Upsert every declared source."""
    with db.transaction(conn) as c:
        return [upsert_spec(spec, conn=c) for spec in registered_sources()]


def get_source(source_id: str, *, conn: Conn | None = None) -> Row | None:
    with db.transaction(conn) as c:
        return c.execute("SELECT * FROM source WHERE id = %s", (source_id,)).fetchone()


def require_collectable(source_id: str, *, conn: Conn | None = None) -> Row:
    """The source row, or UnknownSource / SourceBlocked. Call before collecting."""
    row = get_source(source_id, conn=conn)
    if row is None:
        raise UnknownSource(f"source {source_id!r} has no row; call ensure_source first")
    if row["blocked"]:
        raise SourceBlocked(f"source {source_id!r} is blocked: {row['blocked_reason']}")
    return row


def list_sources(*, conn: Conn | None = None) -> list[Row]:
    with db.transaction(conn) as c:
        return c.execute("SELECT * FROM source ORDER BY id").fetchall()
