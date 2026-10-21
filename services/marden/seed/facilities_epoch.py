"""Epoch AI 'AI data centers' (CC BY 4.0): campus facts, capacity history and building footprints.

Epoch values are tag R, source 'Epoch AI', source URL the dataset page. Owner, user and project
labels carry Epoch's confidence suffix (#confident / #likely / #speculative) as the fact's
confidence. Timeline rows up to the Epoch download (the dcmap build date) are written as dated
history; later rows are Epoch's projection and are kept in one ``capacity_plan`` fact.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date
from typing import Any

from marden.core.db import Conn
from marden.seed.common import as_of, parse_date, slugify
from marden.seed.facilities_archive import DCMAP, EPOCH_CSV, EPOCH_MAP, EPOCH_TIMELINES, Archive
from marden.seed.facilities_sink import EPOCH_NOTICE, EPOCH_URL, FactSink, clean, confidence, num, usd_bn

_CONF = re.compile(r"\s*#(\w+)\s*$")
_PAUSED = re.compile(r"\b(construction|project|work|development)\s+(paused|halted|suspended)\b", re.I)
_CANCELLED = re.compile(r"\b(project|campus|development)\s+(cancell?ed|abandoned)\b", re.I)
UNDER_WAY_FLAGS = {"on track", "behind schedule", "shell up, check fit-out", "early works"}
BUILT_FLAGS = {"built", "built / expanding"}


def split_conf(text: str) -> tuple[str, str | None]:
    """'Microsoft #confident' -> ('Microsoft', 'confident')."""
    m = _CONF.search(text)
    return (text[: m.start()].strip(), m.group(1).lower()) if m else (text.strip(), None)


def labels(text: str | None) -> list[dict[str, Any]]:
    return [{"name": n, "confidence": c} for n, c in (split_conf(p) for p in (text or "").split(",")) if n]


def names(text: str | None) -> list[str]:
    return [p.strip() for p in (text or "").split(",") if p.strip()]


def _timeline_point(r: dict[str, str]) -> dict[str, Any]:
    return {"date": r["Date"], "facility_mw": num(r["Power (MW)"]), "it_mw": num(r["IT power (MW)"]),
            "buildings_operational": num(r["Buildings operational"]),
            "h100_equivalents": num(r["H100 equivalents"])}


def write_epoch_campus(sink: FactSink, arc: Archive, name: str, cid: str, row: dict[str, Any]) -> None:
    """Every Epoch fact for one data center (CSV row, timelines, dcmap Epoch copy, map facts)."""
    ep = row.get("ep") or {}
    csv_row = arc.epoch_rows.get(name)
    rec_day = as_of(ep.get("status_d"), arc.sources[EPOCH_CSV].as_of)
    add = sink.add
    if csv_row:
        s = arc.src(EPOCH_CSV, rec_day)
        owner, oc = split_conf(csv_row["Owner"]) if csv_row["Owner"] else ("", None)
        add("campus", cid, "owner", owner, src=s, url=EPOCH_URL, confidence=confidence(oc))
        if not row.get("cov") and owner:  # spec 14: developer = the owning parent; Epoch lists the owner
            add("campus", cid, "developer", owner, src=s, url=EPOCH_URL, rule=True,
                confidence=confidence(oc), source_name="Epoch AI")
        users = labels(csv_row["Users"])
        confs = [c for u in users if (c := confidence(u["confidence"])) is not None]
        add("campus", cid, "users", users, src=s, url=EPOCH_URL, confidence=min(confs) if confs else None)
        project, pc = split_conf(csv_row["Project"]) if csv_row["Project"] else ("", None)
        add("campus", cid, "project_name", project, src=s, url=EPOCH_URL, confidence=confidence(pc))
        add("campus", cid, "investors", names(csv_row["Investors"]), src=s, url=EPOCH_URL)
        add("campus", cid, "construction_companies", names(csv_row["Construction companies"]), src=s, url=EPOCH_URL)
        add("campus", cid, "energy_companies", names(csv_row["Energy companies"]), src=s, url=EPOCH_URL)
        add("campus", cid, "country", csv_row["Country"], src=s, url=EPOCH_URL)
        add("campus", cid, "address", csv_row["Address"], src=s, url=EPOCH_URL)
        add("campus", cid, "capex_usd", usd_bn(csv_row["Current total capital cost (2025 USD billions)"]),
            src=s, url=EPOCH_URL, unit="USD")
        add("campus", cid, "chip_types", names(csv_row["Current chip types"]), src=s, url=EPOCH_URL)
        add("campus", cid, "copyright_notice", EPOCH_NOTICE, src=s, url=EPOCH_URL)
    dsrc = arc.src(DCMAP, rec_day)
    add("campus", cid, "chips", ep.get("chips"), src=dsrc, url=EPOCH_URL, source_name="Epoch AI", unit="count")
    add("campus", cid, "chips_planned", ep.get("chips_plan"), src=dsrc, url=EPOCH_URL, source_name="Epoch AI",
        unit="count")
    add("campus", cid, "epoch_status", clean(ep.get("status")), src=dsrc, url=EPOCH_URL, source_name="Epoch AI")
    _timeline_facts(sink, arc, name, cid, rec_day)
    dc = arc.map_dcs.get(name)
    if dc is not None:
        msrc = arc.src(EPOCH_MAP, _map_day(arc, dc))
        op = (dc.get("facts") or {}).get("operational")
        if isinstance(op, bool):
            add("campus", cid, "epoch_operational", op, src=msrc, url=EPOCH_URL)
        kinds = Counter((f.get("properties") or {}).get("type") for f in (dc.get("shapes") or {}).get("features", []))
        add("campus", cid, "backup_generator_count", kinds.get("Backup generator"), src=msrc, url=EPOCH_URL,
            rule=True, unit="count", source_name="Epoch AI")
        add("campus", cid, "substation_count", kinds.get("Power substation"), src=msrc, url=EPOCH_URL,
            rule=True, unit="count", source_name="Epoch AI")


def _timeline_facts(sink: FactSink, arc: Archive, name: str, cid: str, rec_day: date) -> None:
    rows = arc.timelines.get(name, [])
    cutoff = arc.sources[EPOCH_TIMELINES].as_of
    past = [r for r in rows if (parse_date(r["Date"]) or date.max) <= cutoff]
    future = [r for r in rows if (parse_date(r["Date"]) or date.min) > cutoff]
    for r in past:
        s = arc.src(EPOCH_TIMELINES, r["Date"])
        sink.add("campus", cid, "operating_mw", num(r["Power (MW)"]), src=s, url=EPOCH_URL, unit="MW")
        sink.add("campus", cid, "operating_it_mw", num(r["IT power (MW)"]), src=s, url=EPOCH_URL, unit="MW")
        sink.add("campus", cid, "buildings_operational", num(r["Buildings operational"]), src=s, url=EPOCH_URL,
                 unit="count")
        sink.add("campus", cid, "h100_equivalents", num(r["H100 equivalents"]), src=s, url=EPOCH_URL, unit="count")
    s = arc.src(EPOCH_TIMELINES, rec_day)
    if past:
        sink.add("campus", cid, "epoch_construction_note", clean(past[-1]["Construction status"]),
                 src=arc.src(EPOCH_TIMELINES, past[-1]["Date"]), url=EPOCH_URL)
    if future:
        sink.add("campus", cid, "capacity_plan", [_timeline_point(r) for r in future], src=s, url=EPOCH_URL)
    if rows:  # Epoch's facility and IT power at its last timeline date (full build-out estimate)
        last = rows[-1]
        sink.add("campus", cid, "facility_mw", num(last["Power (MW)"]), src=s, url=EPOCH_URL, unit="MW")
        sink.add("campus", cid, "it_mw", num(last["IT power (MW)"]), src=s, url=EPOCH_URL, unit="MW")
    first = next((r for r in rows if (num(r["Power (MW)"]) or 0) > 0), None)
    if first:
        d = parse_date(first["Date"])
        if d:
            sink.add("campus", cid, "online_year", d.year, src=s, url=EPOCH_URL, unit="year")
            sink.add("campus", cid, "online_year_basis", "observed" if d <= cutoff else "projected", src=s,
                     url=EPOCH_URL)


def _map_day(arc: Archive, dc: dict[str, Any]) -> date:
    cutoff = arc.sources[EPOCH_MAP].as_of
    days = [d for t in dc.get("timeline") or [] if (d := parse_date(t.get("imageryDate") or t.get("date")))]
    past = [d for d in days if d <= cutoff]
    return max(past) if past else cutoff


def campus_status(arc: Archive, row: dict[str, Any]) -> str | None:
    """types.ts DcStatus by fixed rule (M): Cancelled / Paused when the record says the project
    was cancelled or construction paused; Operating when Epoch marks it operational, its timeline
    shows power before the download, or the archive records delivered MW; Under construction when
    Epoch shows a dated build without power, or the archive's imagery flag says works are under way;
    Planned when only a schedule is known; n/d otherwise."""
    ep = row.get("ep") or {}
    name = ep.get("name")
    text = " ".join(str(x or "") for x in (ep.get("status"), row.get("pwr_status")))
    if _CANCELLED.search(text):
        return "Cancelled"
    if _PAUSED.search(text):
        return "Paused"
    cutoff = arc.sources[EPOCH_TIMELINES].as_of
    past = [r for r in arc.timelines.get(name or "", []) if (parse_date(r["Date"]) or date.max) <= cutoff]
    operational = ((arc.map_dcs.get(name or "") or {}).get("facts") or {}).get("operational")
    flag = ((row.get("s2") or {}).get("txt") or "").strip().lower()
    if operational is True or any((num(r["Power (MW)"]) or 0) > 0 for r in past) or (num(row.get("deliv_mw")) or 0) > 0:
        return "Operating"
    if past or operational is False or flag in UNDER_WAY_FLAGS or flag in BUILT_FLAGS:
        return "Under construction"
    if row.get("sched") or row.get("orig"):
        return "Planned"
    return None


# ---------------------------------------------------------------- buildings
def _ring(geometry: dict[str, Any]) -> list[list[float]] | None:
    """The outer ring of a Polygon, or a LineString outline closed into a ring (Epoch draws a few
    footprints as open outlines whose last point sits on the first)."""
    if geometry.get("type") == "Polygon":
        return geometry["coordinates"][0]
    if geometry.get("type") == "LineString":
        pts = [list(p) for p in geometry["coordinates"]]
        if len(pts) >= 3 and pts[0] != pts[-1]:
            pts.append(pts[0])
        return pts
    return None


def write_buildings(conn: Conn, sink: FactSink, arc: Archive, name: str, cid: str) -> int:
    dc = arc.map_dcs.get(name)
    if dc is None:
        return 0
    msrc = arc.src(EPOCH_MAP, _map_day(arc, dc))
    n = 0
    for f in (dc.get("shapes") or {}).get("features", []):
        p = f.get("properties") or {}
        if p.get("type") != "Building":
            continue
        label = str(p.get("buildingNumber") or "").strip()
        if not label:
            sink.counts[("skipped", "building_without_label")] += 1
            continue
        bid = f"{cid}-b{slugify(label)}"
        ring = _ring(f.get("geometry") or {})
        geo = json.dumps({"type": "Polygon", "coordinates": [ring]}) if ring and len(ring) >= 4 else None
        conn.execute(
            """INSERT INTO building (id, campus_id, label, footprint)
               VALUES (%s, %s, %s, CASE WHEN %s::text IS NULL THEN NULL
                                        ELSE ST_SetSRID(ST_GeomFromGeoJSON(%s::text), 4326) END)
               ON CONFLICT (id) DO UPDATE SET campus_id = EXCLUDED.campus_id, label = EXCLUDED.label,
                 footprint = EXCLUDED.footprint""",
            (bid, cid, f"Building {label}", geo, geo),
        )
        n += 1
        _building_facts(sink, msrc, bid, p)
    return n


def _building_facts(sink: FactSink, msrc: Any, bid: str, p: dict[str, Any]) -> None:
    start, end = parse_date(p.get("startDate")), parse_date(p.get("endDate"))
    complete: bool | None = None
    if p.get("complete") in (0, 1):
        complete = bool(p["complete"])
    elif end is not None:
        complete = end <= msrc.as_of
    elif start is not None:
        complete = False
    add = sink.add
    add("building", bid, "construction_complete", complete, src=msrc, url=EPOCH_URL)
    if complete is not None:  # DcStatus by rule from Epoch's footprint annotation
        add("building", bid, "status", "Operating" if complete else "Under construction", src=msrc,
            url=EPOCH_URL, rule=True, source_name="Epoch AI")
    if start and start <= msrc.as_of:
        add("building", bid, "construction_start", start.isoformat(), src=msrc, url=EPOCH_URL, valid_from=start)
    if end and end <= msrc.as_of:
        add("building", bid, "construction_end", end.isoformat(), src=msrc, url=EPOCH_URL, valid_from=end)
    add("building", bid, "alt_label", p.get("buildingPromo") or p.get("Building_promo"), src=msrc, url=EPOCH_URL)
    add("building", bid, "footprint_area_m2", num(p.get("area_m2")), src=msrc, url=EPOCH_URL, unit="m2")
    add("building", bid, "copyright_notice", EPOCH_NOTICE, src=msrc, url=EPOCH_URL)
