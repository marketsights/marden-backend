"""Writes `grid_region` rows (db/migrations/20261021150000_grid_region.sql), shared by the grid collectors.

Rows are identity: id, kind, name, country, parent and the published boundary. `meta` holds one key per
contributing source ('eia930', 'eia861', 'hifld', 'registry', 'boundary', 'coverage') so each source
replaces only its own part on a rerun. A boundary is stored as published (made valid, 2-D, MultiPolygon,
EPSG:4326); the raw file it came from stays in the raw store, and every value derived from it is a fact
whose `document_id` points at that file.

Upsert rules (safe reruns): a row whose values did not change is not touched (`updated_at` stays);
`prefer_existing=True` keeps an existing row's name, country and parent (sources that only add details
to a row another source owns, such as the EIA-861 crosswalk adding ids to an EIA-930 balancing authority).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from marden.core.db import Conn, Row
from marden.core.jsonutil import jsonb

KINDS = frozenset({"interconnection", "iso_rto", "balancing_authority", "utility", "tso", "reliability_region"})

# A boundary as stored: valid, two-dimensional, polygons only, MultiPolygon in EPSG:4326.
_GEOM = (
    "ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Force2D(ST_Transform("
    "ST_SetSRID(ST_GeomFromWKB(%(wkb)s), %(srid)s), 4326))), 3))"
)

_UPSERT = f"""
INSERT INTO grid_region AS g (id, kind, name, country, parent_id, geom, meta)
VALUES (%(id)s, %(kind)s, %(name)s, %(country)s, %(parent_id)s,
        CASE WHEN %(wkb)s::bytea IS NULL THEN NULL ELSE {_GEOM} END, %(meta)s)
ON CONFLICT (id) DO UPDATE SET
  kind = EXCLUDED.kind,
  name = CASE WHEN %(prefer_existing)s THEN g.name ELSE EXCLUDED.name END,
  country = CASE WHEN %(prefer_existing)s THEN coalesce(g.country, EXCLUDED.country)
                 ELSE coalesce(EXCLUDED.country, g.country) END,
  parent_id = CASE WHEN %(prefer_existing)s THEN coalesce(g.parent_id, EXCLUDED.parent_id)
                   ELSE coalesce(EXCLUDED.parent_id, g.parent_id) END,
  geom = coalesce(EXCLUDED.geom, g.geom),
  meta = g.meta || EXCLUDED.meta,
  updated_at = now()
WHERE g.kind IS DISTINCT FROM EXCLUDED.kind
   OR (NOT %(prefer_existing)s AND g.name IS DISTINCT FROM EXCLUDED.name)
   OR (EXCLUDED.country IS NOT NULL AND g.country IS DISTINCT FROM EXCLUDED.country
       AND NOT (%(prefer_existing)s AND g.country IS NOT NULL))
   OR (EXCLUDED.parent_id IS NOT NULL AND g.parent_id IS DISTINCT FROM EXCLUDED.parent_id
       AND NOT (%(prefer_existing)s AND g.parent_id IS NOT NULL))
   OR (EXCLUDED.geom IS NOT NULL AND (g.geom IS NULL OR NOT ST_Equals(g.geom, EXCLUDED.geom)))
   OR EXISTS (SELECT 1 FROM jsonb_each(EXCLUDED.meta) e WHERE g.meta -> e.key IS DISTINCT FROM e.value)
RETURNING (xmax = 0) AS inserted
"""


@dataclass(frozen=True)
class Region:
    id: str
    kind: str
    name: str
    country: str | None = None
    parent_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"grid_region kind must be one of {sorted(KINDS)}: {self.kind!r}")
        if not self.id or ":" not in self.id:
            raise ValueError(f"grid_region id must look like '<kind prefix>:<key>': {self.id!r}")
        if not self.name.strip():
            raise ValueError(f"grid_region {self.id}: name is required")


@dataclass
class UpsertStats:
    inserted: int = 0
    changed: int = 0
    unchanged: int = 0

    def add(self, outcome: str) -> None:
        setattr(self, outcome, getattr(self, outcome) + 1)

    def as_dict(self) -> dict[str, int]:
        return {"inserted": self.inserted, "changed": self.changed, "unchanged": self.unchanged}


def upsert(
    conn: Conn,
    region: Region,
    *,
    wkb: bytes | None = None,
    srid: int = 4326,
    prefer_existing: bool = False,
) -> str:
    """Insert or update one row. Returns 'inserted', 'changed' or 'unchanged'.

    `meta` is merged at the top level (each source writes its own key); `wkb` replaces the boundary
    (any SRID; transformed to EPSG:4326). Without `wkb` an existing boundary is kept.
    """
    row = conn.execute(
        _UPSERT,
        {
            "id": region.id, "kind": region.kind, "name": region.name.strip(), "country": region.country,
            "parent_id": region.parent_id, "wkb": wkb, "srid": srid, "meta": jsonb(region.meta),
            "prefer_existing": prefer_existing,
        },
    ).fetchone()
    if row is None:
        return "unchanged"
    return "inserted" if row["inserted"] else "changed"


def get(conn: Conn, region_id: str) -> Row | None:
    return conn.execute(
        "SELECT id, kind, name, country, parent_id, meta, geom IS NOT NULL AS has_geom FROM grid_region WHERE id = %s",
        (region_id,),
    ).fetchone()


def rows_of_kind(conn: Conn, kind: str) -> list[Row]:
    return conn.execute(
        """SELECT id, kind, name, country, parent_id, meta, geom IS NOT NULL AS has_geom
           FROM grid_region WHERE kind = %s ORDER BY id""",
        (kind,),
    ).fetchall()


def counts(conn: Conn) -> dict[str, dict[str, int]]:
    """{kind: {'rows': n, 'with_boundary': m}}."""
    rows = conn.execute(
        """SELECT kind, count(*) AS n, count(geom) AS g FROM grid_region GROUP BY kind ORDER BY kind"""
    ).fetchall()
    return {r["kind"]: {"rows": r["n"], "with_boundary": r["g"]} for r in rows}


# ---------------------------------------------------------------- names
_SUFFIXES = {"INC", "LLC", "CO", "CORP", "CORPORATION", "COMPANY", "THE", "OF", "AND", "LTD", "LP", "L", "P"}


def name_key(name: str | None) -> str:
    """A loose key for matching the same operator across EIA and HIFLD spellings
    ('Duke Energy Florida, Inc.' == 'DUKE ENERGY FLORIDA INC')."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().upper().replace("&", " AND ")
    words = [w for w in re.split(r"[^A-Z0-9]+", s) if w and w not in _SUFFIXES]
    return " ".join(words)


def slug(text: str) -> str:
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower() or "unnamed"
