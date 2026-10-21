"""Which read models depend on which facts: (subject_type, field prefix) -> [(view, key function)].

`marden.core.facts` / `marden.core.records` call `mark_dirty_for` when a value is published; the
dirty (view, key) pairs are rebuilt by `rebuild_dirty` within ~30 s. Field prefix '' matches every
field, including the pseudo-fields '@price', '@event' and '@holding'. Later modules extend the
table with `depends(...)`; marking a view that has no builder yet is harmless (it waits).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from marden.core import db
from marden.core.db import Conn

KeyFn = Callable[[Conn, str], Iterable[str]]
Pair = tuple[str, str]


def same(conn: Conn, subject_id: str) -> list[str]:
    """Key = the subject id (site slug, campus id, instrument id, ...)."""
    return [subject_id]


def singleton(conn: Conn, subject_id: str) -> list[str]:
    """Key 'all' (views that are one document: sites, completion, home ...)."""
    return ["all"]


def via(sql: str) -> KeyFn:
    """Keys from a query taking the subject id as its only parameter (first column, nulls dropped)."""

    def keys(conn: Conn, subject_id: str) -> list[str]:
        rows = conn.execute(sql, (subject_id,)).fetchall()
        return [str(v) for r in rows if (v := next(iter(r.values()))) is not None]

    keys.__name__ = f"via({sql!r})"
    return keys


@dataclass(frozen=True)
class Dep:
    subject_type: str
    field_prefix: str
    view: str
    keys: KeyFn


_DEPS: list[Dep] = []


def depends(subject_type: str, field_prefix: str, view: str, keys: KeyFn = same) -> None:
    dep = Dep(subject_type, field_prefix, view, keys)
    if dep not in _DEPS:
        _DEPS.append(dep)


def dependencies() -> list[Dep]:
    return list(_DEPS)


def targets(subject_type: str, subject_id: str, field: str, *, conn: Conn) -> set[Pair]:
    out: set[Pair] = set()
    for dep in _DEPS:
        if dep.subject_type == subject_type and field.startswith(dep.field_prefix):
            out.update((dep.view, key) for key in dep.keys(conn, subject_id))
    return out


def _upsert(conn: Conn, pairs: Iterable[Pair], reason: str | None) -> None:
    ordered = sorted(set(pairs))  # one lock order across writers avoids deadlocks
    if not ordered:
        return
    conn.execute(
        """INSERT INTO read_model_dirty (view, key, marked_at, reason)
           SELECT v, k, clock_timestamp(), %s FROM unnest(%s::text[], %s::text[]) AS t(v, k)
           ON CONFLICT (view, key) DO UPDATE SET marked_at = EXCLUDED.marked_at, reason = EXCLUDED.reason""",
        (reason, [p[0] for p in ordered], [p[1] for p in ordered]),
    )


def mark_dirty(view: str, key: str, *, conn: Conn | None = None, reason: str | None = None) -> None:
    with db.transaction(conn) as c:
        _upsert(c, [(view, key)], reason)


def mark_dirty_for(
    subject_type: str,
    subject_id: str,
    field: str,
    *,
    conn: Conn | None = None,
    reason: str | None = None,
) -> list[Pair]:
    """Mark every read model depending on this subject/field dirty. Returns the pairs marked."""
    with db.transaction(conn) as c:
        pairs = targets(subject_type, subject_id, field, conn=c)
        _upsert(c, pairs, reason)
    return sorted(pairs)


# ---------------------------------------------------------------- starter table
SITE_TABS = ("evidence", "power", "opposition", "events", "debt", "lease", "buildings")
_INSTRUMENT_SITE = via("SELECT site_slug FROM instrument WHERE id = %s")
_CAMPUS_SITES = via("SELECT site_slug FROM site_link WHERE campus_id = %s AND status = 'approved'")
_BUILDING_CAMPUS = via("SELECT campus_id FROM building WHERE id = %s")
_BUILDING_SITES = via(
    """SELECT l.site_slug FROM building b JOIN site_link l ON l.campus_id = b.campus_id
       WHERE b.id = %s AND l.status = 'approved'
         AND (l.building_ids IS NULL OR b.id = ANY(l.building_ids))"""
)
_PLANT_CAMPUSES = via("SELECT campus_id FROM plant_serves WHERE plant_id = %s")
_UNIT_PLANT = via("SELECT plant_id FROM power_unit WHERE id = %s")
_HOLDING_INSTRUMENT = via("SELECT instrument_id FROM holding WHERE id::text = %s")
_HOLDING_LINKED = via("SELECT 'all' FROM holding WHERE id::text = %s AND instrument_id IS NOT NULL")
_OBLIGATION_PAYER = via("SELECT from_entity_id FROM obligation WHERE id = %s")


def _install_defaults() -> None:
    for view in ("site_detail", "site_schedule", "project_file", *(f"site_tab:{t}" for t in SITE_TABS)):
        depends("site", "", view)
    for view in ("sites", "completion", "home"):
        depends("site", "", view, singleton)

    depends("instrument", "", "instrument_detail")
    for view in ("instruments", "home"):
        depends("instrument", "", view, singleton)
    for view in ("site_detail", "site_tab:debt"):
        depends("instrument", "", view, _INSTRUMENT_SITE)
    for view in ("sites", "completion"):
        depends("instrument", "", view, singleton)

    depends("campus", "", "dc_row")
    depends("campus", "", "dc_detail")
    for view in ("site_detail", "site_tab:buildings"):
        depends("campus", "", view, _CAMPUS_SITES)
    for view in ("dc_row", "dc_detail"):
        depends("building", "", view, _BUILDING_CAMPUS)
    depends("building", "", "site_tab:buildings", _BUILDING_SITES)

    depends("plant", "", "plant_row")
    depends("plant", "", "plant_detail")
    for view in ("dc_row", "dc_detail"):
        depends("plant", "", view, _PLANT_CAMPUSES)
    for view in ("plant_row", "plant_detail"):
        depends("unit", "", view, _UNIT_PLANT)

    depends("entity", "", "payer")
    depends("entity", "", "obligations", singleton)

    depends("holding", "", "instrument_detail", _HOLDING_INSTRUMENT)
    depends("holding", "", "instruments", _HOLDING_LINKED)

    for view in ("obligations", "leases"):
        depends("obligation", "", view, singleton)
        depends("contract", "", view, singleton)
    depends("obligation", "", "payer", _OBLIGATION_PAYER)


_install_defaults()
