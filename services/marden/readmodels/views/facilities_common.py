"""Shared reads for the Data centers and On-site power views (specs 14-17, 20, 27): campuses,
buildings, plants and units as Marden knew them on the build date.

- Identity rows (campus, power_plant) count once they existed by the end of the build date
  (`created_at`); facts come through `known_facts` / `known_fact_rows`, events, model outputs and
  site links are dated with `build_date()` (what Marden had learned by the end of that day).
- V1 lists US campuses only (spec 14): a campus outside the 50 states, DC and PR has no `dc_row`
  (so it is in no list, summary or export) but keeps its detail and tabs.
- A value nobody has sourced is n/d: `nd(tag)` for a Sourced<T>, `None` for a plain field.
- `financed` (FinancedLink) is the campus's approved site link only (spec 16: no link without an
  approved evidence record): the site's chain status from the `status` model and the debt of the
  instruments that need the site finished. Proposed links never show.
- `lastChange` (campus and plant rows) is the newest material dated change (`Change`): the History
  tab's entries (value changes, first values, first seen), records (events) and large-load queue
  listings on one scale, by date (valid_from / event date), then learned_at, then kind, field order
  and row id. None is a fallback, so a newly arrived older record never moves it back.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any, NamedTuple

from marden.core.db import Conn, Row
from marden.core.licensing import hidden_source_names
from marden.readmodels.context import known_fact_rows, known_facts
from marden.readmodels.views.sites_common import (
    US_STATES,
    Doc,
    chain_status,
    day,
    day_end,
    debt_value,
    event_evidence,
    fact_evidence,
    latest_output,
    nd,
    next_day,
    site_instruments,
    with_value,
)
from marden.readmodels.views.sites_text import text_of

DC_STATUSES = ("Planned", "Under construction", "Operating", "Paused", "Cancelled")
POWER_SOURCES = ("Grid", "On-site gas", "Hybrid", "Unknown")
PLANT_STATUSES = ("Permit pending", "Permitted", "Under construction", "Operating", "Withdrawn", "Denied")
PLANT_TECHS = ("Simple-cycle turbine", "Reciprocating engine", "Fuel cell", "Combined cycle", "Other")
ISO_CODES = re.compile(r"^\s*(ERCOT|PJM|MISO|SPP|CAISO|NYISO|ISO-NE)\b", re.I)
GRID_JOIN = "collector:grid_join"
QUEUE_PREFIX = "large_load_queue_"  # operator queue listings of a large load (marden.collectors.iso_queue)
PIPELINE_WORDS = ("pipeline", "lateral", "gas supply")  # as sites_power: fuel lines, not generation

FIELD_LABELS = {
    "status": "Status",
    "facility_mw": "Facility MW",
    "it_mw": "IT MW",
    "operating_mw": "Operating MW",
    "operating_it_mw": "Operating IT MW",
    "planned_it_mw": "Planned IT MW",
    "delivered_mw": "MW delivered",
    "grid_mw": "Grid MW",
    "buildings_operational": "Buildings operational",
    "building_count": "Buildings",
    "power_source": "Power source",
    "developer": "Developer",
    "tenant": "Tenant",
    "owner": "Owner",
    "online_year": "Online year",
    "online_year_basis": "Online year basis",
    "utility": "Utility",
    "iso": "ISO/RTO",
    "balancing_authority": "Balancing authority",
    "interconnection": "Interconnection",
    "address": "Address",
    "location_text": "Location",
    "acres": "Acres",
    "sqft_total": "Square feet",
    "sqft_note": "Square feet note",
    "capex_usd": "Capital cost (USD)",
    "chips": "Chips",
    "users": "Users",
    "chip_types": "Chip types",
    "chips_planned": "Chips planned",
    "h100_equivalents": "H100 equivalents",
    "project_name": "Project",
    "first_seen": "First seen",
    "epoch_status": "Epoch status note",
    "power_source_text": "Power (as stated)",
    "power_status": "Power status",
    "power_detail": "Power detail",
    "onsite_generation": "On-site generation",
    "schedule_text": "Schedule (as stated)",
    "original_schedule_text": "Original schedule (as stated)",
    "backup_generator_count": "Backup generators",
    "substation_count": "Substations",
    "utility_territories": "Utility territories",
    "capacity_market": "Capacity market",
    "capacity_zone": "Capacity zone",
    "ercot_load_zone": "ERCOT load zone",
    "copyright_notice": "Attribution",
    "capacity_mw": "Capacity MW (as stated)",
    "expected_cod": "Expected in service",
    "original_cod": "Originally planned in service",
    "operator": "Operator",
    "stage": "Stage",
    "technology": "Technology",
    "air_permit": "Air permit",
    "latest_change": "Latest change",
}


# ---------------------------------------------------------------- identity rows
def campus_ids(conn: Conn) -> list[str]:
    return [r["id"] for r in conn.execute("SELECT id FROM campus ORDER BY id")]


def plant_ids(conn: Conn) -> list[str]:
    return [r["id"] for r in conn.execute("SELECT id FROM power_plant ORDER BY id")]


def campus_row(conn: Conn, campus_id: str) -> Row | None:
    """The campus as known on the build date, with its point as lat / lon (None without a point)."""
    return conn.execute(
        """SELECT id, name, aliases, county, state, parcel_ids, first_seen_at, created_at,
                  ST_Y(geom) AS lat, ST_X(geom) AS lon FROM campus WHERE id = %s AND created_at < %s::date""",
        (campus_id, next_day()),
    ).fetchone()


def plant_row(conn: Conn, plant_id: str) -> Row | None:
    return conn.execute(
        """SELECT id, name, county, state, eia_plant_id, created_at, ST_Y(geom) AS lat, ST_X(geom) AS lon
             FROM power_plant WHERE id = %s AND created_at < %s::date""",
        (plant_id, next_day()),
    ).fetchone()


def is_us(state: str | None, facts: dict[str, Row]) -> bool:
    if state and state.strip().upper() in US_STATES:
        return True
    country = facts.get("country")
    return not state and bool(country) and country["value"] == "United States"


def campus_buildings(conn: Conn, campus_id: str) -> list[Row]:
    """The campus's building rows known on the build date, in label order (with GeoJSON footprints)."""
    rows = conn.execute(
        """SELECT id, campus_id, label, parcel_ids, ST_AsGeoJSON(footprint)::jsonb AS footprint, created_at
             FROM building WHERE campus_id = %s AND created_at < %s::date ORDER BY id""",
        (campus_id, next_day()),
    ).fetchall()
    return sorted(rows, key=lambda b: _label_order(b["label"] or b["id"]))


def _label_order(label: str) -> tuple[int, str]:
    digits = "".join(ch for ch in label if ch.isdigit())
    return (int(digits) if digits else 10**6, label)


# A plant_serves link counts once its evidence fact was known by the build date and is not from a source
# the licence switch hides.
_SERVES_EVIDENCE = """(ps.evidence_fact_id IS NULL OR (f.status IN ('published', 'superseded')
                      AND f.learned_at < %s::date AND f.source_name <> ALL(%s)))"""


def serving_plants(conn: Conn, campus_id: str) -> list[Row]:
    """Plants serving the campus (plant_serves) whose evidence Marden had by the build date."""
    return conn.execute(
        f"""SELECT p.* FROM plant_serves ps JOIN power_plant p ON p.id = ps.plant_id
             LEFT JOIN fact f ON f.id = ps.evidence_fact_id
            WHERE ps.campus_id = %s AND p.created_at < %s::date AND {_SERVES_EVIDENCE}
            ORDER BY p.id""",
        (campus_id, next_day(), next_day(), hidden_source_names()),
    ).fetchall()


def served_campuses(conn: Conn, plant_id: str) -> list[Row]:
    """Campuses the plant serves (plant_serves, evidence known by the build date), US campuses first."""
    rows = conn.execute(
        f"""SELECT c.id, c.name, c.state FROM plant_serves ps JOIN campus c ON c.id = ps.campus_id
             LEFT JOIN fact f ON f.id = ps.evidence_fact_id
            WHERE ps.plant_id = %s AND c.created_at < %s::date AND {_SERVES_EVIDENCE}
            ORDER BY c.id""",
        (plant_id, next_day(), next_day(), hidden_source_names()),
    ).fetchall()
    return sorted(rows, key=lambda c: (c["state"] not in US_STATES, c["id"]))


def is_fuel_line(plant: Row, facts: dict[str, Row]) -> bool:
    text = " ".join([plant.get("name") or "", str((facts.get("technology_type") or {}).get("value") or "")]).lower()
    return any(w in text for w in PIPELINE_WORDS)


# ---------------------------------------------------------------- Sourced values
def enum_value(row: Row | None, allowed: Sequence[str], tag: str = "F") -> Doc:
    """A fact whose value must be one of `allowed` (DcStatus, PlantStatus ...); anything else is n/d."""
    if row is not None and row["value"] in allowed:
        return with_value(row, row["value"])
    return nd(tag)


def text_value(row: Row | None, tag: str = "R") -> Doc:
    """A fact shown as text (strings as stated; dicts and lists as their display text)."""
    if row is None:
        return nd(tag)
    text = row["value"] if isinstance(row["value"], str) else text_of(row["value"])
    return with_value(row, text) if text else nd(tag)


def number_value(row: Row | None, tag: str = "R") -> Doc:
    if row is not None and isinstance(row["value"], int | float) and not isinstance(row["value"], bool):
        return with_value(row, row["value"])
    return nd(tag)


def grid_join_row(conn: Conn, subject_type: str, subject_id: str, field: str) -> Row | None:
    """The boundary join's value of a field (HIFLD territory / ISO / BA, spec 17), as known that day."""
    rows = known_fact_rows(conn, subject_type, subject_id, [field])[field]
    return next((r for r in rows if r.get("producer") == GRID_JOIN and r["valid_from"] <= day()), None)


def iso_value(conn: Conn, subject_type: str, subject_id: str, row: Row | None) -> Doc:
    """ISO/RTO as a code (ERCOT, PJM, MISO, SPP, CAISO, NYISO, ISO-NE): the current fact when it names
    one ('MISO (Zone 7)' -> 'MISO'), else the boundary join's value ('non-RTO'), else the fact as stated."""
    if row is not None and isinstance(row["value"], str) and (m := ISO_CODES.match(row["value"])):
        return with_value(row, m.group(1).upper())
    joined = grid_join_row(conn, subject_type, subject_id, "iso")
    if joined is not None and isinstance(joined["value"], str):
        return with_value(joined, joined["value"])
    return text_value(row, "F")


def field_label(field: str) -> str:
    return FIELD_LABELS.get(field) or field.replace("_", " ").capitalize()


# ---------------------------------------------------------------- financed link (spec 16)
def approved_sites(conn: Conn, campus_ids: Iterable[str]) -> list[Row]:
    return conn.execute(
        """SELECT DISTINCT s.slug, s.name FROM site_link l JOIN site s ON s.slug = l.site_slug
            WHERE l.campus_id = ANY(%s) AND l.status = 'approved'
              AND (l.approved_at IS NULL OR l.approved_at < %s) ORDER BY s.slug""",
        (list(campus_ids), day_end()),
    ).fetchall()


def financed_link(conn: Conn, campus_ids: Iterable[str]) -> Doc | None:
    """FinancedLink of the campus's approved financed site (the one with the most debt when several)."""
    best: tuple[float, Doc] | None = None
    for site in approved_sites(conn, campus_ids):
        debt = debt_value(site_instruments(conn, site["slug"]))
        doc = {
            "siteSlug": site["slug"],
            "siteName": site["name"],
            "chainStatus": chain_status(latest_output(conn, "status", "site", site["slug"])),
            "debtB": debt,
        }
        size = debt["value"] if isinstance(debt["value"], int | float) else -1.0
        if best is None or size > best[0]:
            best = (size, doc)
    return best[1] if best else None


# ---------------------------------------------------------------- events, history, last change
def subject_events(conn: Conn, subject_type: str, subject_id: str) -> list[Row]:
    """Published past-dated events on the subject that Marden knew by the build date, newest first."""
    return conn.execute(
        """SELECT * FROM event WHERE subject_type = %s AND subject_id = %s AND status = 'published'
             AND learned_at < %s::date AND event_date <= %s ORDER BY event_date DESC, id DESC""",
        (subject_type, subject_id, next_day(), day()),
    ).fetchall()


def _shown(value: Any) -> str | None:
    return text_of(value, 160)


class Change(NamedTuple):
    """A dated change on a subject: its DcHistoryEvent and what `last_change` ranks it by. Changes of
    the same date learned at the same instant are ordered by `kind` (FIRST_SEEN < FIRST_VALUE <
    VALUE_CHANGE < QUEUE < RECORD), then `order` (the field's place in its history list, 0 = most
    material), then `row_id` (the fact's or event's id), which makes the order total."""

    entry: Doc
    learned_at: datetime
    kind: int
    order: int
    row_id: int


FIRST_SEEN, FIRST_VALUE, VALUE_CHANGE, QUEUE, RECORD = range(5)


def history_entries(changes: Iterable[Change]) -> list[Doc]:
    """The History tab: DcHistoryEvent per change, newest date first (by field on the same date)."""
    return sorted((c.entry for c in changes), key=lambda h: (h["date"], h["field"]), reverse=True)


def fact_changes(
    conn: Conn, subject_type: str, subject_id: str, fields: Sequence[str], initial: Sequence[str] = ()
) -> list[Change]:
    """A change per new value of each field (before -> after), oldest value first per field; the
    first known value is a change too for the `initial` fields (n/d -> value)."""
    out: list[Change] = []
    rows = known_fact_rows(conn, subject_type, subject_id, list(fields))
    for field in fields:
        prev: str | None = None
        seen = False
        for row in sorted(
            (r for r in rows[field] if r["valid_from"] <= day()), key=lambda r: (r["valid_from"], r["id"])
        ):
            after = _shown(row["value"])
            if after is None or (seen and after == prev):
                continue
            if seen or field in initial:
                text = f"{field_label(field)}: {prev} -> {after}" if seen else f"{field_label(field)}: {after}"
                entry = {
                    "date": row["valid_from"].isoformat(),
                    "field": field,
                    "before": prev if seen else None,
                    "after": after,
                    "evidence": fact_evidence(row, text),
                }
                kind = VALUE_CHANGE if seen else FIRST_VALUE
                out.append(Change(entry, row["learned_at"], kind, fields.index(field), row["id"]))
            prev, seen = after, True
    return out


def event_changes(events: Sequence[Row]) -> list[Change]:
    """Each record (event: permit, docket, filing, queue status or COD change) as a change."""

    def entry(e: Row) -> Doc:
        return {"date": e["event_date"].isoformat(), "field": "record", "before": None, "after": e["title"]}

    return [Change({**entry(e), "evidence": event_evidence(e)}, e["learned_at"], RECORD, 0, e["id"]) for e in events]


def queue_text(field: str, value: Any) -> str | None:
    """A large-load queue listing as the operator states it: 'NYISO queue 1732 (SRIS/SIS Approved)'."""
    parts = []
    for e in value if isinstance(value, list) else [value]:
        if isinstance(e, dict) and e.get("queue_id"):
            status = f" ({e['status']})" if e.get("status") else ""
            parts.append(f"{field.rsplit('_', 1)[-1].upper()} queue {e['queue_id']}{status}")
    return "; ".join(parts) or None


def queue_changes(conn: Conn, subject_type: str, subject_id: str) -> list[Change]:
    """Each operator queue listing of the subject's large loads (`large_load_queue_<iso>`, written
    only when the entry changes), dated by the queue file it was read from."""
    fields = sorted(f for f in known_facts(conn, subject_type, subject_id) if f.startswith(QUEUE_PREFIX))
    if not fields:
        return []
    out = []
    for field, rows in known_fact_rows(conn, subject_type, subject_id, fields).items():
        for row in rows:
            if row["valid_from"] <= day() and (text := queue_text(field, row["value"])):
                entry = {
                    "date": row["valid_from"].isoformat(),
                    "field": field,
                    "before": None,
                    "after": text,
                    "evidence": fact_evidence(row, f"Large-load queue: {text}"),
                }
                out.append(Change(entry, row["learned_at"], QUEUE, 0, row["id"]))
    return out


def change_rank(c: Change) -> tuple[str, datetime, int, int, int]:
    """Newest first: date (valid_from / event date), learned_at, kind, field order, row id."""
    return (c.entry["date"], c.learned_at, c.kind, -c.order, c.row_id)


def last_change(changes: Iterable[Change], facts: dict[str, Row]) -> Doc | None:
    """`lastChange` (spec 14/15 "Latest fact"): the newest material dated change, by `change_rank`,
    over every kind on one scale: the History tab's entries (value changes, the first value of a
    tracked field: n/d -> value, the first-seen date), records (events) and operator queue listings.
    No kind is a fallback that a later arrival switches off, so new information never moves
    lastChange to an older date. With no change at all, the newest fact; None (n/d) without facts."""
    best = max(changes, key=change_rank, default=None)
    if best is not None:
        return best.entry["evidence"]
    dated = [r for r in facts.values() if r["valid_from"] <= day()]
    if dated:
        row = max(dated, key=lambda r: (r["valid_from"], r["id"]))
        return fact_evidence(row, f"{field_label(row['field'])}: {_shown(row['value']) or 'recorded'}")
    return None


def first_seen(facts: dict[str, Row], fallback: date | None) -> str | None:
    row = facts.get("first_seen")
    if row and isinstance(row["value"], str):
        return row["value"][:10]
    return fallback.isoformat() if fallback else None
