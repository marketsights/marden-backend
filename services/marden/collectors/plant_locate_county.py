"""A power plant's county for `plant_locate`: from its point (the Census county polygon), else from its
interconnection queue entries. Each function returns (county name, FactWriter.add kwargs with `value`) or None.

- From the point: the `us_county` polygon (census_counties, 2025 vintage at 1:5,000,000) containing it, when that
  polygon's state is the plant's state. Tag R, source the Census file (URL and document in us_county.meta),
  method COUNTY_METHOD, valid_from = the point's record date. When EIA states the county of the plant the point
  came from and the polygon disagrees (generalized boundaries near a county line), EIA's record is kept: tag F,
  source EIA-860M, quote 'County <name>' from the workbook row.
- From the queue: every entry named in the plant's `queue_ids` fact is in `queue_project` with a county and
  state, all name the same county, in the plant's state (a queue id found in two queues is not used). Tag F, the
  queue's source (ERCOT GIS report, PJM queue) and document, quote = that entry's id and County cell
  ('28INR0100 | County Ellis'), method QUEUE_METHOD. Used only for plants without a point.
"""

from __future__ import annotations

from typing import Any

from marden.collectors.eia_860m_store import word_list
from marden.core.db import Conn, Row

COUNTY_METHOD = "point-in-county-polygon@0.1"
QUEUE_METHOD = "plant-locate-queue@0.1"
COUNTY_SOURCE = "US Census Bureau cartographic boundary files"
SRC_KEYS = ("source_name", "source_url", "document_id", "valid_from", "method_version")

Found = tuple[str, dict[str, Any]]


def norm(name: str | None) -> str:
    """'DeWitt' / 'De Witt County' / 'Doña Ana' -> 'de witt' / 'de witt' / 'dona ana'."""
    return " ".join(w for w in word_list(name or "") if w not in ("county", "parish", "borough"))


def polygons_at(conn: Conn, lat: float, lon: float) -> list[Row]:
    return conn.execute(
        """SELECT geoid, stusps, name, namelsad, meta FROM us_county
            WHERE ST_Contains(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326)) ORDER BY geoid""",
        (lon, lat),
    ).fetchall()


def fips_of(conn: Conn, state: str, county: str) -> str | None:
    rows = conn.execute("SELECT geoid, name FROM us_county WHERE stusps = %s", (state,)).fetchall()
    return next((r["geoid"] for r in rows if norm(r["name"]) == norm(county)), None)


def from_point(conn: Conn, point: tuple[float, float], state: str | None, src: dict[str, Any],
               eia_county: str | None) -> tuple[Found | None, str]:
    """((county, fact), outcome) for a point; outcome names what happened (for the run's counts)."""
    lat, lon = point
    found = polygons_at(conn, lat, lon)
    if len(found) != 1 or (state and found[0]["stusps"] != state.strip().upper()):
        return None, "county_polygon_missing_or_other_state"
    c, meta = found[0], found[0]["meta"] or {}
    if eia_county and norm(eia_county) != norm(c["name"]):
        fips = fips_of(conn, c["stusps"], eia_county)
        name = next((r["name"] for r in conn.execute("SELECT name FROM us_county WHERE geoid = %s", (fips,))),
                    eia_county) if fips else eia_county
        return (name, {
            **{k: src[k] for k in SRC_KEYS}, "tag": "F", "quote": f"County {eia_county}",
            "value": {"name": name, "state": c["stusps"], **({"fips": fips} if fips else {})},
            "quote_loc": f"{src['quote_loc'].split(';')[0]}; the county polygon at the point is {c['namelsad']} "
                         f"({c['geoid']}): EIA's record is kept",
        }), "county_eia_record_over_polygon"
    return (c["name"], {
        "value": {"name": c["name"], "fips": c["geoid"], "state": c["stusps"]}, "tag": "R",
        "source_name": COUNTY_SOURCE, "source_url": meta.get("source_url"), "document_id": meta.get("document_id"),
        "valid_from": src["valid_from"], "method_version": COUNTY_METHOD,
        "quote_loc": f"point in county polygon: ({lat}, {lon}) in {c['namelsad']}, {c['stusps']} (us_county "
                     f"{c['geoid']}, {meta.get('layer')})",
    }), "county_polygon" + ("_agrees_with_eia" if eia_county else "")


def from_queue(conn: Conn, state: str | None, queue_ids: Row | None) -> Found | None:
    ids = queue_ids["value"] if queue_ids else None
    if not isinstance(ids, list) or not ids:
        return None
    rows = conn.execute(
        """SELECT * FROM fact_current WHERE subject_type = 'queue_project' AND field IN ('county', 'state')
             AND subject_id IN (SELECT id FROM queue_project WHERE split_part(id, ':', 2) = ANY(%s))""",
        ([str(i) for i in ids],),
    ).fetchall()
    by_entry: dict[str, dict[str, Row]] = {}
    for r in rows:
        by_entry.setdefault(r["subject_id"], {})[r["field"]] = r
    st = (state or "").strip().upper()
    # every named entry found exactly once (a queue id in two ISOs' queues is not one entry), each with both
    found = sorted(e.split(":", 1)[1] for e in by_entry)
    if found != sorted({str(i) for i in ids}) or any(not {"county", "state"} <= set(e) for e in by_entry.values()):
        return None
    named = {(norm(e["county"]["value"]), e["state"]["value"]) for e in by_entry.values()}
    if len(named) != 1 or next(iter(named))[1] != st:
        return None
    entries = sorted(by_entry)
    src_entry = max(entries, key=lambda e: (by_entry[e]["county"]["valid_from"], by_entry[e]["county"]["id"]))
    src = by_entry[src_entry]["county"]
    county = src["value"]
    fips = fips_of(conn, st, county)
    return county, {
        "value": {"name": county, "state": st, **({"fips": fips} if fips else {})}, "tag": "F",
        "source_name": src["source_name"], "source_url": src["source_url"], "document_id": src["document_id"],
        "valid_from": src["valid_from"], "method_version": QUEUE_METHOD,
        "quote": src["quote"] or f"{src_entry.split(':', 1)[1]} | County {county}",
        "quote_loc": f"interconnection queue {', '.join(entries)}: county '{county}', state '{st}' (the plant's "
                     f"queue_ids fact {queue_ids['id'] if queue_ids else ''}); the plant has no point",
    }
