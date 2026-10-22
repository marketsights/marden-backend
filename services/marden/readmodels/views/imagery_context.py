"""What the satellite panels need to mark up a Sentinel-2 image (spec 04 Schedule / Evidence, spec 14
Overview): each image's ground box and dates, and per linked campus the buildings with their outlines and
construction dates, the campus's planned and actual counts of operational buildings, the schedule as
first and as now stated, and Marden's read of the imagery. Nothing is inferred here: every value is a
stored row as Marden knew it on the build date, with its source.

- Image items (types.ts ImageryImage): `bbox` [west, south, east, north] is the box the collector cut the
  image to (sentinel2_stac.imagery_url writes it into the URL's /bbox/ path; the image is a plain lon/lat
  grid over exactly that box), `date` the scene's acquisition day (the fact's valid_from), and for a
  scored scene `comparedWith`, the acquisition day of the earlier scene the change score compares it with
  (the score's quote_loc: "<scene> vs <base scene>; ..."), `changeThreshold` the score at which the
  collector records a visible change (sentinel2_store.CHANGE_THRESHOLD) and `changeMethod` the score's method version.
- Campus entries (types.ts ImageryCampus): building outlines are the `building` rows' footprints (Epoch
  AI's traced polygons), each with its status and Epoch's construction start / end; `plan` is the
  campus's `capacity_plan` steps (date, buildings operational), `operational` its newest
  `buildings_operational` count, `buildingCount` its stated building count; `originalSchedule` /
  `schedule` the delivery schedule as originally and as now stated; `constructionNote` Epoch's note on
  the buildings; `read` the `imagery_note` fact (Marden's read of the imagery, with its flag such as
  "behind schedule"). A field with no row is n/d (value null).
"""

from __future__ import annotations

import json
import re
from typing import Any

from marden.collectors.sentinel2_store import CHANGE_THRESHOLD
from marden.core.db import Conn, Row
from marden.readmodels.context import known_fact_rows, known_facts
from marden.readmodels.views.facilities_common import campus_buildings, campus_row
from marden.readmodels.views.sites_common import Doc, day, nd, src_or_nd, with_value

_BBOX = re.compile(r"/bbox/(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)\.png")
_SCENE_DAY = re.compile(r"^S2[A-D]_MSIL2A_(\d{4})(\d{2})(\d{2})T")

CAMPUS_FACTS = ("imagery_note", "capacity_plan", "buildings_operational", "building_count",
                "original_schedule_text", "schedule_text", "epoch_construction_note")
BUILDING_FACTS = ("status", "construction_start", "construction_end")
STAGE_FIELD = "imagery_stage"  # sentinel2_buildings: per building and capture, tag M
NAIP_FIELD = "imagery_naip"    # naip: the newest NAIP aerial photo over the subject's box, tag R
STAGES = ("bare_ground", "under_construction", "roofed")
ROOFED_SHARE = 2 / 3           # a roofed run holds when this share of the readings from it on are roofed


def image_bbox(url: str) -> list[float] | None:
    """[west, south, east, north] of a Planetary Computer /bbox/ rendering, None for any other URL."""
    m = _BBOX.search(url)
    if not m:
        return None
    w, s, e, n = (float(g) for g in m.groups())
    return [w, s, e, n] if w < e and s < n else None


def scene_day(scene_id: str) -> str | None:
    """'S2B_MSIL2A_20261008T172209_...' -> '2026-10-08' (the acquisition day in the product name)."""
    m = _SCENE_DAY.match(scene_id.strip())
    return f"{m[1]}-{m[2]}-{m[3]}" if m else None


def image_items(conn: Conn, subject_type: str, subject_id: str) -> list[Doc]:
    """Monthly Sentinel-2 images of a site or campus (oldest first), each with its box, day and, when
    scored, its change score, the day of the scene it was compared with and the visible-change threshold."""
    rows = known_fact_rows(conn, subject_type, subject_id, ["imagery", "change_score"])
    scores: dict[str, tuple[float, str | None, str | None]] = {}
    for r in rows["change_score"]:
        scenes = (r.get("quote_loc") or "").split(";")[0].split(" vs ")
        scene = scenes[0].strip()
        if scene and isinstance(r["value"], int | float) and not isinstance(r["value"], bool):
            base = scene_day(scenes[1]) if len(scenes) > 1 else None
            scores.setdefault(scene, (float(r["value"]), base, r.get("method_version")))
    by_month: dict[str, Doc] = {}
    for r in rows["imagery"]:
        v = r["value"] if isinstance(r["value"], dict) else {}
        month, url = v.get("month"), v.get("url")
        if not (isinstance(month, str) and isinstance(url, str)) or month in by_month or r["valid_from"] > day():
            continue
        item: Doc = {"month": month, "url": url, "caption": v.get("caption") or "", "date": r["valid_from"].isoformat()}
        if (box := image_bbox(url)) is not None:
            item["bbox"] = box
        if (scored := scores.get(v.get("scene_id") or "")) is not None:
            item["changeScore"] = scored[0]
            if scored[1]:
                item["comparedWith"] = scored[1]
            item["changeThreshold"] = CHANGE_THRESHOLD
            if scored[2]:
                item["changeMethod"] = scored[2]
        by_month[month] = item
    return [by_month[m] for m in sorted(by_month)]


def _footprint(raw: Any) -> dict[str, Any] | None:
    shape = json.loads(raw) if isinstance(raw, str) else raw
    return shape if isinstance(shape, dict) and shape.get("type") == "Polygon" else None


def _date_value(row: Row | None, tag: str) -> Doc:
    if row is None or not isinstance(row["value"], str):
        return nd(tag)
    return with_value(row, row["value"][:10])


def imagery_building(conn: Conn, b: Row) -> Doc | None:
    footprint = _footprint(b.get("footprint"))
    if footprint is None:
        return None
    facts = known_facts(conn, "building", b["id"], BUILDING_FACTS)
    doc: Doc = {
        "id": b["id"],
        "label": b["label"] or b["id"],
        "footprint": footprint,
        "status": src_or_nd(facts.get("status"), "F"),
        "constructionStart": _date_value(facts.get("construction_start"), "R"),
        "constructionEnd": _date_value(facts.get("construction_end"), "R"),
    }
    if (stage := building_stage(conn, b["id"])) is not None:
        doc["imageryStage"] = stage
    return doc


def building_stage(conn: Conn, building_id: str) -> Doc | None:
    """types.ts BuildingImageryStage from the building's `imagery_stage` readings (one per capture day, the
    newest row per day): the latest reading, every reading oldest first, and `roofedBy`, the first roofed
    reading (confidence 0.6 or more) from which at least ROOFED_SHARE of the readings are roofed and the
    latest is not bare ground. Optical readings only: never an operating status. None without readings."""
    by_day: dict[str, Row] = {}
    for r in known_fact_rows(conn, "building", building_id, [STAGE_FIELD])[STAGE_FIELD]:
        v = r["value"] if isinstance(r["value"], dict) else {}
        if v.get("stage") in STAGES and r["valid_from"] <= day():
            by_day.setdefault(r["valid_from"].isoformat(), r)
    if not by_day:
        return None
    rows = [by_day[d] for d in sorted(by_day)]

    def conf(r: Row) -> float:
        return round(float(r["confidence"]), 2) if r.get("confidence") is not None else 0.0

    readings = [{"date": r["valid_from"].isoformat(), "stage": r["value"]["stage"], "confidence": conf(r)}
                for r in rows]
    last = rows[-1]
    roofed: Doc = nd("M")
    if last["value"]["stage"] != "bare_ground":
        for i, r in enumerate(rows):
            later = rows[i:]
            if (r["value"]["stage"] == "roofed" and conf(r) >= 0.6
                    and sum(x["value"]["stage"] == "roofed" for x in later) >= ROOFED_SHARE * len(later)):
                roofed = with_value(r, r["valid_from"].isoformat())
                break
    return {
        "latest": with_value(last, {"stage": last["value"]["stage"], "detail": str(last["value"].get("detail") or ""),
                                    "confidence": conf(last)}),
        "roofedBy": roofed,
        "readings": readings,
    }


def naip_image(conn: Conn, subject_type: str, subject_id: str) -> Doc | None:
    """Sourced<NaipImage>: the newest NAIP aerial photo stored for the subject, None when there is none."""
    rows = known_fact_rows(conn, subject_type, subject_id, [NAIP_FIELD])[NAIP_FIELD]
    for r in rows:
        v = r["value"] if isinstance(r["value"], dict) else {}
        tiles = [t for t in v.get("tiles") or [] if isinstance(t, dict) and isinstance(t.get("url"), str)
                 and isinstance(t.get("bbox"), list) and len(t["bbox"]) == 4]
        bbox = v.get("bbox")
        if tiles and isinstance(bbox, list) and len(bbox) == 4 and isinstance(v.get("date"), str):
            return with_value(r, {
                "date": v["date"], "gsd": float(v.get("gsd") or 0.6), "bbox": [float(x) for x in bbox],
                "tiles": [{"url": t["url"], "bbox": [float(x) for x in t["bbox"]]} for t in tiles],
                "caption": str(v.get("caption") or ""),
            })
    return None


def _plan(row: Row | None) -> Doc:
    steps = row["value"] if row and isinstance(row["value"], list) else []
    out = [
        {"date": s["date"][:10], "buildingsOperational": s["buildings_operational"]}
        for s in steps
        if isinstance(s, dict) and isinstance(s.get("date"), str)
        and isinstance(s.get("buildings_operational"), int) and not isinstance(s.get("buildings_operational"), bool)
    ]
    if row is None or not out:
        return nd("R")
    return with_value(row, sorted(out, key=lambda s: s["date"]))


def _read(row: Row | None) -> Doc:
    v = row["value"] if row and isinstance(row["value"], dict) else None
    if row is None or v is None or not isinstance(v.get("read"), str) or not v["read"].strip():
        return nd("M")
    roof = v.get("roof_ha")
    return with_value(row, {
        "date": v["date"] if isinstance(v.get("date"), str) else None,
        "flag": v["flag"] if isinstance(v.get("flag"), str) and v["flag"].strip() else None,
        "text": v["read"].strip(),
        "roofHa": float(roof) if isinstance(roof, int | float) and not isinstance(roof, bool) else None,
    })


def _count(row: Row | None) -> Doc:
    ok = row is not None and isinstance(row["value"], int) and not isinstance(row["value"], bool)
    return src_or_nd(row if ok else None, "R")


def _text(row: Row | None) -> Doc:
    ok = row is not None and isinstance(row["value"], str) and row["value"].strip() != ""
    return src_or_nd(row if ok else None, "R")


def imagery_campus(conn: Conn, campus_id: str) -> Doc | None:
    campus = campus_row(conn, campus_id)
    if campus is None:
        return None
    facts = known_facts(conn, "campus", campus_id, CAMPUS_FACTS)
    buildings = [d for b in campus_buildings(conn, campus_id) if (d := imagery_building(conn, b)) is not None]
    return {
        "id": campus_id,
        "name": campus["name"],
        "lat": campus["lat"],
        "lon": campus["lon"],
        "buildings": buildings,
        "plan": _plan(facts.get("capacity_plan")),
        "operational": _count(facts.get("buildings_operational")),
        "buildingCount": _count(facts.get("building_count")),
        "originalSchedule": _text(facts.get("original_schedule_text")),
        "schedule": _text(facts.get("schedule_text")),
        "constructionNote": _text(facts.get("epoch_construction_note")),
        "read": _read(facts.get("imagery_note")),
    }


def imagery_campuses(conn: Conn, campus_ids: list[str]) -> list[Doc]:
    """One entry per campus (in the order given), for the image markup."""
    return [d for cid in dict.fromkeys(campus_ids) if (d := imagery_campus(conn, cid)) is not None]
