"""Building rows for the campus Buildings tab, the Data centers table's expanded row and the site
Buildings tab (types.ts Building + CampusBuilding extras), each value with its source or the reason it is
n/d (`why`, keyed by field, shown on hover).

Per building, on top of sites_links.building_doc (its own stated sq ft, MW, permit, certificate of
occupancy, first visible, change score):

- `status` / `statusNote`: the one status facilities_status decides for the campus (strongest, newest
  record; n/d with the reason when a count says how many buildings operate but not which);
- `sqft`: stated floor area when a record gives it; else the traced footprint's ground area measured by
  the capacity model (tag M, `sqftBasis: 'footprint'`: one storey's ground area, not floor area);
- `registration`: the state construction registration (Texas TDLR TABS) when one names the building: its
  number and status, with every registration (scope, dates, floor area, stated cost) in `items`;
- `estCompletion`: the completion date a record states for the building (TDLR's estimated completion,
  tag F) else, for a building not yet operating on a campus a V1 site finances, the site's P50 rent start
  from the cushion model (tag M, site-level: `why` says so); operating buildings have none;
- `gpuType`: the chips stated for the building, else for the campus (Epoch AI, tag R), else the capacity
  model's generation by online year (tag M);
- `estimate`: the capacity model's building estimate (facility and IT MW, GPUs and, for NVL72 platforms,
  racks, each low / base / high) with its method and assumptions, tag M;
- `firstVisible` / `firstVisibleSource`: Epoch AI's dated construction start (R), else the first Sentinel-2
  capture whose footprint reading shows a structure (the imagery model's `imageryStage` readings, M);
- `inService`: the dated in-service record (certificate of occupancy, energization, Epoch's completion
  date), n/d until one exists.
Nothing is computed here: every number is a stored fact or a model output.
"""

from __future__ import annotations

import re
from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.context import known_fact_rows, known_facts
from marden.readmodels.views import facilities_status
from marden.readmodels.views.sites_common import Doc, day, latest_output, nd, with_value
from marden.readmodels.views.sites_links import building_doc

EXTRA_FACTS = ("sqft", "construction_start", "construction_end", "expected_completion", "gpu_type", "co_date",
               "energized_date", "in_service_date")
IN_SERVICE = ("co_date", "energized_date", "in_service_date", "construction_end")
CAMPUS_FACTS = ("chip_types", "chips", "chips_planned", "facility_mw", "it_mw")
ESTIMATE_KEYS = ("facilityMw", "itMw", "gpus", "racks")
_DATE = re.compile(r"^\d{4}-\d{2}(-\d{2})?")


def _m(out: Row, value: Any) -> Doc:
    o = out["output"]
    return {"value": value, "tag": "M", "asOf": out["as_of"].isoformat(), "sourceName": "Marden capacity model",
            "methodVersion": o.get("methodVersion") or "capacity v0.1 (model v0)"}


def estimate_doc(out: Row | None) -> Doc | None:
    """The capacity model's building estimate as shown in the table (ranges, generation, method)."""
    if out is None or not isinstance(out["output"], dict):
        return None
    o = out["output"]
    doc: Doc = {k: o[k] for k in ESTIMATE_KEYS if isinstance(o.get(k), dict) and o[k].get("base") is not None}
    if not doc:
        return None
    gen = o.get("gpuGeneration")
    doc.update({
        "gpuGeneration": gen if isinstance(gen, dict) else None,
        "gpusPerRack": o.get("gpusPerRack"),
        "kwPerGpu": (o.get("kwPerGpu") or {}).get("value"),
        "gpuShareOfIt": (o.get("gpuShareOfIt") or {}).get("value"),
        "pue": (o.get("pue") or {}).get("value"),
        "confidence": o.get("confidence"),
        "methods": [{"method": m.get("method"), "assumptions": m.get("assumptions")} for m in o.get("methods") or []],
        "methodVersion": o.get("methodVersion"),
        "asOf": out["as_of"].isoformat(),
    })
    return doc


def _usd_text(v: Any) -> str:
    if not isinstance(v, int | float):
        return ""
    return f"${v / 1e9:,.2f}B" if v >= 1e9 else f"${v / 1e6:,.0f}M"


def registration_doc(rows: list[Row]) -> Doc | None:
    """The building's state construction registration (core and shell first): 'TDLR TABS2024023596', with
    every registration for the building in `items` (number, scope, status, dates, stated cost)."""
    regs = [r for r in rows if isinstance(r["value"], dict) and r["value"].get("number")]
    if not regs:
        return None
    regs.sort(key=lambda r: (r["value"].get("scope") != "Core and shell", r["value"].get("date") or ""))
    lead = regs[0]
    v = lead["value"]
    doc = with_value(lead, f"{v.get('agency') or 'State'} {v['number']}")
    doc["status"] = v.get("status")
    doc["items"] = [{
        "number": r["value"]["number"], "scope": r["value"].get("scope"), "status": r["value"].get("status"),
        "registered": r["value"].get("date"), "start": r["value"].get("start"),
        "completion": r["value"].get("completion"), "sqft": r["value"].get("sqft"),
        "estimatedCost": _usd_text(r["value"].get("estimatedCostUsd")), "sourceUrl": r.get("source_url"),
    } for r in regs]
    return doc


def _chips(cf: dict[str, Row]) -> Doc | None:
    """The campus's stated chips (largest count first), as text: 'GB200', 'H100, B200'."""
    for f in ("chips", "chips_planned"):
        row = cf.get(f)
        if row is not None and isinstance(row["value"], dict) and row["value"]:
            names = sorted(row["value"], key=lambda k: -(row["value"][k] or 0))
            return with_value(row, ", ".join(names))
    row = cf.get("chip_types")
    if row is not None and isinstance(row["value"], list) and row["value"]:
        return with_value(row, ", ".join(str(x) for x in row["value"]))
    return None


def site_projection(conn: Conn, campus_id: str) -> tuple[str, Doc] | None:
    """(site slug, P50 rent start Sourced) of the V1 site that finances the campus, from the cushion model."""
    from marden.models.cushion_output import stored_output

    rows = conn.execute(
        """SELECT s.slug FROM site_link l JOIN site s ON s.slug = l.site_slug
            WHERE l.campus_id = %s AND l.status = 'approved' AND s.in_v1 ORDER BY s.slug""",
        (campus_id,),
    ).fetchall()
    for r in rows:
        out = stored_output(conn, r["slug"], day())
        p50 = ((out or {}).get("projection") or {}).get("p50")
        if isinstance(p50, str):
            return r["slug"], {"value": p50, "tag": "M", "asOf": day().isoformat(),
                               "sourceName": "Marden cushion model", "methodVersion": "cushion v0.1"}
    return None


def building_rows(conn: Conn, buildings: list[Row]) -> list[Doc]:
    """Building docs with the decided status and every extra (module docstring), in the given order."""
    by_campus: dict[str, list[Row]] = {}
    for b in buildings:
        by_campus.setdefault(b["campus_id"], []).append(b)
    decided: dict[str, facilities_status.Resolution] = {}
    campus_facts: dict[str, dict[str, Row]] = {}
    projections: dict[str, tuple[str, Doc] | None] = {}
    for cid in by_campus:
        decided[cid] = facilities_status.load(conn, cid)  # every building of the campus, for the count
        campus_facts[cid] = known_facts(conn, "campus", cid, CAMPUS_FACTS)
        projections[cid] = site_projection(conn, cid)
    out = []
    for b in buildings:
        doc = building_doc(conn, b)
        if doc is None:
            continue
        out.append(extend(conn, b, doc, decided[b["campus_id"]], campus_facts[b["campus_id"]],
                          projections[b["campus_id"]]))
    return out


def extend(conn: Conn, b: Row, doc: Doc, res: facilities_status.Resolution, cf: dict[str, Row],
           projection: tuple[str, Doc] | None) -> Doc:
    facts = known_facts(conn, "building", b["id"], EXTRA_FACTS)
    why: dict[str, str] = {}
    status = res.buildings.get(b["id"])
    if status is not None:
        doc["status"] = status
    if (note := res.building_notes.get(b["id"])) is not None:
        doc["statusNote"] = note
        if status is not None and status.get("value") is None:
            why["status"] = note
    out = latest_output(conn, "capacity", "building", b["id"])
    est = estimate_doc(out)
    # square feet: stated, else the traced footprint's ground area
    measured = out["output"].get("footprintSqft") if out is not None else None
    if doc["sqft"].get("value") is None and out is not None and isinstance(measured, int | float):
        doc["sqft"] = _m(out, measured)
        doc["sqftBasis"] = "footprint"
        why["sqft"] = ("Measured from the building's traced footprint (Epoch AI's outline): ground area of one "
                       "storey, not floor area. No permit or filing states its floor area.")
    elif doc["sqft"].get("value") is None:
        why["sqft"] = "No permit, registration or filing states its floor area, and it has no traced footprint."
    regs = known_fact_rows(conn, "building", b["id"], ["state_registration"])["state_registration"]
    if (reg := registration_doc([r for r in regs if r["valid_from"] <= day()])) is not None:
        doc["registration"] = reg
    # estimated completion: stated for the building, else the financed site's P50 (not yet operating only)
    operating = (doc["status"] or {}).get("value") == "Operating"
    row = facts.get("expected_completion")
    if row is not None and isinstance(row["value"], str) and _DATE.match(row["value"]):
        doc["estCompletion"] = with_value(row, row["value"][:10])
    elif not operating and projection is not None:
        slug, p50 = projection
        doc["estCompletion"] = p50
        why["estCompletion"] = (f"Site-level: P50 rent start of the {slug} financing from the cushion model, "
                                "not a date stated for this building.")
    elif not operating:
        why["estCompletion"] = "No permit, registration or company schedule dates this building's completion."
    # GPU type: the building's, else the campus's stated chips, else the model's generation
    if (row := facts.get("gpu_type")) is not None and isinstance(row["value"], str):
        doc["gpuType"] = with_value(row, row["value"])
    elif (chips := _chips(cf)) is not None:
        doc["gpuType"] = chips
        why["gpuType"] = f"Stated for the campus ({chips.get('sourceName') or 'its record'}), not per building."
    elif est is not None and isinstance(est.get("gpuGeneration"), dict) and est["gpuGeneration"].get("value"):
        doc["gpuType"] = est["gpuGeneration"]
        why["gpuType"] = ("Not stated by the tenant or operator; the capacity model assumes it from the online year "
                          "(2026 or later GB300, 2025 GB200, earlier H100).")
    else:
        why["gpuType"] = "Not stated by the tenant or operator, and no online year to assume one from."
    if est is not None:
        doc["estimate"] = est
    else:
        why["estimate"] = "The capacity model has not estimated this building yet."
    if est is not None and "gpus" not in est:
        missing = (out["output"].get("missing") or []) if out is not None else []
        lacking = next((m.removeprefix("GPU estimate: ") for m in missing if m.startswith("GPU estimate")),
                       "an input is missing")
        why["estGpus"] = f"No GPU estimate: {lacking}."
    if est is not None and "racks" not in est:
        why["estRacks"] = ("Racks are counted only for rack-scale platforms (GB200 / GB300 NVL72, 72 GPUs a rack); "
                           "this building's chips ship as 8-GPU servers or are not known.")
    # in service and first visible
    doc["inService"] = _in_service(facts)
    if doc["inService"].get("value") is None:
        why["inService"] = ("Not operating yet." if not operating else
                            "No certificate of occupancy, energization record or completion date is public.")
    start = facts.get("construction_start")
    if doc.get("firstVisible") and start is not None:
        doc["firstVisibleSource"] = with_value(start, doc["firstVisible"])
    elif not doc.get("firstVisible") and (seen := first_structure(doc.get("imageryStage"))) is not None:
        doc["firstVisible"], doc["firstVisibleSource"] = seen["value"], seen
        why["firstVisible"] = ("First Sentinel-2 capture whose reading of the footprint shows a structure "
                               "(Marden imagery model, 10 m: construction, not operation).")
    if not doc.get("firstVisible"):
        why["firstVisible"] = ("No dated construction start in imagery for this building yet (Epoch AI dates only "
                               "some footprints; the per-building Sentinel-2 read has not run).")
    if doc["facilityMw"].get("value") is None:
        why["facilityMw"] = "Not stated per building by any permit, utility filing or company statement."
    if doc["itMw"].get("value") is None:
        why["itMw"] = "Not stated per building by any permit, utility filing or company statement."
    if doc["buildingPermit"].get("value") is None:
        why["buildingPermit"] = ("No building permit Marden holds names this building"
                                 + ("; the state registration is shown instead." if "registration" in doc else "."))
    if doc["coDate"].get("value") is None:
        why["coDate"] = "No certificate of occupancy Marden holds names this building."
    doc["why"] = why
    return doc


def first_structure(stage: Any) -> Doc | None:
    """The first imagery reading of the footprint that is not bare ground (the imagery model's readings, M)."""
    if not isinstance(stage, dict):
        return None
    readings = [r for r in stage.get("readings") or []
                if isinstance(r, dict) and r.get("stage") not in (None, "bare_ground")]
    if not readings:
        return None
    latest = stage.get("latest") if isinstance(stage.get("latest"), dict) else {}
    first = min(readings, key=lambda r: str(r.get("date")))
    return {"value": str(first["date"])[:10], "tag": "M", "asOf": str(first["date"])[:10],
            "sourceName": latest.get("sourceName") or "Sentinel-2 imagery",
            "methodVersion": latest.get("methodVersion") or "building-stage@0.1"}


def _in_service(facts: dict[str, Row]) -> Doc:
    for f in IN_SERVICE:
        row = facts.get(f)
        if row is not None and isinstance(row["value"], str) and _DATE.match(row["value"]) and len(row["value"]) >= 10:
            return with_value(row, row["value"][:10])
    return nd("R")
