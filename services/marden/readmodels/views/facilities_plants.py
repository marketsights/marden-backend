"""On-site power (spec 15, 16, 20): `plant_row` (types.ts PowerPlantRow; the API filters, sorts, pages
and summarises these docs) and `plant_detail` (PowerPlantDetail).

- Units: the plant's power_unit rows with their unit facts (OEM, model, count, rating, fuel) as known
  on the build date; total MW = count x rating, computed here. A unit has no status of its own in the
  records, so it shows the plant's status.
- Permitted MW = the sum of unit ratings when every unit row has a count and a rating (spec 15:
  computed in code, tagged with the weakest unit fact); else the plant's capacity as stated; else n/d.
- Air permit: the plant's `air_permit` fact (agency, number, type, URL); its status from the permit
  docket entity's newer status when Marden watches it (TCEQ 'PENDING' -> Permit pending ...). A PSD
  permit is a major-source permit ('Major'). Filed / decided dates and amendments stay n/d until the
  permit collectors record them.
- Fuel supply: the fuel line (pipeline / lateral plant) serving the same campus, or the plant itself
  when it is one: its FERC / state docket and the planned in-service dates as stated.
- Grid mode from the served campus's power source (On-site gas -> Islanded, Grid / Hybrid -> Grid-tied).
- Location: the plant's own point and county; n/d (null) until a permit or EIA record places it.
- `financed`: the approved site link of the campus the plant serves (facilities_common).
"""

from __future__ import annotations

from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.context import known_facts
from marden.readmodels.registry import builder
from marden.readmodels.views.facilities_campus import building_counts
from marden.readmodels.views.facilities_common import (
    DC_STATUSES,
    PLANT_STATUSES,
    PLANT_TECHS,
    enum_value,
    event_history,
    fact_history,
    financed_link,
    is_fuel_line,
    last_change,
    number_value,
    plant_ids,
    plant_row,
    served_campuses,
    serving_plants,
    subject_events,
    text_value,
)
from marden.readmodels.views.sites_common import Doc, day, fact_evidence, nd, weakest_tag, with_value
from marden.readmodels.views.sites_text import text_of

PLANT_FIELDS = (
    "operator",
    "technology",
    "technology_type",
    "status",
    "stage",
    "capacity_mw",
    "air_permit",
    "expected_cod",
    "original_cod",
    "dockets",
    "latest_change",
)
UNIT_FIELDS = ("oem", "model", "count", "rating_mw_each", "fuel", "count_is_maximum", "online_year")
HISTORY_FIELDS = ("status", "capacity_mw", "expected_cod", "air_permit", "technology", "operator")
PERMIT_STATUS = {
    "pending": "Permit pending",
    "under review": "Permit pending",
    "received": "Permit pending",
    "issued": "Permitted",
    "approved": "Permitted",
    "final": "Permitted",
    "withdrawn": "Withdrawn",
    "denied": "Denied",
}
PERMIT_TYPES = {"PSD": "Major"}
GRID_MODES = {"On-site gas": "Islanded", "Grid": "Grid-tied", "Hybrid": "Grid-tied"}


# ---------------------------------------------------------------- units
def plant_units(conn: Conn, plant_id: str, plant_status: Any) -> list[Doc]:
    """PowerUnit rows (with `_rows`: the facts behind count and rating, for permitted MW)."""
    out = []
    for u in conn.execute("SELECT * FROM power_unit WHERE plant_id = %s ORDER BY id", (plant_id,)).fetchall():
        f = known_facts(conn, "unit", u["id"], UNIT_FIELDS)
        if not f:
            continue  # not known on the build date
        count = (f.get("count") or {}).get("value", u.get("count"))
        rating = (f.get("rating_mw_each") or {}).get("value")
        count = count if isinstance(count, int) and not isinstance(count, bool) else None
        rated = isinstance(rating, int | float) and not isinstance(rating, bool)
        year = (f.get("online_year") or {}).get("value")
        out.append(
            {
                "oem": (f.get("oem") or {}).get("value") or u.get("oem") or None,
                "model": (f.get("model") or {}).get("value") or u.get("model") or None,
                "count": count or 0,
                "ratingMwEach": number_value(f.get("rating_mw_each"), "F"),
                "totalMw": round(count * rating, 3) if count is not None and rated else None,
                "fuel": (f.get("fuel") or {}).get("value"),
                "status": plant_status if plant_status in PLANT_STATUSES else None,
                "onlineYear": year if isinstance(year, int) else None,
                "permitRef": u.get("permit_ref") or None,
                "_rows": [r for r in (f.get("count"), f.get("rating_mw_each")) if r is not None],
            }
        )
    return out


def public_units(units: list[Doc]) -> list[Doc]:
    return [{k: v for k, v in u.items() if not k.startswith("_")} for u in units]


def units_summary(units: list[Doc]) -> Doc:
    by: dict[tuple[Any, Any], int] = {}
    for u in units:
        by[(u["oem"], u["model"])] = by.get((u["oem"], u["model"]), 0) + u["count"]
    by_model = [
        {"oem": oem, "model": model, "count": n}
        for (oem, model), n in sorted(by.items(), key=lambda kv: (-kv[1], str(kv[0])))
    ]
    return {"count": sum(u["count"] for u in units), "byModel": by_model}


def permitted_mw(units: list[Doc], facts: dict[str, Row]) -> Doc:
    if units and all(u["totalMw"] is not None for u in units):
        rows = [r for u in units for r in u["_rows"]]
        names = sorted({r["source_name"] for r in rows if r.get("source_name")})
        urls = {r["source_url"] for r in rows if r.get("source_url")}
        out: Doc = {
            "value": round(sum(u["totalMw"] for u in units), 3),
            "tag": weakest_tag(r["tag"] for r in rows),
            "asOf": max(r["valid_from"] for r in rows).isoformat(),
            "sourceName": " + ".join(names),
        }
        if len(urls) == 1:
            out["sourceUrl"] = urls.pop()
        return out
    return number_value(facts.get("capacity_mw"), "F")


# ---------------------------------------------------------------- permit, fuel, grid mode
def _permit_entity(conn: Conn, key: Any) -> dict[str, Row]:
    if not isinstance(key, str):
        return {}
    row = conn.execute(
        "SELECT id FROM entity WHERE meta ->> 'proceeding_key' = %s ORDER BY id LIMIT 1", (key,)
    ).fetchone()
    return known_facts(conn, "entity", row["id"], ["status", "proceeding"]) if row else {}


def air_permit(conn: Conn, facts: dict[str, Row]) -> Doc | None:
    """types.ts AirPermit from the plant's permit fact; None (n/d) when the plant has none."""
    row = facts.get("air_permit")
    v = row["value"] if row and isinstance(row["value"], dict) else None
    if v is None or not v.get("number"):
        return None
    docket = _permit_entity(conn, v.get("proceeding_key")).get("status")
    stated = docket["value"] if docket and (docket["valid_from"] >= row["valid_from"]) else v.get("status")
    kind = v.get("type")
    return {
        "agency": str(v.get("agency") or ""),
        "number": str(v["number"]),
        "type": PERMIT_TYPES.get(kind, kind) if isinstance(kind, str) else None,
        "status": PERMIT_STATUS.get(str(stated).strip().lower()) if stated else None,
        "filed": None,
        "decided": None,
        "url": v.get("url") or row.get("source_url") or "",
        "amendments": [],
    }


def row_permit(permit: Doc | None) -> Doc | None:
    keys = ("agency", "number", "type", "status", "decided", "url")
    return {k: permit[k] for k in keys} if permit else None


def fuel_lines(conn: Conn, plant: Row, facts: dict[str, Row], campuses: list[Row]) -> list[tuple[Row, dict[str, Row]]]:
    if is_fuel_line(plant, facts):
        return [(plant, facts)]
    out = []
    for campus in campuses:
        for other in serving_plants(conn, campus["id"]):
            of = known_facts(conn, "plant", other["id"], PLANT_FIELDS)
            if other["id"] != plant["id"] and is_fuel_line(other, of):
                out.append((other, of))
    return out


def fuel_supply(lines: list[tuple[Row, dict[str, Row]]]) -> Doc:
    for line, lf in lines:
        if row := lf.get("expected_cod"):
            return with_value(row, f"{line['name']} · in service {text_of(row['value'])}")
    for line, lf in lines:
        if row := lf.get("dockets") or lf.get("operator"):
            return with_value(row, line["name"])
    return nd("F")


def fuel_detail(lines: list[tuple[Row, dict[str, Row]]]) -> Doc | None:
    if not lines:
        return None
    line, lf = lines[0]
    dockets = lf.get("dockets")
    first = dockets["value"][0] if dockets and isinstance(dockets["value"], list) and dockets["value"] else {}
    pipeline = with_value(dockets, first.get("title") or line["name"]) if dockets else fuel_supply(lines)
    in_service = []
    for field in ("original_cod", "expected_cod"):
        if row := lf.get(field):
            label = "Originally planned in service" if field == "original_cod" else "Planned in service"
            in_service.append(
                {"date": str(row["value"]), "evidence": fact_evidence(row, f"{label} {text_of(row['value'])}")}
            )
    return {
        "pipeline": pipeline,
        "regulator": first.get("agency"),
        "docket": first.get("number"),
        "inService": in_service,
    }


def grid_mode(conn: Conn, campuses: list[Row]) -> str:
    modes = {
        GRID_MODES.get((known_facts(conn, "campus", c["id"], ["power_source"]).get("power_source") or {}).get("value"))
        for c in campuses
    }
    modes.discard(None)
    return modes.pop() if len(modes) == 1 else "Unknown"


# ---------------------------------------------------------------- documents
def _plant(conn: Conn, plant_id: str) -> tuple[Row, dict[str, Row]] | None:
    plant = plant_row(conn, plant_id)
    return (plant, known_facts(conn, "plant", plant_id, PLANT_FIELDS)) if plant else None


def plant_history(conn: Conn, plant_id: str, events: list[Row]) -> list[Doc]:
    out = fact_history(conn, "plant", plant_id, HISTORY_FIELDS, initial=("status", "air_permit")) + event_history(
        events
    )
    out.sort(key=lambda h: (h["date"], h["field"]), reverse=True)
    return out


def row_parts(conn: Conn, plant: Row, facts: dict[str, Row]) -> tuple[Doc, dict[str, Any]]:
    pid = plant["id"]
    status = facts.get("status")
    units = plant_units(conn, pid, status["value"] if status else None)
    campuses = served_campuses(conn, pid)
    events = subject_events(conn, "plant", pid)
    history = plant_history(conn, pid, events)
    permit = air_permit(conn, facts)
    lines = fuel_lines(conn, plant, facts, campuses)
    tech = (facts.get("technology") or {}).get("value")
    doc = {
        "id": pid,
        "name": plant["name"],
        "county": plant.get("county"),
        "state": plant["state"],
        "lat": plant["lat"],
        "lon": plant["lon"],
        "serves": {"id": campuses[0]["id"], "name": campuses[0]["name"]} if campuses else None,
        "operator": text_value(facts.get("operator"), "F"),
        "technology": tech if tech in PLANT_TECHS else None,
        "units": units_summary(units),
        "permittedMw": permitted_mw(units, facts),
        "status": enum_value(status, PLANT_STATUSES),
        "airPermit": row_permit(permit),
        "fuelSupply": fuel_supply(lines),
        "gridMode": grid_mode(conn, campuses),
        "financed": financed_link(conn, [c["id"] for c in campuses]),
        "lastChange": last_change(history, events, facts),
    }
    return doc, {"units": units, "campuses": campuses, "history": history, "permit": permit, "lines": lines}


@builder("plant_row", keys=plant_ids)
def build_plant_row(conn: Conn, plant_id: str) -> Doc | None:
    found = _plant(conn, plant_id)
    return row_parts(conn, *found)[0] if found else None


def served_campus(conn: Conn, campuses: list[Row], permitted: Doc) -> Doc | None:
    if not campuses:
        return None
    campus = campuses[0]
    cf = known_facts(conn, "campus", campus["id"], ["status", "facility_mw", "building_count", "buildings_operational"])
    mw = number_value(cf.get("facility_mw"), "F")
    cover = nd("M")
    if isinstance(permitted["value"], int | float) and isinstance(mw["value"], int | float) and mw["value"] > 0:
        cover = {
            "value": round(100.0 * permitted["value"] / mw["value"], 1),
            "tag": "M",
            "asOf": day().isoformat(),
            "methodVersion": "plant-load-cover@0.1",
            "sourceName": f"Marden: permitted MW ({permitted.get('sourceName') or 'n/d'}) / facility MW "
            f"({mw.get('sourceName') or 'n/d'})",
        }
    status = (cf.get("status") or {}).get("value")
    return {
        "id": campus["id"],
        "name": campus["name"],
        "status": status if status in DC_STATUSES else None,
        "facilityMw": mw,
        "buildings": building_counts(conn, campus["id"], cf)["total"],
        "loadCoverPct": cover,
    }


@builder("plant_detail", keys=plant_ids)
def build_plant_detail(conn: Conn, plant_id: str) -> Doc | None:
    found = _plant(conn, plant_id)
    if not found:
        return None
    doc, parts = row_parts(conn, *found)
    doc.update(
        {
            "unitRows": public_units(parts["units"]),
            "permit": parts["permit"],
            "fuel": fuel_detail(parts["lines"]),
            "servedCampus": served_campus(conn, parts["campuses"], doc["permittedMw"]),
            "history": parts["history"],
        }
    )
    return doc
