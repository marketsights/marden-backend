"""Which read models depend on which facts: (subject_type, field prefix) -> [(view, key function)].

`marden.core.facts` / `marden.core.records` call `mark_dirty_for` when a value is published; the
dirty (view, key) pairs are rebuilt by `rebuild_dirty` within ~30 s. Field prefix '' matches every
field, including the pseudo-fields '@price', '@event' and '@holding'. Later modules extend the
table with `depends(...)`; marking a view that has no builder yet is harmless (it waits). Views
must be in `marden.readmodels.views.VIEWS` (the contract with the API).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from marden.core import db
from marden.core.db import Conn
from marden.readmodels.views import CAMPUS_TABS, SITE_TABS, check_view

KeyFn = Callable[[Conn, str], Iterable[str]]
Pair = tuple[str, str]


def same(conn: Conn, subject_id: str) -> list[str]:
    """Key = the subject id (site slug, campus id, instrument id, ...)."""
    return [subject_id]


def singleton(conn: Conn, subject_id: str) -> list[str]:
    """Key 'all' (views that are one document: sites, completion, home ...)."""
    return ["all"]


def via(sql: str) -> KeyFn:
    """Keys from a query taking the subject id as its only parameter, `%s` or (to use it more than
    once) `%(id)s`. First column, nulls dropped."""
    named = "%(id)s" in sql

    def keys(conn: Conn, subject_id: str) -> list[str]:
        rows = conn.execute(sql, {"id": subject_id} if named else (subject_id,)).fetchall()
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
    dep = Dep(subject_type, field_prefix, check_view(view), keys)
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
    check_view(view)
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


# ---------------------------------------------------------------- default table
# Which screens show a subject's values. Views rebuilt by their own task are in views.SCHEDULED_VIEWS.
_INSTRUMENT_SITE = via("SELECT site_slug FROM instrument WHERE id = %s")
_INSTRUMENT_CAMPUSES = via(
    """SELECT l.campus_id FROM instrument i JOIN site_link l ON l.site_slug = i.site_slug
       WHERE i.id = %s AND l.status = 'approved'"""
)
_INSTRUMENT_PAYERS = via(
    """SELECT cp.entity_id FROM instrument i JOIN chain_party cp ON cp.site_slug = i.site_slug
       WHERE i.id = %s AND cp.entity_id IS NOT NULL"""
)
_INSTRUMENT_COMPANIES = via(
    """SELECT from_entity_id FROM obligation WHERE %(id)s = ANY(instrument_ids)
       UNION SELECT to_entity_id FROM obligation WHERE %(id)s = ANY(instrument_ids)"""
)
_SITE_CAMPUSES = via("SELECT campus_id FROM site_link WHERE site_slug = %s AND status = 'approved'")
_SITE_PAYERS = via("SELECT entity_id FROM chain_party WHERE site_slug = %s AND entity_id IS NOT NULL")
_CAMPUS_SITES = via("SELECT site_slug FROM site_link WHERE campus_id = %s AND status = 'approved'")
_CAMPUS_PLANTS = via("SELECT plant_id FROM plant_serves WHERE campus_id = %s")
_BUILDING_CAMPUS = via("SELECT campus_id FROM building WHERE id = %s")
_BUILDING_SITES = via(
    """SELECT l.site_slug FROM building b JOIN site_link l ON l.campus_id = b.campus_id
       WHERE b.id = %s AND l.status = 'approved'
         AND (l.building_ids IS NULL OR b.id = ANY(l.building_ids))"""
)
_PLANT_CAMPUSES = via("SELECT campus_id FROM plant_serves WHERE plant_id = %s")
_PLANT_SITES = via(
    """SELECT l.site_slug FROM plant_serves ps JOIN site_link l ON l.campus_id = ps.campus_id
       WHERE ps.plant_id = %s AND l.status = 'approved'"""
)
_UNIT_PLANT = via("SELECT plant_id FROM power_unit WHERE id = %s")
_ENTITY_SITES = via("SELECT site_slug FROM chain_party WHERE entity_id = %s")
_HOLDING_INSTRUMENT = via("SELECT instrument_id FROM holding WHERE id::text = %s")
_HOLDING_LINKED = via("SELECT 'all' FROM holding WHERE id::text = %s AND instrument_id IS NOT NULL")
_FUND_INSTRUMENTS = via("SELECT DISTINCT instrument_id FROM holding WHERE fund_entity_id = %s")
_OBLIGATION_PAYER = via("SELECT from_entity_id FROM obligation WHERE id = %s")
_OBLIGATION_COMPANIES = via(
    """SELECT from_entity_id FROM obligation WHERE id = %(id)s
       UNION SELECT to_entity_id FROM obligation WHERE id = %(id)s"""
)


def _many(subject_type: str, views: tuple[str, ...], keys: KeyFn = same) -> None:
    for view in views:
        depends(subject_type, "", view, keys)


SITE_PAGE = ("site_detail", "site_schedule", "project_file", *(f"site_tab:{t}" for t in SITE_TABS))
CAMPUS_PAGE = ("dc_row", "dc_detail", *(f"dc_tab:{t}" for t in CAMPUS_TABS))
PLANT_PAGE = ("plant_row", "plant_detail", "plant_debt")


def _install_defaults() -> None:
    _many("site", SITE_PAGE)
    _many("site", ("sites", "completion", "home", "payers", "leases"), singleton)
    _many("site", ("dc_row", "dc_detail"), _SITE_CAMPUSES)  # the campus row's financed site
    _many("site", ("payer", "payer_capacity"), _SITE_PAYERS)

    _many("instrument", ("instrument_detail", "gpu_collateral"))
    _many("instrument", ("instruments", "curve", "home", "sites", "completion", "payers", "obligations"), singleton)
    _many("instrument", ("site_detail", "site_tab:debt", "project_file"), _INSTRUMENT_SITE)
    _many("instrument", ("dc_row", "dc_detail"), _INSTRUMENT_CAMPUSES)
    _many("instrument", ("payer", "payer_capacity"), _INSTRUMENT_PAYERS)
    _many("instrument", ("obligation_company",), _INSTRUMENT_COMPANIES)

    _many("campus", CAMPUS_PAGE)
    _many("campus", ("gpu_deployment", "leases"), singleton)
    _many("campus", ("site_detail", "site_tab:buildings", "site_tab:power"), _CAMPUS_SITES)
    _many("campus", ("plant_row", "plant_detail"), _CAMPUS_PLANTS)  # plants list the campus they serve
    _many("building", ("dc_row", "dc_detail", "dc_tab:buildings", "dc_tab:capacity"), _BUILDING_CAMPUS)
    _many("building", ("site_tab:buildings",), _BUILDING_SITES)

    _many("plant", PLANT_PAGE)
    _many("plant", ("dc_row", "dc_detail"), _PLANT_CAMPUSES)
    _many("plant", ("site_tab:power",), _PLANT_SITES)
    _many("unit", ("plant_row", "plant_detail", "plant_debt"), _UNIT_PLANT)

    _many("entity", ("payer", "payer_capacity", "obligation_company"))
    _many("entity", ("payers", "obligations"), singleton)
    _many("entity", ("site_detail",), _ENTITY_SITES)  # chain parties (ratings, funding)

    _many("holding", ("instrument_detail",), _HOLDING_INSTRUMENT)
    _many("holding", ("instruments",), _HOLDING_LINKED)

    for subject_type in ("obligation", "contract"):
        _many(subject_type, ("obligations", "leases", "payers"), singleton)
        _many(subject_type, ("payer", "payer_capacity"), _OBLIGATION_PAYER)
        _many(subject_type, ("obligation_company",), _OBLIGATION_COMPANIES)

    # Rate series (UST par curve, SOFR, spread indexes): tier-curve yields sit on the Treasury curve.
    # Only observations ('value'), not licence facts such as 'notice' or 'display_allowed'.
    depends("series", "value", "curve", singleton)

    # A fund's net assets (collectors/sec_nport) set the NAV share and NAV hit of its holder rows.
    depends("entity", "net_assets_usd", "instrument_detail", _FUND_INSTRUMENTS)

    # Company construction in progress vs PP&E (collectors/sec_xbrl) is the GPU deployment page's
    # companies chart (spec 19); the payer views already depend on every entity fact.
    for prefix in ("construction_in_progress", "ppe_"):
        depends("entity", prefix, "gpu_deployment", singleton)

    # Interconnection queue entries (collectors/iso_queue): a link to one of our campuses, sites or
    # plants changes that subject's power section. Status changes reach them as events and, for large
    # loads, as `large_load_queue_<iso>` facts on the subject itself.
    for kind, views in (("campus", ("dc_row", "dc_detail")), ("site", ("site_detail", "site_tab:power")),
                        ("plant", ("plant_row", "plant_detail"))):
        _many_field("queue_project", f"linked_{kind}", views, _queue_links(kind))


def _queue_links(kind: str) -> KeyFn:
    """Ids a queue entry is or was linked to (every published linked_<kind> list, so an unlink also
    refreshes the subject it left)."""
    return via(
        f"""SELECT DISTINCT l->>'id' FROM fact f,
                  jsonb_array_elements(CASE WHEN jsonb_typeof(f.value) = 'array' THEN f.value ELSE '[]' END) l
             WHERE f.subject_type = 'queue_project' AND f.subject_id = %s AND f.field = 'linked_{kind}'
               AND f.status = 'published'"""
    )


def _many_field(subject_type: str, field_prefix: str, views: tuple[str, ...], keys: KeyFn) -> None:
    for view in views:
        depends(subject_type, field_prefix, view, keys)


_install_defaults()
