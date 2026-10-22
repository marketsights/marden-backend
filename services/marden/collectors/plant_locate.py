"""Where each power plant is: its point, county and EIA plant id, from the strongest record Marden holds.

power_plant rows come from the seed (id, name, state; `facilities_plants`); this collector owns their `geom`,
`county` and `eia_plant_id` columns and keeps them equal to the facts it writes (subject 'plant', producer
collector:plant_locate). No external requests: it reads documents and facts other collectors stored.

Point, first that exists:
1. EIA-860M: the plant is an EIA plant (`plant_locate_eia`: name, state, technology and an operator, location or
   capacity record agreeing; or an EIA plant id already on it). Facts `eia_plant_id` and `coordinates`, tag F,
   source 'EIA-860M', the newest stored workbook (document, URL), valid_from = its report month's end, quote =
   the workbook row's cells, quote_loc = sheet and row plus the match evidence, method_version = the match method.
2. A record with coordinates: a published `coordinates` fact on the plant from another collector (an air-permit
   or interconnection record, tag F). It is used as it stands; nothing new is written for the point.
3. On-site at the served campus: only when the plant's own facts say it is on-site (name, utility, serves or risk
   text: 'on-site', 'behind-the-meter', 'microgrid'), it is not a fuel line, and it serves exactly one campus
   (plant_serves) with a point in its state, else exactly one site (`serves_sites`). Fact `coordinates`, tag R,
   source = the serves fact's source and document (the archive file that holds both the link and the campus
   point), quote = the plant's own on-site text, basis 'on-site at served campus <id>' (method ONSITE_METHOD).
   A utility plant built for a campus (Entergy's Hyperion units, El Paso Electric's unit for Meta) is not
   on-site and is not placed there.
Otherwise the plant has no point (n/d): no coordinates are ever invented.

County: from the point, the `us_county` polygon containing it (census_counties), tag R, method COUNTY_METHOD,
only when the polygon's state is the plant's state. EIA states a county for its plants: a disagreement with the
polygon (generalized 1:5M boundaries near a county line) leaves the county to EIA's record (tag F) and is
counted. Without a point: the county all of the plant's interconnection queue entries (`queue_ids`) name in its
state, tag F, source the queue (ERCOT GIS report, PJM queue), method QUEUE_METHOD.

Writes are change-only against this collector's latest value per plant and field (a rerun writes nothing). The
columns follow the decision of the run; a column whose evidence is gone is cleared and counted. Daily, ten
minutes before grid_join, so a new point gets its grid facts the same morning.
`python -m marden collect plant_locate [--dry-run]`.
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from marden import cli
from marden.collectors import plant_locate_county as county
from marden.collectors import plant_locate_eia as eia
from marden.collectors.eia_860m_parse import Report, parse_workbook
from marden.collectors.eia_860m_store import report_date, served_campus_points
from marden.collectors.rates_common import RunStats, collect, print_stats
from marden.core import db
from marden.core.business_day import business_today
from marden.core.db import Conn, Row
from marden.core.documents import DocumentRow
from marden.core.facts import FactWriter
from marden.core.jsonutil import canonical_json
from marden.core.queue import app
from marden.core.sources import SourceSpec, declare_source
from marden.readmodels.deps import mark_dirty_for

SOURCE_ID = "plant_locate"
PRODUCER = "collector:plant_locate"
CRON_UTC = "37 10 * * *"  # daily 10:37 UTC, before grid_join (10:47)
ONSITE_METHOD = "plant-locate-onsite@0.1"
ONSITE = re.compile(r"\bon[- ]?site\b|\bbehind[- ]the[- ]meter\b|\bmicrogrid\b", re.I)
ONSITE_FIELDS = ("utility", "serves_text", "risk_note")
FUEL_LINE = re.compile(r"\b(pipeline|lateral)\b", re.I)
FACT_FIELDS = ("operator", "technology", "technology_type", "capacity_mw", "utility", "serves_text", "risk_note",
               "queue_ids", "serves_sites", "serves_campus")
OWN_FIELDS = ("eia_plant_id", "coordinates", "county")
SOURCE = declare_source(
    SOURCE_ID, "Power plant locations (EIA-860M, records, served campus)", "model", None, 24 * 60,
    "Internal: no external requests. Reads what other collectors stored: the newest EIA-860M workbook (eia_860m; "
    "EIA Copyrights and Reuse: \"U.S. government publications are in the public domain and are not subject to "
    "copyright protection. You may use and/or distribute any of our data...\"), Census county polygons "
    "(census_counties; US government work, \"Access constraints: None\"), ISO/RTO queue facts (ercot_gis, "
    "pjm_queue; terms on those sources) and the seed archive's plant-campus links.",
)


# ---------------------------------------------------------------- inputs
def eia_document(conn: Conn) -> DocumentRow | None:
    row = conn.execute(
        """SELECT * FROM document WHERE source_id = 'eia_860m' AND doc_type = 'eia860m_xlsx' AND doc_date IS NOT NULL
            ORDER BY doc_date DESC, fetched_at DESC, id DESC LIMIT 1"""
    ).fetchone()
    return DocumentRow.from_row(row, was_new=False) if row else None


def _latest(conn: Conn, fields: tuple[str, ...], *, own: bool) -> dict[tuple[str, str], Row]:
    """(plant, field) -> this collector's latest value (own) or the current value from any other producer."""
    order = ("learned_at DESC, id DESC" if own else
             "(tag = 'M'), valid_from DESC, (CASE tag WHEN 'F' THEN 0 WHEN 'R' THEN 1 ELSE 2 END), learned_at DESC, "
             "id DESC")
    rows = conn.execute(
        f"""SELECT DISTINCT ON (subject_id, field) * FROM fact
             WHERE subject_type = 'plant' AND status = 'published' AND field = ANY(%s)
               AND (producer = %s) = %s AND (valid_to IS NULL OR valid_to > %s)
             ORDER BY subject_id, field, {order}""",
        (list(fields), PRODUCER, own, business_today()),  # New York's today, as fact_current
    ).fetchall()
    return {(r["subject_id"], r["field"]): r for r in rows}


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _record_point(record: Row | None) -> tuple[float, float] | None:
    """A filed (tag F) `coordinates` record's point; a value without a numeric lat/lon in range is no point."""
    value = record["value"] if record is not None and record["tag"] == "F" else None
    if not isinstance(value, dict):
        return None
    lat, lon = _number(value.get("lat")), _number(value.get("lon"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


@dataclass
class Located:
    """One plant's decision: its point (with the fact behind it), county and EIA plant id."""

    plant: str
    method: str  # eia_860m | record | onsite_campus | onsite_site | none
    point: tuple[float, float] | None = None
    point_fact: dict[str, Any] | None = None  # kwargs for FactWriter.add, None when the fact exists already
    point_src: dict[str, Any] | None = None  # the record behind the point (source, document, date, location)
    eia_fact: dict[str, Any] | None = None
    eia_plant_id: int | None = None
    eia_county: str | None = None
    county: str | None = None
    county_fact: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- point
def _eia_facts(loc: Located, out: eia.Outcome, doc: DocumentRow, report: Report) -> None:
    site = out.site
    assert site is not None
    d, file_name = site.row.descriptor, doc.url.rsplit("/", 1)[-1]
    common = {"tag": "F", "source_name": "EIA-860M", "source_url": doc.url, "document_id": doc.id,
              "valid_from": report_date(report.year, report.month), "method_version": eia.METHOD}
    where = f"{file_name}!{site.row.sheet} row {site.row.row}"
    cells = (f"Plant ID {site.id} | Plant Name {site.plant.name} | Plant State {site.plant.state} | "
             f"County {site.county} | Entity Name {d.get('entity_name')}")
    loc.eia_plant_id, loc.eia_county = site.id, site.county
    if out.status == "matched":
        loc.eia_fact = {**common, "value": site.id, "quote": cells,
                        "quote_loc": f"{where}; matched: {'; '.join(out.evidence)}"}
    if site.plant.lat is None:
        loc.notes.append(f"EIA plant {site.id} has no coordinates")
        return
    loc.method, loc.point = "eia_860m", (site.plant.lat, site.plant.lon)
    loc.point_fact = loc.point_src = {
        **common, "value": {"lat": site.plant.lat, "lon": site.plant.lon,
                            "basis": f"EIA-860M plant {site.id} ({site.plant.name})"},
        "quote": f"{cells} | Latitude {site.plant.lat} | Longitude {site.plant.lon}", "quote_loc": where,
    }


def _onsite_text(plant: Row, facts: dict[str, Row]) -> tuple[str, str] | None:
    """(field, verbatim text) where the plant's own record says it is on-site."""
    texts = [("name", plant["name"])] + [(f, facts[f]["value"]) for f in ONSITE_FIELDS
                                         if f in facts and isinstance(facts[f]["value"], str)]
    return next(((f, t) for f, t in texts if ONSITE.search(t)), None)


def _onsite(conn: Conn, loc: Located, plant: Row, facts: dict[str, Row], campus: dict[str, Any] | None) -> None:
    target, serves = None, None
    served = (facts.get("serves_campus") or {}).get("value")
    if campus is not None and isinstance(served, dict) and served.get("campus_id") == campus["campus_id"]:
        target, serves = ("campus", campus["campus_id"], campus["lat"], campus["lon"]), facts["serves_campus"]
    elif campus is None and "serves_sites" in facts and isinstance(facts["serves_sites"]["value"], list):
        slugs = facts["serves_sites"]["value"]
        rows = conn.execute(
            "SELECT slug, state, ST_Y(geom) AS lat, ST_X(geom) AS lon FROM site WHERE slug = ANY(%s) AND geom IS "
            "NOT NULL", (slugs,)).fetchall()
        same = [r for r in rows if (r["state"] or "").upper() == (plant["state"] or "").upper()]
        if len(slugs) == 1 and len(same) == 1:
            target, serves = ("site", same[0]["slug"], same[0]["lat"], same[0]["lon"]), facts["serves_sites"]
    said = _onsite_text(plant, facts)
    if target is None:
        loc.notes.append("no served campus or site with a point in the plant's state" +
                         (f" (its {said[0]} says on-site)" if said else ""))
        return
    if said is None:
        loc.notes.append(f"serves {target[0]} {target[1]}, but its facts do not say it is on-site: not placed there")
        return
    if FUEL_LINE.search(" ".join([plant["name"], str((facts.get("technology_type") or {}).get("value") or "")])):
        loc.notes.append(f"a fuel line: not placed at the {target[0]} it feeds")
        return
    kind, tid, lat, lon = target
    basis = f"on-site at served {kind} {tid}"
    loc.method, loc.point = f"onsite_{kind}", (lat, lon)
    loc.point_fact = loc.point_src = {
        "value": {"lat": lat, "lon": lon, "basis": basis}, "tag": "R", "source_name": serves["source_name"],
        "source_url": serves.get("source_url"), "document_id": serves.get("document_id"),
        "valid_from": serves["valid_from"], "method_version": ONSITE_METHOD, "quote": said[1],
        "quote_loc": f"{basis}: the {kind}'s point; served per plant fact {serves['id']} ({serves['field']}); "
                     f"on-site per the plant's {said[0]}",
    }


# ---------------------------------------------------------------- run
def decide(conn: Conn, report: Report | None, doc: DocumentRow | None, counts: Counter[str]) -> list[Located]:
    plants = conn.execute(
        "SELECT id, name, state, eia_plant_id, county, ST_Y(geom) AS lat, ST_X(geom) AS lon FROM power_plant "
        "ORDER BY id").fetchall()
    others = _latest(conn, (*FACT_FIELDS, "eia_plant_id", "coordinates"), own=False)
    own = _latest(conn, OWN_FIELDS, own=True)
    campuses = served_campus_points(conn)
    by_id, by_state = eia.index(report) if report else ({}, {})
    out: list[Located] = []
    for p in plants:
        pid = p["id"]
        facts = {f: others[(pid, f)] for f in FACT_FIELDS if (pid, f) in others}
        record = others.get((pid, "coordinates"))
        record_pt = _record_point(record)
        campus = campuses.get(pid)
        ref = ((*record_pt, "the plant's recorded point") if record_pt else
               (campus["lat"], campus["lon"], f"the served campus {campus['campus_id']} point") if campus else None)
        given = others.get((pid, "eia_plant_id"))
        given_id = given["value"] if given else (p["eia_plant_id"] if (pid, "eia_plant_id") not in own else None)
        value = {f: (facts.get(f) or {}).get("value") for f in FACT_FIELDS}
        ours = eia.OurPlant(
            pid, p["name"], p["state"], str(value["operator"] or ""),
            eia.our_tech(value["technology"], value["technology_type"]),
            _number(value["capacity_mw"]), ref[:2] if ref else None, ref[2] if ref else "",
            given_id if isinstance(given_id, int) else None,
        )
        loc = Located(pid, "none")
        outcome = eia.match(ours, by_id, by_state) if report else eia.Outcome("no_report")
        counts[f"eia_{outcome.status}"] += 1
        if outcome.site is not None and doc is not None and report is not None:
            _eia_facts(loc, outcome, doc, report)
        elif outcome.status == "eia_plant_id_not_in_report":
            loc.eia_plant_id = ours.eia_plant_id
            loc.notes.append(f"EIA plant {ours.eia_plant_id} is not in the {report.label if report else ''} workbook")
        elif outcome.status == "no_name_match":
            loc.notes.append(f"no EIA-860M plant of that name in {p['state']}")
        elif outcome.candidates:
            loc.notes.append(f"EIA {outcome.status}: " + "; ".join(
                f"{c['eia_plant_id']} {c['eia_name']} ({c['why']})" for c in outcome.candidates))
        if loc.point is None and record and record_pt:
            loc.method, loc.point = "record", record_pt
            loc.point_src = {k: record[k] for k in ("source_name", "source_url", "document_id", "valid_from",
                                                    "method_version")} | {"quote_loc": record["quote_loc"] or ""}
            loc.notes.append(f"point from {record['source_name']} fact {record['id']}")
        if loc.point is None:
            _onsite(conn, loc, p, facts, campus)
        if loc.point is not None and loc.point_src is not None:
            found, what = county.from_point(conn, loc.point, p["state"], loc.point_src, loc.eia_county)
            counts[what] += 1
        else:
            found = county.from_queue(conn, p["state"], facts.get("queue_ids"))
            counts["county_queue" if found else "county_none"] += 1
        if found:
            loc.county, loc.county_fact = found
        counts[f"located_by_{loc.method}"] += 1
        counts["with_county"] += int(loc.county is not None)
        out.append(loc)
    return out


def _put(facts: FactWriter, own: dict[tuple[str, str], Row], pid: str, fld: str, spec: dict[str, Any] | None,
         stats: RunStats) -> None:
    if spec is None:
        return
    stats.facts_seen += 1
    prior = own.get((pid, fld))
    if prior is not None and canonical_json(prior["value"]) == canonical_json(spec["value"]):
        return
    kw = {k: v for k, v in spec.items() if k != "value"}
    res = facts.add("plant", pid, fld, spec["value"], producer=PRODUCER, status="published", **kw)
    stats.facts_new += int(res.created)


def sync_columns(conn: Conn, loc: Located) -> bool:
    """power_plant.geom / county / eia_plant_id = the decision; marks the plant's views dirty when they change."""
    lat, lon = loc.point if loc.point else (None, None)
    row = conn.execute(
        """UPDATE power_plant SET
             geom = CASE WHEN %(lat)s::float8 IS NULL THEN NULL
                         ELSE ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) END,
             county = %(county)s, eia_plant_id = %(eia)s
           WHERE id = %(id)s AND (eia_plant_id IS DISTINCT FROM %(eia)s OR county IS DISTINCT FROM %(county)s
             OR (geom IS NULL) <> (%(lat)s::float8 IS NULL)
             OR (geom IS NOT NULL AND NOT ST_Equals(geom, ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326))))
           RETURNING id""",
        {"lat": lat, "lon": lon, "county": loc.county, "eia": loc.eia_plant_id, "id": loc.plant},
    ).fetchone()
    if row is not None:
        mark_dirty_for("plant", loc.plant, "coordinates", conn=conn, reason="power_plant location")
    return row is not None


def locate(conn: Conn, stats: RunStats, *, dry_run: bool = False) -> list[Located]:
    doc = eia_document(conn)
    report = parse_workbook(doc.read(), (doc.doc_date.year, doc.doc_date.month)) if doc and doc.doc_date else None
    counts: Counter[str] = Counter()
    decisions = decide(conn, report, doc, counts)
    changed = cleared = 0
    if not dry_run:
        facts, own = FactWriter(conn), _latest(conn, OWN_FIELDS, own=True)
        for loc in decisions:
            _put(facts, own, loc.plant, "eia_plant_id", loc.eia_fact, stats)
            _put(facts, own, loc.plant, "coordinates", loc.point_fact, stats)
            _put(facts, own, loc.plant, "county", loc.county_fact, stats)
            if sync_columns(conn, loc):
                changed += 1
                cleared += int(loc.point is None and (loc.plant, "coordinates") in own)
    stats.extra.update(
        eia_report=report.label if report else None, eia_document_id=doc.id if doc else None,
        plants=len(decisions), counts=dict(sorted(counts.items())), columns_changed=changed, columns_cleared=cleared,
        unplaced=[{"plant": d.plant, "county": d.county, "why": d.notes} for d in decisions if d.point is None],
    )
    return decisions


def run(*, source: SourceSpec = SOURCE, dry_run: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def body(stats: RunStats) -> None:
        with db.transaction() as conn:
            decisions = locate(conn, stats, dry_run=dry_run)
            out["placed"] = [{"plant": d.plant, "method": d.method, "eia_plant_id": d.eia_plant_id,
                              "county": d.county, "point": d.point} for d in decisions if d.point is not None]

    result = collect(source, body)
    result["placed"] = out.get("placed", [])
    return result


@app.periodic(cron=CRON_UTC, periodic_id="daily")
@app.task(name="collect.plant_locate", queue="collect", queueing_lock="collect.plant_locate",
          lock="collect.plant_locate")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    out = run()
    out.pop("placed", None)
    return out


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="decide and print, write nothing")


@cli.action("collect", "plant_locate", help="Power plant points, counties and EIA plant ids from stored records",
            configure=_configure)
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run(dry_run=args.dry_run))
