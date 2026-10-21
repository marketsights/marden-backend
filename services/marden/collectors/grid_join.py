"""Spatial join: every campus, building, site and power plant -> interconnection, ISO/RTO, balancing authority, utility.

Facts (tag F; the boundary file is the source): `interconnection` ('Eastern' | 'Western' | 'ERCOT'), `iso`
('ERCOT', 'PJM', 'MISO', 'SPP', 'CAISO', 'NYISO', 'ISO-NE' or 'non-RTO'), `balancing_authority` (EIA-930 code,
e.g. 'ERCO'; the HIFLD name when EIA publishes no code), `utility` (the retail service territory's name as HIFLD
publishes it) and `utility_territories` (every territory containing the point, sorted). source_name 'HIFLD',
source_url and document_id = the layer's GeoPackage, valid_from = the layer's HIFLD Open 'modified' date,
quote = the matched feature's published name, quote_loc = 'spatial join: ...' (the point used, layer, version,
feature).

Points: the campus, site and plant point; a building's footprint (point on surface), else its campus point.

Rules (a conflict is flagged, never silently resolved):
- exactly one boundary contains the point -> published;
- interconnection, ISO or BA boundaries overlap -> each is a candidate fact, with one review item for the
  subject and field (a person picks);
- retail territories overlap (HIFLD draws many at county resolution, so co-ops, municipals and IOUs overlap) ->
  no `utility` fact; `utility_territories` lists them until a filing names the serving utility;
- none -> no fact (n/d), except `iso`: a point inside a US interconnection but outside all seven ISO/RTO
  footprints is 'non-RTO';
- the point's balancing authority is retired per EIA-930 -> its `balancing_authority` fact, and the `iso`
  fact when the retirement is newer than the ISO layer, are candidates for review (e.g. WACM and WAUW,
  retired 2026-04-01 when SPP's RTO West started, while the ISO layer dates from 2022);
- a balancing authority containing the point belongs (EIA-930) to another interconnection than the NERC-region
  union says -> the `interconnection` fact is a candidate for review (seams such as Lubbock).

Runs after each hifld_grid load and daily (new campuses and plants). `python -m marden collect grid_join`.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from marden import cli
from marden.collectors import grid_registry
from marden.collectors.rates_common import RunStats, collect, print_stats
from marden.core import db
from marden.core.db import Conn, Row
from marden.core.facts import FactWriter
from marden.core.queue import app
from marden.core.sources import SourceSpec, declare_source

SOURCE_ID = "grid_join"
PRODUCER = "collector:grid_join"
SOURCE_NAME = "HIFLD"
CRON_UTC = "47 10 * * *"  # daily 10:47 UTC, after the overnight collectors
FIELDS = ("interconnection", "iso", "balancing_authority", "utility", "utility_territories")
SOURCE = declare_source(
    SOURCE_ID, "Grid region spatial join", "model", None, 24 * 60,
    "Internal: PostGIS point-in-polygon over boundary files hifld_grid already stored (no external requests).",
)

SUBJECTS = """
SELECT 'campus' AS subject_type, id AS subject_id, geom AS pt, 'campus point' AS basis
FROM campus WHERE geom IS NOT NULL
UNION ALL
SELECT 'building', b.id, ST_PointOnSurface(b.footprint), 'point on the building footprint'
FROM building b WHERE b.footprint IS NOT NULL
UNION ALL
SELECT 'building', b.id, c.geom, 'campus point (building has no footprint)'
FROM building b JOIN campus c ON c.id = b.campus_id WHERE b.footprint IS NULL AND c.geom IS NOT NULL
UNION ALL
SELECT 'site', slug, geom, 'site point' FROM site WHERE geom IS NOT NULL
UNION ALL
SELECT 'plant', id, geom, 'plant point' FROM power_plant WHERE geom IS NOT NULL
"""
MATCHES = f"""
WITH s AS ({SUBJECTS})
SELECT s.subject_type, s.subject_id, s.basis, ST_X(s.pt) AS lon, ST_Y(s.pt) AS lat,
       r.id AS region_id, r.kind, r.name, r.parent_id, r.meta
FROM s LEFT JOIN grid_region r
  ON r.geom IS NOT NULL AND ST_Intersects(r.geom, s.pt)
 AND r.kind IN ('interconnection', 'iso_rto', 'balancing_authority', 'utility', 'reliability_region')
ORDER BY s.subject_type, s.subject_id, r.kind, r.id
"""
LAYER_TITLES = {"control-areas": "Control Areas", "independent-systems-operators": "Independent System Operators",
                "electric-retail-service-territories": "Electric Retail Service Territories",
                "nerc-regions": "NERC Regions"}


@dataclass
class Subject:
    subject_type: str
    subject_id: str
    basis: str
    lon: float
    lat: float
    regions: dict[str, list[Row]]

    def where(self) -> str:
        return f"point ({self.lon:.5f}, {self.lat:.5f}), {self.basis}"


@dataclass(frozen=True)
class Assignment:
    field: str
    value: Any
    src: dict[str, Any]  # source_url, document_id, as_of, layer, version
    quote: str | None
    quote_loc: str
    review: str | None = None  # why it waits for a person (None = published)


def load_subjects(conn: Conn) -> list[Subject]:
    out: dict[tuple[str, str], Subject] = {}
    for r in conn.execute(MATCHES).fetchall():
        key = (r["subject_type"], r["subject_id"])
        s = out.get(key)
        if s is None:
            s = out[key] = Subject(r["subject_type"], r["subject_id"], r["basis"], r["lon"], r["lat"],
                                   defaultdict(list))
        if r["region_id"] is not None:
            s.regions[r["kind"]].append(r)
    return list(out.values())


def _hifld(row: Row) -> dict[str, Any]:
    return (row["meta"] or {}).get("hifld") or {}


def _feature(meta: dict[str, Any]) -> str:
    return (f"HIFLD {LAYER_TITLES.get(meta.get('layer'), meta.get('layer'))} {meta.get('version')} feature "
            f"{meta.get('feature_id')}")


def ba_value(row: Row) -> str:
    code = row["region_id"].removeprefix("ba:")
    return row["name"] if code.startswith("eia") or "-" in code else code


def _label(row: Row) -> str:
    return (row["meta"] or {}).get("label") or row["name"]


def _overlap(field: str, rows: list[Row]) -> str:
    names = "; ".join(f"{r['region_id']} {r['name']}" for r in rows)
    return f"{len(rows)} boundaries for {field} contain the point: {names}"


def _interconnections(s: Subject) -> list[Assignment]:
    nerc = s.regions.get("reliability_region", [])
    inter = s.regions.get("interconnection", [])
    bas = s.regions.get("balancing_authority", [])
    out = []
    for row in inter:
        sub = next((n for n in nerc if n["parent_id"] == row["region_id"]), None)
        review = _overlap("interconnection", inter) if len(inter) > 1 else None
        other = sorted({b["parent_id"] for b in bas if b["parent_id"] and b["parent_id"] != row["region_id"]})
        if review is None and other:
            review = (f"balancing authorities containing the point ({', '.join(b['region_id'] for b in bas)}) include "
                      f"one in {other[0]} per EIA-930; the NERC-region union says {row['region_id']}")
        loc = (f"spatial join: {s.where()} in {_feature(_hifld(sub)) if sub else 'HIFLD NERC Regions'} "
               f"({sub['region_id'] if sub else 'no subregion'}); WECC -> Western, Texas RE -> ERCOT, "
               "others -> Eastern")
        out.append(Assignment("interconnection", _label(row), (row["meta"] or {}).get("boundary") or {},
                              sub["name"] if sub else None, loc, review))
    return out


def _isos(s: Subject, iso_layer: dict[str, Any] | None) -> list[Assignment]:
    isos = s.regions.get("iso_rto", [])
    inter = s.regions.get("interconnection", [])
    out = []
    for row in isos:
        meta = _hifld(row)
        review = _overlap("iso", isos) if len(isos) > 1 else None
        out.append(Assignment("iso", _label(row), meta, meta.get("name"),
                              f"spatial join: {s.where()} in {_feature(meta)}", review))
    if not isos and inter and iso_layer:
        retired = [b for b in s.regions.get("balancing_authority", [])
                   if ((b["meta"] or {}).get("eia930") or {}).get("active") is False
                   and (b["meta"]["eia930"].get("retirement_date") or "") > iso_layer.get("as_of", "")]
        review = (f"balancing authority {retired[0]['region_id']} retired per EIA-930 on "
                  f"{retired[0]['meta']['eia930']['retirement_date']}, after the ISO layer ({iso_layer.get('as_of')})"
                  if retired else None)
        loc = (f"spatial join: {s.where()} outside all {LAYER_TITLES['independent-systems-operators']} polygons "
               f"({iso_layer.get('version')}), inside the {inter[0]['name']}")
        out.append(Assignment("iso", grid_registry.NON_RTO, iso_layer, None, loc, review))
    return out


def _bas(s: Subject) -> list[Assignment]:
    bas = s.regions.get("balancing_authority", [])
    out = []
    for row in bas:
        meta = _hifld(row)
        e930 = (row["meta"] or {}).get("eia930") or {}
        review = _overlap("balancing_authority", bas) if len(bas) > 1 else None
        if review is None and e930.get("active") is False:
            review = (f"{row['region_id']} retired per EIA-930 on {e930.get('retirement_date')}; the control-area "
                      f"boundary is as of {meta.get('as_of')}")
        out.append(Assignment("balancing_authority", ba_value(row), meta, meta.get("name"),
                              f"spatial join: {s.where()} in {_feature(meta)}", review))
    return out


def _utilities(s: Subject) -> list[Assignment]:
    rows = s.regions.get("utility", [])
    if not rows:
        return []
    meta = _hifld(rows[0])
    names = sorted(r["name"] for r in rows)
    features = ", ".join(str(_hifld(r).get("feature_id")) for r in rows)
    loc = (f"spatial join: {s.where()} in HIFLD {LAYER_TITLES['electric-retail-service-territories']} "
           f"{meta.get('version')} features {features}")
    out = [Assignment("utility_territories", names, meta, "; ".join(names), loc)]
    if len(rows) == 1:
        out.append(Assignment("utility", rows[0]["name"], meta, meta.get("name"),
                              f"spatial join: {s.where()} in {_feature(meta)}"))
    return out


def assign(s: Subject, iso_layer: dict[str, Any] | None) -> list[Assignment]:
    """The facts one subject gets (see the module rules)."""
    out = [*_interconnections(s), *_isos(s, iso_layer), *_bas(s), *_utilities(s)]
    return [a for a in out if a.src.get("source_url") and a.src.get("as_of")]


def iso_layer_meta(conn: Conn) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT meta -> 'hifld' AS m FROM grid_region
           WHERE kind = 'iso_rto' AND geom IS NOT NULL AND meta ? 'hifld' ORDER BY id LIMIT 1"""
    ).fetchone()
    return row["m"] if row else None


def write(facts: FactWriter, s: Subject, a: Assignment) -> Any:
    return facts.add(
        s.subject_type, s.subject_id, a.field, a.value, tag="F", source_name=SOURCE_NAME,
        source_url=a.src["source_url"], document_id=a.src.get("document_id"), valid_from=a.src["as_of"],
        producer=PRODUCER, status="candidate" if a.review else "published", quote=a.quote, quote_loc=a.quote_loc,
    )


def join(conn: Conn, stats: RunStats | None = None) -> dict[str, Any]:
    stats = stats or RunStats()
    facts = FactWriter(conn)
    subjects = load_subjects(conn)
    iso_layer = iso_layer_meta(conn)
    by_field = {f: {"published": 0, "candidate": 0, "new": 0, "none": 0} for f in FIELDS}
    results: list[dict[str, Any]] = []
    reviews = 0
    for s in subjects:
        assignments = assign(s, iso_layer)
        flagged: dict[str, list[tuple[Assignment, Any]]] = defaultdict(list)
        for f in FIELDS:
            by_field[f]["none"] += int(not any(a.field == f for a in assignments))
        for a in assignments:
            res = write(facts, s, a)
            counts = by_field[a.field]
            counts["candidate" if a.review else "published"] += 1
            counts["new"] += int(res.created)
            stats.facts_seen += 1
            stats.facts_new += int(res.created)
            if a.review:
                flagged[a.field].append((a, res))
            results.append({"subject": f"{s.subject_type}:{s.subject_id}", "field": a.field, "value": a.value,
                            "status": res.status, "review": a.review})
        for field, items in flagged.items():  # one review per subject and field, listing every candidate
            first = items[0]
            facts.request_review(
                first[1].id, title=f"{s.subject_type} {s.subject_id}: which {field}?", reason=first[0].review or "",
                payload={"field": field, "candidates": [{"fact_id": r.id, "value": a.value, "quote_loc": a.quote_loc}
                                                        for a, r in items]})
            reviews += 1
    stats.extra.update(subjects=len(subjects), by_field=by_field, reviews=reviews)
    return {"subjects": len(subjects), "by_field": by_field, "reviews": reviews, "assignments": results}


def run(*, source: SourceSpec = SOURCE) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def body(stats: RunStats) -> None:
        with db.transaction() as conn:
            out.update(join(conn, stats))

    result = collect(source, body)
    result["assignments"] = out.get("assignments", [])
    return result


@app.periodic(cron=CRON_UTC, periodic_id="daily")
@app.task(name="collect.grid_join", queue="collect", queueing_lock="collect.grid_join", lock="collect.grid")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    out = run()
    out.pop("assignments", None)
    return out


@cli.action("collect", "grid_join", help="Spatial join: campuses, buildings, sites, plants -> ISO, BA, utility")
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run())
