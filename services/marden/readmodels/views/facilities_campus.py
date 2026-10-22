"""Data centers (spec 14, 16, 17): `dc_row` (types.ts DataCenterRow, one per US campus; the API
filters, sorts, pages and summarises these docs) and `dc_detail` (DataCenterDetail).

Row values are the campus facts as known on the build date: developer, tenant (the lease tenant,
else the compute users Epoch names), status (DcStatus), facility / IT MW, power source, utility,
ISO/RTO (as a code; the boundary join when the stated value names none), online year. Buildings
built / total count the campus's building records (Operating = built); a campus with no building
record yet shows the building counts its own facts state. `plantId` is the largest generating plant
serving the campus (fuel lines excluded). `financed` is the approved site link with the most debt and
`financedSites` every approved financed site on the campus (facilities_common).
`lastChange` is the newest dated change (facilities_common.last_change): a History-tab change (value
change, first status / MW, first seen), a record (event) or a large-load queue listing, by date, then
learned_at; else the newest fact. Detail adds every displayed fact with its source, owner of record
(assessor; n/d until a county collector writes `owner_of_record`), parcels, balancing authority, the
large-load request and queue entry, backup generators counted on the campus (MW and fuel n/d until
the air permit is read), key dates, Sentinel-2 imagery and the tab counts.
"""

from __future__ import annotations

from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.context import known_fact_rows, known_facts
from marden.readmodels.registry import builder
from marden.readmodels.views.facilities_campus_tabs import (
    building_docs,
    campus_changes,
    campus_history,
    campus_records,
    request_text,
)
from marden.readmodels.views.facilities_common import (
    DC_STATUSES,
    POWER_SOURCES,
    QUEUE_PREFIX,
    campus_buildings,
    campus_ids,
    campus_row,
    enum_value,
    event_changes,
    field_label,
    financed_sites,
    first_seen,
    is_fuel_line,
    is_us,
    iso_value,
    last_change,
    number_value,
    queue_changes,
    queue_text,
    serving_plants,
    subject_events,
    text_value,
)
from marden.readmodels.views.sites_common import Doc, day, nd, with_value
from marden.readmodels.views.sites_text import text_of

ROW_FIELDS = (
    "country",
    "county",
    "developer",
    "tenant",
    "users",
    "status",
    "facility_mw",
    "it_mw",
    "building_count",
    "buildings_operational",
    "power_source",
    "utility",
    "iso",
    "online_year",
    "online_year_basis",
    "first_seen",
)
USER_CONFIDENCE = ("confident", "likely")
# Shown elsewhere on the page, or not a campus value (imagery, links, attribution of other rows).
NOT_IN_FACTS = frozenset(
    {
        "imagery",
        "change_score",
        "site_link_evidence",
        "materiality_note",
        "large_load_request",
        "capacity_plan",
        "county",
        "country",
        "state",
        "imagery_note",
        "grid_project",
    }
)


def _county(campus: Row, facts: dict[str, Row]) -> str | None:
    if campus.get("county"):
        return campus["county"]
    row = facts.get("county")
    return row["value"].get("name") if row and isinstance(row["value"], dict) else None


def _tenant(facts: dict[str, Row]) -> Doc:
    """Tenant / end user (spec 14): the lease tenant as stated, else the compute users Epoch names."""
    if facts.get("tenant"):
        return text_value(facts["tenant"])
    row = facts.get("users")
    names = [
        u["name"]
        for u in (row["value"] if row and isinstance(row["value"], list) else [])
        if isinstance(u, dict) and u.get("name") and u.get("confidence", "confident") in USER_CONFIDENCE
    ]
    return with_value(row, ", ".join(names)) if row and names else nd("R")


def _count(row: Row | None) -> int | None:
    value = row["value"] if row else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def building_counts(conn: Conn, campus_id: str, facts: dict[str, Row]) -> dict[str, int]:
    rows = campus_buildings(conn, campus_id)
    if rows:
        built = sum(
            1
            for b in rows
            if (s := known_facts(conn, "building", b["id"], ["status"]).get("status")) and s["value"] == "Operating"
        )
        return {"built": built, "total": len(rows)}
    total = _count(facts.get("building_count")) or 0
    built = _count(facts.get("buildings_operational")) or 0
    return {"built": min(built, total) if total else built, "total": max(total, built)}


def main_plant_id(conn: Conn, campus_id: str) -> str | None:
    best: tuple[float, str] | None = None
    for plant in serving_plants(conn, campus_id):
        pf = known_facts(conn, "plant", plant["id"], ["capacity_mw", "technology_type"])
        if is_fuel_line(plant, pf):
            continue
        mw = (pf.get("capacity_mw") or {}).get("value")
        size = float(mw) if isinstance(mw, int | float) else -1.0
        if best is None or (size, plant["id"]) > best:
            best = (size, plant["id"])
    return best[1] if best else None


def row_doc(conn: Conn, campus: Row, facts: dict[str, Row]) -> Doc:
    cid = campus["id"]
    changes = campus_changes(conn, cid) + event_changes(subject_events(conn, "campus", cid))
    changes += queue_changes(conn, "campus", cid)
    financed = financed_sites(conn, [cid])
    return {
        "id": cid,
        "name": campus["name"],
        "aliases": list(campus.get("aliases") or []),
        "county": _county(campus, facts),
        "state": campus["state"],
        "lat": campus["lat"],
        "lon": campus["lon"],
        "developer": text_value(facts.get("developer")),
        "tenant": _tenant(facts),
        "status": enum_value(facts.get("status"), DC_STATUSES),
        "facilityMw": number_value(facts.get("facility_mw"), "F"),
        "itMw": number_value(facts.get("it_mw"), "F"),
        "buildings": building_counts(conn, cid, facts),
        "powerSource": enum_value(facts.get("power_source"), POWER_SOURCES, "M"),
        "utility": text_value(facts.get("utility"), "F"),
        "iso": iso_value(conn, "campus", cid, facts.get("iso")),
        "plantId": main_plant_id(conn, cid),
        "onlineYear": number_value(facts.get("online_year"), "F"),
        "financed": financed[0] if financed else None,  # the primary site (most debt)
        "financedSites": financed,
        "lastChange": last_change(changes, facts),
        "firstSeen": first_seen(facts, campus["first_seen_at"].date() if campus.get("first_seen_at") else None),
    }


def _campus(conn: Conn, campus_id: str, *, us_only: bool = False) -> tuple[Row, dict[str, Row]] | None:
    campus = campus_row(conn, campus_id)
    if campus is None:
        return None
    facts = known_facts(conn, "campus", campus_id, ROW_FIELDS)
    return (campus, facts) if not us_only or is_us(campus["state"], facts) else None


@builder("dc_row", keys=campus_ids)
def build_dc_row(conn: Conn, campus_id: str) -> Doc | None:
    """US campuses only: the rows are the Data centers list (spec 14). Detail and tabs exist for every
    campus Marden tracks, so search results and links outside the US still open."""
    found = _campus(conn, campus_id, us_only=True)
    return row_doc(conn, *found) if found else None


# ---------------------------------------------------------------- detail
def facts_table(facts: dict[str, Row]) -> dict[str, Doc]:
    """Every campus value with its tag, source and as-of (Overview facts table), by label."""
    out: dict[str, Doc] = {}
    for field in sorted(facts, key=field_label):
        row = facts[field]
        if field in NOT_IN_FACTS or field.startswith(QUEUE_PREFIX) or row["valid_from"] > day():
            continue
        value: Any = row["value"]
        if not (isinstance(value, int | float) and not isinstance(value, bool)):
            value = value if isinstance(value, str) else text_of(value)
        if value is not None and value != "":
            out[field_label(field)] = with_value(row, value)
    return out


def large_load(conn: Conn, campus_id: str, facts: dict[str, Row]) -> Doc | None:
    rows = known_fact_rows(conn, "campus", campus_id, ["large_load_request"])["large_load_request"]
    rows = [r for r in rows if r["valid_from"] <= day() and isinstance(r["value"], dict)]
    queue = queue_entry(facts)
    if not rows and queue is None:
        return None
    doc: Doc = {"request": with_value(rows[0], request_text(rows[0]["value"])) if rows else nd("F")}
    if queue is not None:
        doc["queuePosition"] = queue
    return doc


def queue_entry(facts: dict[str, Row]) -> Doc | None:
    """The campus's large-load queue entry as the operator lists it (large_load_queue_<iso> facts)."""
    for field in sorted(f for f in facts if f.startswith(QUEUE_PREFIX)):
        if text := queue_text(field, facts[field]["value"]):
            return with_value(facts[field], text)
    return None


def first_operational(conn: Conn, campus_id: str) -> Row | None:
    """The first record of a building in service on the campus: the earliest dated count of
    operational buildings above zero (`buildings_operational`, e.g. Epoch AI's imagery series, 0 -> 1)."""
    rows = known_fact_rows(conn, "campus", campus_id, ["buildings_operational"])["buildings_operational"]
    live = [
        r
        for r in rows
        if r["valid_from"] <= day()
        and isinstance(r["value"], int | float)
        and not isinstance(r["value"], bool)
        and r["value"] > 0
    ]
    return min(live, key=lambda r: (r["valid_from"], r["id"])) if live else None


def key_dates(conn: Conn, campus_id: str, facts: dict[str, Row]) -> list[Doc]:
    """Overview key dates (spec 14): first seen, construction start, first building complete, first
    building operational, online (first or expected), each with the record that dates it."""
    out = []
    if (row := facts.get("first_seen")) and isinstance(row["value"], str):
        out.append({"label": "First seen", "date": with_value(row, row["value"][:10])})
    starts, ends = [], []
    for b in campus_buildings(conn, campus_id):
        bf = known_facts(conn, "building", b["id"], ["construction_start", "construction_end"])
        starts += [bf["construction_start"]] if bf.get("construction_start") else []
        ends += [bf["construction_end"]] if bf.get("construction_end") else []
    for label, rows in (("Construction start", starts), ("First building complete", ends)):
        dated = [r for r in rows if isinstance(r["value"], str) and r["value"][:10] <= day().isoformat()]
        if dated:
            first = min(dated, key=lambda r: r["value"])
            out.append({"label": label, "date": with_value(first, first["value"][:10])})
    if (row := first_operational(conn, campus_id)) is not None:
        out.append({"label": "First building operational", "date": with_value(row, row["valid_from"].isoformat())})
    if (row := facts.get("online_year")) and isinstance(row["value"], int):
        basis = (facts.get("online_year_basis") or {}).get("value")
        label = "Online" if basis == "observed" else "Expected online"
        out.append({"label": label, "date": with_value(row, str(row["value"]))})
    return out


def campus_imagery(conn: Conn, campus_id: str) -> list[Doc]:
    by_month: dict[str, Doc] = {}
    for r in known_fact_rows(conn, "campus", campus_id, ["imagery"])["imagery"]:
        v = r["value"] if isinstance(r["value"], dict) else {}
        if isinstance(v.get("month"), str) and isinstance(v.get("url"), str) and r["valid_from"] <= day():
            by_month.setdefault(v["month"], {"month": v["month"], "url": v["url"]})
    return [by_month[m] for m in sorted(by_month)]


def detail_doc(conn: Conn, campus: Row, facts: dict[str, Row]) -> Doc:
    cid = campus["id"]
    all_facts = known_facts(conn, "campus", cid)
    doc = row_doc(conn, campus, facts)
    parcels = list(campus.get("parcel_ids") or [])
    for b in campus_buildings(conn, cid):
        parcels += [p for p in (b.get("parcel_ids") or []) if p not in parcels]
    doc.update(
        {
            "facts": facts_table(all_facts),
            "ownerOfRecord": text_value(all_facts.get("owner_of_record"), "F"),
            "parcelIds": parcels,
            "balancingAuthority": text_value(all_facts.get("balancing_authority"), "F"),
            "keyDates": key_dates(conn, cid, all_facts),
            "imagery": campus_imagery(conn, cid),
            "tabCounts": {
                "buildings": len(building_docs(conn, cid)),
                "records": len(campus_records(conn, cid)),
                "history": len(campus_history(conn, cid)),
            },
        }
    )
    if (load := large_load(conn, cid, all_facts)) is not None:
        doc["largeLoad"] = load
    if (gens := all_facts.get("backup_generator_count")) is not None:
        doc["backupGenerators"] = {"count": number_value(gens, "F"), "mw": nd("F"), "fuel": nd("F")}
    return doc


@builder("dc_detail", keys=campus_ids)
def build_dc_detail(conn: Conn, campus_id: str) -> Doc | None:
    found = _campus(conn, campus_id)
    return detail_doc(conn, *found) if found else None
