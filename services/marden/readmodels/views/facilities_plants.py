"""On-site power (spec 15, 16, 20): `plant_row` (types.ts PowerPlantRow; the API filters, sorts, pages
and summarises these docs) and `plant_detail` (PowerPlantDetail).

- Units: the plant's power_unit rows with their unit facts (OEM, model, count, rating, fuel) as known
  on the build date; total MW = count x rating, computed here. A unit has no status of its own in the
  records, so it shows the plant's status. A plant without power_unit rows takes its units from the
  equipment statements of its generating-unit air permits (`air_permits`: PSD / NSR / standard permits,
  not PBR registrations for emergency engines or Title V permits that restate units): each statement
  that gives a count and a rating each, one per OEM and model per permit (the latest record's, since a
  revision restates the units), with the permit's status and number and the record's URL.
- Fuel type (detail `fuelType`): the unit rows' fuel facts as stated, with the units they cover when
  not every unit row states one; n/d otherwise.
- Permitted MW = the sum of unit ratings when every unit row has a count and a rating (spec 15:
  computed in code, tagged with the weakest unit fact); else the plant's capacity as stated; else n/d.
- Air permit: the plant's `air_permit` fact (agency, number, type, URL); its status from the permit
  docket entity's newer status when Marden watches it (TCEQ 'PENDING' -> Permit pending ...). A PSD
  permit is a major-source permit ('Major'). Filed / decided dates and amendments come from the
  `air_permits` collector's value (filed, issued, amendments); an older record without them stays n/d.
  The detail's permit also names the holder (applicant) and facility, the permit's other ids, the
  emission limits only as the record states them (pollutant, amount, unit, basis, URL), and the plant's
  other matched permits (`related`: number, type, status, dates, record URL).
- Fuel supply: the fuel line (pipeline / lateral plant) serving the same campus, or the plant itself
  when it is one: its FERC / state docket and the planned in-service dates as stated.
- Grid mode from the served campus's power source (On-site gas -> Islanded, Grid / Hybrid -> Grid-tied).
- Location: the plant's own point and county; n/d (null) until a permit or EIA record places it.
- `financedSites`: every approved financed site of the campuses the plant serves, most debt first;
  `financed` is the first of them (facilities_common).
"""

from __future__ import annotations

import re
from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.context import known_facts
from marden.readmodels.registry import builder
from marden.readmodels.views.facilities_campus import building_counts
from marden.readmodels.views.facilities_common import (
    DC_STATUSES,
    PLANT_STATUSES,
    PLANT_TECHS,
    Change,
    enum_value,
    event_changes,
    fact_changes,
    financed_sites,
    history_entries,
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
    "air_permits",
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
    "cancelled": "Withdrawn",
    "denied": "Denied",
}
# Agency permit kinds (air_permits collector) -> the card's type: PSD is a major-source permit; a Texas NSR
# permit outside PSD and a permit-by-rule registration are minor authorizations.
PERMIT_TYPES = {"PSD": "Major", "NSR": "Minor", "PBR": "Minor"}
GRID_MODES = {"On-site gas": "Islanded", "Grid": "Grid-tied", "Hybrid": "Grid-tied"}
# Permits whose equipment statements do not stand in for the plant's generating units: PBR registrations
# (emergency engines) and Title V operating permits (they restate units authorized elsewhere).
NON_UNIT_PERMITS = {"PBR", "Title V"}
CLOSED_PERMITS = {"withdrawn", "cancelled", "denied", "inactive"}
# Makers as the permit records shorten them ("Solar Titan 350", "GE LM2500").
OEM_NAMES = {"Solar": "Solar Turbines", "GE": "GE Vernova"}
BUILT = ("Under construction", "Operating")


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
                "_fuel": f.get("fuel"),
            }
        )
    return out


def _is_num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and v > 0


def _record_day(statement: dict[str, Any]) -> str:
    """'Technical Review 2025-01-22' -> '2025-01-22'; '' when the record is not dated."""
    m = re.search(r"\d{4}-\d{2}-\d{2}", str(statement.get("record") or ""))
    return m.group(0) if m else ""


def permit_units(row: Row | None, plant_status: Any) -> list[Doc]:
    """PowerUnit rows from the equipment statements of the plant's generating-unit permits (see the
    module notes); [] when no such statement gives both a count and a rating each."""
    permits = row["value"] if row and isinstance(row["value"], list) else []
    out = []
    for p in permits:
        if not isinstance(p, dict) or p.get("type") in NON_UNIT_PERMITS:
            continue
        stated = str(p.get("status") or "").strip().lower()
        if stated in CLOSED_PERMITS:
            continue
        latest: dict[tuple[Any, Any], dict[str, Any]] = {}
        for s in p.get("units") or []:
            count = s.get("count") if isinstance(s, dict) else None
            if not (isinstance(count, int) and _is_num(count) and _is_num(s.get("rating_mw_each"))):
                continue
            key = (s.get("oem"), s.get("model"))
            if key not in latest or _record_day(s) >= _record_day(latest[key]):
                latest[key] = s
        status = PERMIT_STATUS.get(stated)
        if status == "Permitted" and plant_status in BUILT:
            status = plant_status  # the plant's construction or operating record is the newer state
        for s in latest.values():
            assert row is not None
            src = {
                "value": None,
                "tag": row["tag"],
                "valid_from": row["valid_from"],
                "method_version": row.get("method_version"),
                "source_name": " ".join(str(x) for x in (p.get("agency"), s.get("record")) if x)
                or row.get("source_name"),
                "source_url": s.get("url") or p.get("url") or row.get("source_url"),
            }
            out.append(
                {
                    "oem": OEM_NAMES.get(s.get("oem"), s.get("oem")) or None,
                    "model": s.get("model") or None,
                    "count": s["count"],
                    "ratingMwEach": with_value(src, s["rating_mw_each"]),
                    "totalMw": round(s["count"] * s["rating_mw_each"], 3),
                    "fuel": None,
                    "status": status,
                    "onlineYear": None,
                    "permitRef": str(p["number"]) if p.get("number") else None,
                    "_rows": [src],
                    "_fuel": None,
                }
            )
    return out


def fuel_type(units: list[Doc]) -> Doc:
    """The units' fuel as stated ("natural gas"; "diesel (Cat C32B)" when only some unit rows state one)."""
    rows = [(u, u["_fuel"]) for u in units if u.get("_fuel") and isinstance(u["_fuel"]["value"], str)]
    if not rows:
        return nd("F")
    every = len(rows) == len(units)
    by_fuel: dict[str, list[str]] = {}
    for u, r in rows:
        by_fuel.setdefault(r["value"], []).append(" ".join(x for x in (u["oem"], u["model"]) if x))
    text = "; ".join(
        f if every else f"{f} ({', '.join(m for m in models if m) or 'unit n/d'})" for f, models in by_fuel.items()
    )
    facts = [r for _, r in rows]
    newest = max(facts, key=lambda r: r["valid_from"])
    out = with_value(newest, text)
    out["tag"] = weakest_tag(r["tag"] for r in facts)
    out["sourceName"] = " + ".join(sorted({r["source_name"] for r in facts if r.get("source_name")}))
    if len({r.get("source_url") for r in facts}) > 1:
        out.pop("sourceUrl", None)  # several records: the names say which
    methods = sorted({r["method_version"] for r in facts if r["tag"] == "M" and r.get("method_version")})
    if out["tag"] == "M" and methods:
        out["methodVersion"] = " + ".join(methods)
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
        methods = sorted({r["method_version"] for r in rows if r["tag"] == "M" and r.get("method_version")})
        if out["tag"] == "M" and methods:
            out["methodVersion"] = " + ".join(methods)  # an M sum names the methods of its M parts
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
        "filed": _iso_day(v.get("filed")),
        "decided": _iso_day(v.get("issued")),
        "url": v.get("url") or row.get("source_url") or "",
        "amendments": [
            {"date": a["date"], "text": str(a.get("text") or ""), "tag": row["tag"],
             "sourceName": row.get("source_name") or "", "sourceUrl": a.get("url") or v.get("url") or ""}
            for a in v.get("amendments") or [] if isinstance(a, dict) and _iso_day(a.get("date"))
        ],
        "holder": v["applicant"] if isinstance(v.get("applicant"), str) and v["applicant"].strip() else None,
        "facility": _facility(v.get("facility")),
        "otherIds": other_ids(str(v["number"]), v.get("ids")),
        "limits": [
            {"pollutant": str(lim["pollutant"]), "value": lim["value"], "unit": str(lim.get("unit") or ""),
             "basis": str(lim.get("basis") or ""), "moreThan": lim.get("more_than") is True,
             "lessThan": lim.get("less_than") is True,
             "url": lim.get("url") or v.get("url") or row.get("source_url") or ""}
            for lim in v.get("emission_limits") or []
            if isinstance(lim, dict) and lim.get("pollutant") and isinstance(lim.get("value"), int | float)
        ],
        "related": related_permits(facts.get("air_permits"), str(v["number"])),
    }


def other_ids(number: str, ids: Any) -> list[str]:
    """Other numbers the permit goes by, without the ones its number already shows ('10883' beside 'NSR 10883',
    each of '26-A-117, 26-A-118')."""
    shown = {number, *re.split(r"[,\s]+", number)}
    out: list[str] = []
    for i in ids if isinstance(ids, list) else []:
        if str(i) not in shown and str(i) not in out:
            out.append(str(i))
    return out


def related_permits(row: Row | None, main: str) -> list[Doc]:
    """The plant's other matched permits (the `air_permits` collector), oldest filing first: the permits behind
    unit rows, backup engines (PBR) and the operating permit (Title V), each with its agency record."""
    permits = row["value"] if row and isinstance(row["value"], list) else []
    out = []
    for p in permits:
        if not isinstance(p, dict) or not p.get("number") or str(p["number"]) == main:
            continue
        kind, stated = p.get("type"), str(p.get("status") or "").strip().lower()
        out.append(
            {
                "agency": str(p.get("agency") or ""),
                "number": str(p["number"]),
                "type": PERMIT_TYPES.get(kind, kind) if isinstance(kind, str) else None,
                "status": PERMIT_STATUS.get(stated),
                "filed": _iso_day(p.get("filed")),
                "decided": _iso_day(p.get("issued")),
                "url": p.get("url") or (row or {}).get("source_url") or "",
            }
        )
    return sorted(out, key=lambda r: (r["filed"] or r["decided"] or "9999", r["number"]))


def _facility(value: Any) -> Doc | None:
    """The agency's facility record the permit belongs to (TCEQ RN, Iowa plant number ...)."""
    if isinstance(value, dict) and value.get("key"):
        return {"id": str(value["key"]), "name": value.get("name") or None}
    return {"id": value, "name": None} if isinstance(value, str) and value else None


def _iso_day(value: Any) -> str | None:
    """A permit collector's ISO date ('2025-01-22'); anything else is n/d."""
    return value if isinstance(value, str) and len(value) == 10 and value[4] == "-" and value[7] == "-" else None


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


def plant_changes(conn: Conn, plant_id: str, events: list[Row]) -> list[Change]:
    """The plant's history (value changes, first status and air permit, records); newest first via
    history_entries, and the candidates of `lastChange`."""
    return fact_changes(conn, "plant", plant_id, HISTORY_FIELDS, initial=("status", "air_permit")) + event_changes(
        events
    )


def row_parts(conn: Conn, plant: Row, facts: dict[str, Row]) -> tuple[Doc, dict[str, Any]]:
    pid = plant["id"]
    status = facts.get("status")
    units = plant_units(conn, pid, status["value"] if status else None)
    if not units:
        units = permit_units(facts.get("air_permits"), status["value"] if status else None)
    campuses = served_campuses(conn, pid)
    events = subject_events(conn, "plant", pid)
    changes = plant_changes(conn, pid, events)
    history = history_entries(changes)
    permit = air_permit(conn, facts)
    lines = fuel_lines(conn, plant, facts, campuses)
    financed = financed_sites(conn, [c["id"] for c in campuses])
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
        "financed": financed[0] if financed else None,  # the primary site (most debt)
        "financedSites": financed,
        "lastChange": last_change(changes, facts),
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
            "fuelType": fuel_type(parts["units"]),
            "permit": parts["permit"],
            "fuel": fuel_detail(parts["lines"]),
            "servedCampus": served_campus(conn, parts["campuses"], doc["permittedMw"]),
            "history": parts["history"],
        }
    )
    return doc
