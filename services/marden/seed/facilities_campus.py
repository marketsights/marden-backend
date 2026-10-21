"""Campus rows (all 102 dcmap rows) and the campus facts taken from the archive's own research.

Epoch AI values are written by ``facilities_epoch``; this module covers dcmap research fields,
pw_found power lookups, gpu/site_power (covered campuses) and sqft.json.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from marden.core.db import Conn
from marden.seed.common import as_of, parse_date
from marden.seed.facilities_archive import (
    DCMAP, EPOCH_TIMELINES, PW_FOUND, SITE_POWER, SQFT, Archive,
)
from marden.seed.facilities_geo import County, county_at, state_code
from marden.seed.facilities_sink import FactSink, confidence, first_url, is_nd, num

SQFT_FILED = {"permit", "filing", "county"}  # dictionary 3.2: permit/filing/county -> F; company/press -> R
POWER_SOURCE = {"grid": "Grid", "on-site + grid": "Hybrid"}
_GAS = re.compile(r"\b(gas|turbines?|gensets?|reciprocating)\b", re.I)
GAS_PLANT_TYPES = {"gas plant"}


@dataclass(frozen=True)
class CampusRecord:
    id: str
    key: str
    name: str
    aliases: tuple[str, ...]
    state: str | None
    county: County | None
    lon: float | None
    lat: float | None
    first_seen: date
    country: str | None


def _first_seen(arc: Archive, row: dict[str, Any]) -> date:
    """Earliest dated observation of the campus in the archive (Epoch timeline, Sentinel-2
    scenes, sqft records), else the dcmap build date."""
    dates: list[date] = []
    ep = row.get("ep") or {}
    for r in arc.timelines.get(ep.get("name") or "", []):
        d = parse_date(r["Date"])
        if d:
            dates.append(d)
    for img in (row.get("s2") or {}).get("imgs") or []:
        d = parse_date(img[0] if img else None)
        if d:
            dates.append(d)
    for s in arc.sqft.get(row["key"], []):
        d = parse_date(s.get("asof"))
        if d:
            dates.append(d)
    return min([as_of(d) for d in dates] or [arc.sources[DCMAP].as_of])


def campus_record(arc: Archive, row: dict[str, Any], counties: list[County]) -> CampusRecord:
    ep = row.get("ep") or {}
    lat, lon = num(row.get("lat")), num(row.get("lon"))
    code = state_code(row.get("state")) or state_code(ep.get("state"))
    us = code is not None or ep.get("country") == "United States"
    county = county_at(counties, lon, lat) if us else None
    if county is not None and code is not None and county.state != code:
        county = None  # the point falls outside the stated state: no county rather than a wrong one
    state = code or (county.state if county else None) or (row.get("state") or ep.get("state") or None)
    if state == "United States":
        state = None
    aliases = tuple(a for a in [ep.get("name")] if a and a != row["name"])
    country = ep.get("country") or ("United States" if code else None)
    return CampusRecord(arc.campus_of_key[row["key"]], row["key"], row["name"], aliases, state, county,
                        lon, lat, _first_seen(arc, row), country)


def upsert_campus(conn: Conn, rec: CampusRecord) -> None:
    conn.execute(
        """INSERT INTO campus (id, name, aliases, county, state, geom, first_seen_at)
           VALUES (%(id)s, %(name)s, %(aliases)s, %(county)s, %(state)s,
                   CASE WHEN %(lon)s::float8 IS NULL OR %(lat)s::float8 IS NULL THEN NULL
                        ELSE ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) END,
                   %(first)s::date)
           ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, aliases = EXCLUDED.aliases,
             county = EXCLUDED.county, state = EXCLUDED.state, geom = EXCLUDED.geom,
             first_seen_at = least(campus.first_seen_at, EXCLUDED.first_seen_at)""",
        {"id": rec.id, "name": rec.name, "aliases": list(rec.aliases),
         "county": rec.county.name if rec.county else None, "state": rec.state,
         "lon": rec.lon, "lat": rec.lat, "first": rec.first_seen},
    )


def power_source(row: dict[str, Any], arc: Archive, pw_found: dict[str, Any] | None) -> str | None:
    """types.ts PowerSource from the archive's power class (rule): Grid, Hybrid (on-site + grid),
    On-site gas when on-site generation is gas-fired by the record's own text or its case-linked
    plants, Unknown when the fuel is not stated; n/d when the class is 'not disclosed'."""
    text = (row.get("psrc") or (pw_found or {}).get("source") or "").strip().lower()
    if text in POWER_SOURCE:
        return POWER_SOURCE[text]
    if text != "on-site generation":
        return None
    prose = " ".join(str(x or "") for x in (row.get("pwr_detail"), row.get("pwr_status"),
                                            (pw_found or {}).get("detail")))
    linked = [arc.projects.get(p["id"], {}).get("ty") for p in row.get("pw") or []] if row.get("cov") else []
    return "On-site gas" if _GAS.search(prose) or GAS_PLANT_TYPES & set(linked) else "Unknown"


def write_campus_facts(sink: FactSink, arc: Archive, row: dict[str, Any], rec: CampusRecord) -> None:
    src = arc.sources[DCMAP]
    cid = rec.id
    add = sink.add
    if rec.county is not None:
        add("campus", cid, "county", {"name": rec.county.name, "fips": rec.county.fips, "state": rec.county.state},
            src=arc.sources["aiwc/q3x/geo/us-atlas-3.0.1/counties-10m.json"], rule=True, valid_from=src.as_of)
    if rec.state and not state_code(row.get("state")) and rec.county and rec.state == rec.county.state:
        add("campus", cid, "state", rec.state, src=src, rule=True)  # from the county the point falls in
    in_epoch = (row.get("ep") or {}).get("name") in arc.timelines
    add("campus", cid, "first_seen", rec.first_seen.isoformat(),
        src=arc.sources[EPOCH_TIMELINES] if in_epoch else src, rule=True, valid_from=src.as_of)
    if not row.get("ep") and rec.country:
        add("campus", cid, "country", rec.country, src=src, rule=True)  # from the US state code
    pwf = arc.data[PW_FOUND].get(row["key"])
    ps = power_source(row, arc, pwf)
    add("campus", cid, "power_source", ps, src=src, rule=True)
    if row.get("cov"):
        _covered_facts(sink, arc, row, cid)
    else:
        add("campus", cid, "power_source_text", row.get("psrc"), src=arc.sources[PW_FOUND] if pwf else src,
            url=(pwf or {}).get("url"), confidence=confidence((pwf or {}).get("confidence")))
    if pwf:
        s, url, conf = arc.sources[PW_FOUND], pwf.get("url"), confidence(pwf.get("confidence"))
        add("campus", cid, "utility", pwf.get("utility"), src=s, url=url, confidence=conf)
        add("campus", cid, "iso", pwf.get("iso"), src=s, url=url, confidence=conf)
        add("campus", cid, "power_detail", pwf.get("detail"), src=s, url=url, confidence=conf)
        add("campus", cid, "grid_mw", num(pwf.get("mw")), src=s, url=url, unit="MW", confidence=conf)
    for rec_sq in arc.sqft.get(row["key"], []):
        s = arc.src(SQFT, rec_sq.get("asof"))
        url = rec_sq.get("src")
        tag = "F" if rec_sq.get("src_kind") in SQFT_FILED else "R"
        add("campus", cid, "sqft_total", num(rec_sq.get("sqft_total")), src=s, url=url, tag=tag, unit="sqft")
        add("campus", cid, "building_count", num(rec_sq.get("buildings")), src=s, url=url, tag=tag, unit="count")
        add("campus", cid, "acres", num(rec_sq.get("acres")), src=s, url=url, tag=tag, unit="acres")
        add("campus", cid, "sqft_note", rec_sq.get("sqft_note"), src=s, url=url, tag=tag)


def _covered_facts(sink: FactSink, arc: Archive, row: dict[str, Any], cid: str) -> None:
    """Hand-researched fields of the 21 covered rows (no per-field URL: the archive is the source)."""
    src = arc.sources[DCMAP]
    add = sink.add
    add("campus", cid, "location_text", row.get("loc"), src=src)
    add("campus", cid, "tenant", row.get("tenant"), src=src)
    if not row.get("ep"):
        add("campus", cid, "owner", row.get("owner"), src=src)
        if not is_nd(row.get("users")):
            add("campus", cid, "users", [{"name": row["users"], "confidence": None}], src=src)
    add("campus", cid, "facility_mw", num(row.get("gross_mw")), src=src, unit="MW")
    add("campus", cid, "planned_it_mw", num(row.get("it_mw_plan")), src=src, unit="MW")
    add("campus", cid, "delivered_mw", num(row.get("deliv_mw")), src=src, unit="MW")
    add("campus", cid, "schedule_text", row.get("sched"), src=src)
    add("campus", cid, "original_schedule_text", row.get("orig"), src=src)
    add("campus", cid, "power_status", row.get("pwr_status"), src=src)
    add("campus", cid, "power_detail", row.get("pwr_detail"), src=src)
    add("campus", cid, "power_source_text", row.get("psrc"), src=src)
    s2 = row.get("s2") or {}
    if s2.get("read") or s2.get("txt"):
        add("campus", cid, "imagery_note", {"date": s2.get("d"), "read": s2.get("read"), "flag": s2.get("txt"),
                                            "roof_ha": s2.get("last_ha")},
            src=src, judged=True, valid_from=as_of(s2.get("d"), src.as_of))
    case = row.get("case")
    if case:
        _case_power(sink, arc, row, cid, case)
        _developer(sink, arc, cid, case)


def _case_power(sink: FactSink, arc: Archive, row: dict[str, Any], cid: str, case: str) -> None:
    sp = ((arc.data[SITE_POWER].get("projects") or {}).get(case) or {}).get("site_power") or {}
    s = arc.sources[SITE_POWER]
    url = first_url(sp.get("src"))
    if sp:
        sink.add("campus", cid, "utility", sp.get("utility"), src=s, url=url)
        sink.add("campus", cid, "iso", sp.get("iso"), src=s, url=url)
        sink.add("campus", cid, "onsite_generation", sp.get("onsite_generation"), src=s, url=url)
        return
    lead = (row.get("pw") or [None])[0]
    if lead:  # the case-linked power project copied into dcmap (power.json fields)
        sink.add("campus", cid, "utility", lead.get("ut"), src=arc.sources[DCMAP])
        sink.add("campus", cid, "iso", lead.get("iso"), src=arc.sources[DCMAP])


def _developer(sink: FactSink, arc: Archive, cid: str, case: str) -> None:
    """The case dossier's Developer party (else its Sponsor), tagged by the party's basis."""
    memo = arc.memo(case) or {}
    chain = memo.get("chain") or []
    party = next((c for c in chain if c.get("role") == "Developer"), None) or next(
        (c for c in chain if c.get("role") == "Sponsor"), None)
    if party and party.get("name"):
        sink.add("campus", cid, "developer", party["name"], src=arc.sources[f"aiwc/memo/{case}.json"],
                 basis=party.get("basis") or "reported")
