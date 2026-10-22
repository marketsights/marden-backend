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


def via(sql: str, *, int_id: bool = False) -> KeyFn:
    """Keys from a query taking the subject id as its only parameter, `%s` or (to use it more than
    once) `%(id)s`. First column, nulls dropped. `int_id`: the id is an integer key (holding.id), passed
    as an integer so its index serves the lookup (`id::text = %s` scans the table on every write)."""
    named = "%(id)s" in sql

    def keys(conn: Conn, subject_id: str) -> list[str]:
        if int_id and not subject_id.isdigit():
            return []
        param = int(subject_id) if int_id else subject_id
        rows = conn.execute(sql, {"id": param} if named else (param,)).fetchall()
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
    """Mark every read model depending on this subject/field dirty. Returns the pairs marked.
    Models whose inputs include the value are marked too (marden.models.triggers, same transaction)."""
    with db.transaction(conn) as c:
        pairs = targets(subject_type, subject_id, field, conn=c)
        _upsert(c, pairs, reason)
        _mark_models(c, subject_type, subject_id, field, reason)
    return sorted(pairs)


def _mark_models(conn: Conn, subject_type: str, subject_id: str, field: str, reason: str | None) -> None:
    try:
        from marden.models.triggers import mark_models_for  # models depend on readmodels, not the reverse
    except ImportError:  # pragma: no cover - the models package is optional until it lands
        return
    mark_models_for(conn, subject_type, subject_id, field, reason)


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
_HOLDING_INSTRUMENT = via("SELECT instrument_id FROM holding WHERE id = %s", int_id=True)
_HOLDING_LINKED = via("SELECT 'all' FROM holding WHERE id = %s AND instrument_id IS NOT NULL", int_id=True)
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
    # Only observations ('value') of rate series: not licence facts such as 'notice' or
    # 'display_allowed', nor other series such as ERCOT's large-load MW (collectors/ercot).
    depends("series", "value", "curve", _rate_series)

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


# collectors ust_par_curve, nyfed_sofr, fred, etf_oas (the fund-OAS proxies for the ICE series)
RATE_SERIES_PREFIXES = ("ust_par_", "sofr", "ice_bofa_", "etf_oas_")


def _rate_series(conn: Conn, subject_id: str) -> list[str]:
    """Key 'all' for a rate series (the curve card); nothing for other series."""
    return ["all"] if subject_id.startswith(RATE_SERIES_PREFIXES) else []


def _many_field(subject_type: str, field_prefix: str, views: tuple[str, ...], keys: KeyFn) -> None:
    for view in views:
        depends(subject_type, field_prefix, view, keys)


_install_defaults()


# Instruments area (readmodels/views/instruments.py, instrument_detail.py, curve.py, gpu_collateral.py).
# An instrument's chain status is the 'status' model output on its site, so a site's status output
# ('@model:status', marden.models.status) refreshes the instruments list and the site's instruments.
# Outputs for subject 'all' (the 'curve' tiers, the 'gpu_deployment' page) may sit on subject_type 'series'.
_SITE_INSTRUMENTS = via("SELECT id FROM instrument WHERE site_slug = %s")
# An unsited instrument's chain status is its issuer's own link on the sites it is a chain party of.
_SITE_PARTY_INSTRUMENTS = via(
    """SELECT DISTINCT i.id FROM instrument i JOIN chain_party cp ON cp.entity_id = i.issuer_entity_id
        WHERE cp.site_slug = %s AND i.site_slug IS NULL"""
)
# A fund's newer filing that no longer lists an instrument ends the fund's holding of it (holder count,
# par and marks on instrument_detail), so a fund's new holding line refreshes every instrument it holds.
_FUND_HOLDING_INSTRUMENTS = via(
    """SELECT DISTINCT o.instrument_id FROM holding h JOIN holding o ON o.fund_entity_id = h.fund_entity_id
        WHERE h.id = %s AND o.instrument_id IS NOT NULL""",
    int_id=True,
)
_GPU_INSTRUMENTS_FOR_ALL = via(
    """SELECT DISTINCT subject_id FROM fact_current
        WHERE subject_type = 'instrument' AND field IN ('gpu_collateral', 'gpu_model') AND %s = 'all'"""
)


def _install_instrument_deps() -> None:
    depends("site", "@model:status", "instruments", singleton)
    depends("site", "@model:status", "instrument_detail", _SITE_INSTRUMENTS)
    depends("site", "@model:status", "instrument_detail", _SITE_PARTY_INSTRUMENTS)
    depends("holding", "", "instrument_detail", _FUND_HOLDING_INSTRUMENTS)
    depends("series", "@model", "curve", singleton)
    depends("series", "@model", "gpu_collateral", _GPU_INSTRUMENTS_FOR_ALL)


_install_instrument_deps()


# Sites area (readmodels/views/sites*.py). A site shows its approved campus links, or its proposed
# ones while none is approved (Buildings tab `linkApproved: false`), and the plants serving those
# campuses or naming the site in `serves_sites`; the debt tab lists the tenant's own V1 notes exposed
# to the site (instrument `exposure_links`); the opposition tab and project file read the docket
# entities whose `linked_sites` names the site; filed holders appear on the debt tab, the detail
# header and the project file.
_LINKED = "status IN ('approved', 'proposed')"
_CAMPUS_LINKED_SITES = via(f"SELECT site_slug FROM site_link WHERE campus_id = %s AND {_LINKED}")
_BUILDING_LINKED_SITES = via(
    f"""SELECT l.site_slug FROM building b JOIN site_link l ON l.campus_id = b.campus_id
        WHERE b.id = %s AND l.{_LINKED} AND (l.building_ids IS NULL OR b.id = ANY(l.building_ids))"""
)
_PLANT_LINKED_SITES = via(
    f"""SELECT l.site_slug FROM plant_serves ps JOIN site_link l ON l.campus_id = ps.campus_id
        WHERE ps.plant_id = %(id)s AND l.{_LINKED}
        UNION SELECT jsonb_array_elements_text(value) FROM fact
        WHERE subject_type = 'plant' AND subject_id = %(id)s AND field = 'serves_sites'
          AND status IN ('published', 'superseded') AND jsonb_typeof(value) = 'array'"""
)
_UNIT_LINKED_SITES = via(
    f"""SELECT l.site_slug FROM power_unit u JOIN plant_serves ps ON ps.plant_id = u.plant_id
          JOIN site_link l ON l.campus_id = ps.campus_id WHERE u.id = %(id)s AND l.{_LINKED}
        UNION SELECT jsonb_array_elements_text(f.value) FROM power_unit u JOIN fact f
          ON f.subject_type = 'plant' AND f.subject_id = u.plant_id AND f.field = 'serves_sites'
        WHERE u.id = %(id)s AND f.status IN ('published', 'superseded') AND jsonb_typeof(f.value) = 'array'"""
)
_INSTRUMENT_EXPOSED_SITES = via(
    """SELECT DISTINCT s.slug FROM fact f, jsonb_array_elements(
             CASE WHEN jsonb_typeof(f.value) = 'array' THEN f.value ELSE '[]' END) e
         JOIN site s ON s.legacy_group = e->>'case'
        WHERE f.subject_type = 'instrument' AND f.subject_id = %s AND f.field = 'exposure_links'
          AND f.status IN ('published', 'superseded') AND e->>'role' = 'Tenant'"""
)
_HOLDING_SITE = via(
    "SELECT i.site_slug FROM holding h JOIN instrument i ON i.id = h.instrument_id WHERE h.id = %s", int_id=True
)
_ENTITY_LINKED_SITES = via(
    """SELECT DISTINCT jsonb_array_elements_text(value) FROM fact
        WHERE subject_type = 'entity' AND subject_id = %s AND field = 'linked_sites'
          AND status IN ('published', 'superseded') AND jsonb_typeof(value) = 'array'"""
)


def _any_linked(sites: KeyFn) -> KeyFn:
    """Key 'all' when the subject is linked to at least one site."""

    def keys(conn: Conn, subject_id: str) -> list[str]:
        return ["all"] if list(sites(conn, subject_id)) else []

    return keys


def _install_site_deps() -> None:
    linked_views = ("site_detail", "site_tab:buildings", "site_tab:power", "project_file")
    _many("campus", linked_views, _CAMPUS_LINKED_SITES)
    _many("building", ("site_detail", "site_tab:buildings", "project_file"), _BUILDING_LINKED_SITES)
    _many("plant", ("site_tab:power", "site_tab:buildings", "project_file"), _PLANT_LINKED_SITES)
    _many("unit", ("site_tab:power", "site_tab:buildings", "project_file"), _UNIT_LINKED_SITES)
    _many("instrument", ("site_tab:debt",), _INSTRUMENT_EXPOSED_SITES)
    _many("holding", ("site_detail", "site_tab:debt", "project_file"), _HOLDING_SITE)
    _many("entity", ("site_tab:opposition", "project_file"), _ENTITY_LINKED_SITES)
    # The lists' "Bldgs" column (spec 16) counts the linked campuses' buildings and building facts.
    _many("campus", ("sites", "completion"), _any_linked(_CAMPUS_LINKED_SITES))
    _many("building", ("sites", "completion"), _any_linked(_BUILDING_LINKED_SITES))
    # Completion's and Home's 'Last evidence' (sites_evidence) read the linked campuses' and plants' events.
    _many("campus", ("home",), _any_linked(_CAMPUS_LINKED_SITES))
    _many("plant", ("completion", "home"), _any_linked(_PLANT_LINKED_SITES))
    # The Power and Buildings tabs embed the main plant's `plant_row` (built with its own builder), whose
    # financed link shows the site's chain status and debt: the site's instruments refresh them too.
    _many("instrument", ("site_tab:power", "site_tab:buildings"), _INSTRUMENT_SITE)


_install_site_deps()


# Facilities area (readmodels/views/facilities*.py). A plant's row and detail show the financed link of
# the campus it serves (that campus's approved site: its chain status and instruments), the fuel line
# serving the same campus, the served campus's building count and its air-permit docket's status; the
# plant debt card shows the Power purchase edges of the sites and campuses the plant serves.
_SITE_PLANTS = via(
    """SELECT ps.plant_id FROM site_link l JOIN plant_serves ps ON ps.campus_id = l.campus_id
        WHERE l.site_slug = %s AND l.status = 'approved'"""
)
_INSTRUMENT_PLANTS = via(
    """SELECT ps.plant_id FROM instrument i JOIN site_link l ON l.site_slug = i.site_slug
         JOIN plant_serves ps ON ps.campus_id = l.campus_id WHERE i.id = %s AND l.status = 'approved'"""
)
_BUILDING_PLANTS = via(
    "SELECT ps.plant_id FROM building b JOIN plant_serves ps ON ps.campus_id = b.campus_id WHERE b.id = %s"
)
_SIBLING_PLANTS = via(
    """SELECT DISTINCT o.plant_id FROM plant_serves ps JOIN plant_serves o ON o.campus_id = ps.campus_id
        WHERE ps.plant_id = %s AND o.plant_id <> ps.plant_id"""
)
_PERMIT_DOCKET_PLANTS = via(
    """SELECT DISTINCT f.subject_id FROM entity e JOIN fact f ON f.subject_type = 'plant' AND f.field = 'air_permit'
          AND f.status IN ('published', 'superseded') AND f.value ->> 'proceeding_key' = e.meta ->> 'proceeding_key'
        WHERE e.id = %s"""
)
_OBLIGATION_PLANTS = via(
    """SELECT ps.plant_id FROM obligation o JOIN plant_serves ps ON ps.campus_id = o.campus_id WHERE o.id = %(id)s
       UNION SELECT f.subject_id FROM obligation o JOIN fact f ON f.subject_type = 'plant' AND f.field = 'serves_sites'
          AND f.status IN ('published', 'superseded') AND jsonb_typeof(f.value) = 'array'
          AND f.value @> jsonb_build_array(o.site_slug)
        WHERE o.id = %(id)s"""
)


def _install_facility_deps() -> None:
    _many("site", ("plant_row", "plant_detail"), _SITE_PLANTS)
    _many("instrument", ("plant_row", "plant_detail"), _INSTRUMENT_PLANTS)
    _many("building", ("plant_row", "plant_detail"), _BUILDING_PLANTS)
    _many("plant", ("plant_row", "plant_detail"), _SIBLING_PLANTS)
    _many("entity", ("plant_row", "plant_detail"), _PERMIT_DOCKET_PLANTS)
    for subject_type in ("obligation", "contract"):
        _many(subject_type, ("plant_debt",), _OBLIGATION_PLANTS)


_install_facility_deps()


# Payers and obligations area: its rules live in deps_payers.py (kept under the 400-line limit), and the moves
# and event-effect views' in deps_moves.py; imported here so every writer that marks dirty sees them.
from marden.readmodels import deps_moves as _deps_moves  # noqa: E402,F401
from marden.readmodels import deps_payers as _deps_payers  # noqa: E402,F401
