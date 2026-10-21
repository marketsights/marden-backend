"""ERCOT load zone of every Texas site, campus and plant in ERCOT -> fact `ercot_load_zone` (tag M).

Run as the first step of `ercot_prices`, so a site's zone and the zone's prices (`power_price:ercot:<zone>:dam`
and `:rt`) sit side by side for the stress model.
- Subjects: site, campus and power_plant rows with state 'TX' whose market facts put them in ERCOT: the first of
  `iso`, `grid_region`, `power_iso` (capacity_zones.MARKET_FIELDS) that names a market must name ERCOT alone
  (grid_join writes 'ERCOT' from the HIFLD boundary; "SPP (not ERCOT)" names SPP). Texas campuses served by
  SPP or El Paso Electric, and subjects whose facts name no market, get no zone; ERCOT and SPP overlap on the
  map, so the map alone never decides membership.
- Location: the subject's point; a plant without one takes the point of the single campus it serves
  (plant_serves: on-site generation); else the polygon (us_county) of the one Texas county the row names.
- Zone: what ERCOT's Load Zone Map shows there (ercot_zone_map: within 2 km of the point, or over the county
  grown by 2 km). Several zones there, gray, or no location leave the subject unassigned; the run meta lists
  each with the reason and nothing is guessed.
Facts: field `ercot_load_zone` = 'LZ_WEST' etc. (the price series' point), tag M, producer
'model:ercot_load_zones', METHOD_VERSION, source ERCOT with the map URL and its stored document, quote_loc the
pixel evidence; valid_from = the later of the map's posting date and the market fact's date, so reruns write
nothing new.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from marden.collectors import ercot_zone_map as zmap
from marden.collectors.capacity_zones import MARKET_FIELDS, markets_in
from marden.collectors.rates_common import ParseError, RunStats
from marden.core import db, http
from marden.core.db import Conn
from marden.core.facts import FactWriter
from marden.core.http import Fetcher

FIELD = "ercot_load_zone"
PRODUCER = "model:ercot_load_zones"
METHOD_VERSION = "ercot_load_zone_map-0.1"
SUBJECT_TABLES = (("site", "site", "slug"), ("campus", "campus", "id"), ("plant", "power_plant", "id"))
_COUNTY = re.compile(r"^\s*([A-Za-z][A-Za-z .'-]*?)(?:\s+county)?\s*$", re.I)


@dataclass
class Subject:
    stype: str
    sid: str
    county: str | None
    x: float | None
    y: float | None
    market_text: str | None = None
    market_from: date | None = None
    located_by: str = ""


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_text(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_text(v) for v in value)
    return "" if value is None else str(value)


def in_ercot(facts: dict[str, dict[str, Any]]) -> tuple[bool, str | None, date | None]:
    """(in ERCOT, the deciding text, its date): the first market field that names a market decides."""
    for name in MARKET_FIELDS:
        row = facts.get(name)
        found = markets_in(_text(row["value"])) if row else set()
        if found:
            return found == {"ercot"}, f"{name}={_text(row['value'])!r}", row["valid_from"]
    return False, None, None


def texas_subjects(conn: Conn) -> list[Subject]:
    out = []
    for stype, table, key in SUBJECT_TABLES:
        rows = conn.execute(
            f"""SELECT {key} AS id, county, ST_X(ST_Transform(geom, {zmap.MAP_SRID})) AS x,
                       ST_Y(ST_Transform(geom, {zmap.MAP_SRID})) AS y
                  FROM {table} WHERE upper(trim(state)) = 'TX' ORDER BY {key}""").fetchall()
        out += [Subject(stype, r["id"], r["county"], r["x"], r["y"], located_by=f"{stype}.geom" if r["x"] else "")
                for r in rows]
    return out


def _market_facts(conn: Conn) -> dict[tuple[str, str], dict[str, dict[str, Any]]]:
    rows = conn.execute("""SELECT subject_type, subject_id, field, value, valid_from FROM fact_current
                            WHERE subject_type IN ('site', 'campus', 'plant') AND field = ANY(%s)""",
                        (list(MARKET_FIELDS),)).fetchall()
    out: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for r in rows:
        out.setdefault((r["subject_type"], r["subject_id"]), {})[r["field"]] = r
    return out


def _served_campus_point(conn: Conn, plant_id: str) -> tuple[str, float, float] | None:
    rows = conn.execute(
        f"""SELECT c.id, ST_X(ST_Transform(c.geom, {zmap.MAP_SRID})) AS x, ST_Y(ST_Transform(c.geom, {zmap.MAP_SRID}))
                   AS y FROM plant_serves s JOIN campus c ON c.id = s.campus_id WHERE s.plant_id = %s""",
        (plant_id,)).fetchall()
    if len(rows) != 1 or rows[0]["x"] is None:
        return None
    return rows[0]["id"], rows[0]["x"], rows[0]["y"]


def _county_rings(conn: Conn, county: str | None) -> tuple[str, list[list[tuple[float, float]]]] | None:
    m = _COUNTY.match(county or "")
    if not m:
        return None  # several counties, a note, or none
    row = conn.execute(
        f"""SELECT geoid, namelsad, ST_AsGeoJSON(ST_Transform(geom, {zmap.MAP_SRID})) AS g FROM us_county
             WHERE stusps = 'TX' AND lower(name) = lower(%s)""", (m[1].strip(),)).fetchone()
    if row is None:
        return None
    g = json.loads(row["g"])
    polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
    return f"{row['namelsad']} (us_county {row['geoid']})", [[tuple(p) for p in poly[0]] for poly in polys]


def map_document(stats: RunStats, source_id: str, fetcher: Fetcher) -> tuple[int, Any]:
    """The stored map (fetched raw-first, conditional GET) and its pixels; a different map is refused."""
    doc = http.fetch_document(source_id, zmap.MAP_URL, fetcher=fetcher, doc_type="load_zone_map",
                              title="ERCOT Load Zone Map", doc_date=zmap.MAP_POSTED)
    stats.document(doc)
    if doc.sha256 != zmap.MAP_SHA256:
        raise ParseError(f"ERCOT changed its Load Zone Map (sha256 {doc.sha256[:12]}..., document {doc.id}); "
                         "refit ercot_zone_map.AFFINE with fit_affine before mapping zones")
    return doc.id, zmap.decode(doc.read())


def assign(stats: RunStats, source_id: str, fetcher: Fetcher) -> None:
    doc_id, img = map_document(stats, source_id, fetcher)
    out: dict[str, Any] = {"assigned": {}, "unassigned": {}, "not_in_ercot": {}}
    stats.extra["ercot_load_zones"] = out
    with db.transaction() as conn:
        markets = _market_facts(conn)
        facts = FactWriter(conn)
        for s in texas_subjects(conn):
            key = f"{s.stype}:{s.sid}"
            ok, s.market_text, s.market_from = in_ercot(markets.get((s.stype, s.sid), {}))
            if not ok:
                out["not_in_ercot"][key] = s.market_text or "no market fact"
                continue
            reading, where = _read(conn, img, s)
            if reading is None:
                out["unassigned"][key] = where
                continue
            zone, why = reading.zone()
            if zone is None:
                out["unassigned"][key] = f"{why} ({where})"
                continue
            loc = (f"ERCOT Load Zone Map pixel {reading.pixel[0]},{reading.pixel[1]} ({where}; EPSG:{zmap.MAP_SRID} "
                   f"affine fit to county lines, median residual {zmap.FIT_RESIDUAL_PX} px): {why} "
                   f"({reading.counts.get(zone, 0)} px, {int(zmap.MARGIN_M)} m margin); in ERCOT per {s.market_text}")
            valid_from = max(zmap.MAP_POSTED, s.market_from or zmap.MAP_POSTED)
            res = facts.add(s.stype, s.sid, FIELD, zone, tag="M", source_name="ERCOT", source_url=zmap.MAP_URL,
                            valid_from=valid_from, producer=PRODUCER, status="published", document_id=doc_id,
                            quote_loc=loc, method_version=METHOD_VERSION)
            stats.facts_seen += 1
            stats.facts_new += int(res.created)
            if res.created:
                stats.new_by_series[FIELD] = stats.new_by_series.get(FIELD, 0) + 1
            out["assigned"][key] = zone


def _read(conn: Conn, img: Any, s: Subject) -> tuple[zmap.Reading | None, str]:
    if s.x is not None and s.y is not None:
        return zmap.at_point(img, s.x, s.y), f"point of {s.located_by}"
    if s.stype == "plant" and (served := _served_campus_point(conn, s.sid)):
        return zmap.at_point(img, served[1], served[2]), f"point of campus {served[0]} it serves (plant_serves)"
    county = _county_rings(conn, s.county)
    if county:
        return zmap.over_polygon(img, county[1]), f"county {county[0]}"
    return None, "no point, no served campus with a point, and no single Texas county named"
