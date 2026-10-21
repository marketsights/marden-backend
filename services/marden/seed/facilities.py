"""Seed loader `facilities`: campuses, buildings, power plants and units, plant_serves, site-link
proposals, events (dockets, case news, power feed, catalysts) and SEC exhibit document rows.

    cd services && uv run python -m marden seed facilities

Reads (each stored raw first as a `document` of `seed_marden_data`): aiwc/q3x/{dcmap,sqft,pw_found,
cat,docs}.json, aiwc/q3x/docs/library.json, aiwc/q3x/epoch_dl/x/{data_centers,data_center_timelines}.csv
and epoch_dl/map_props.json (Epoch AI, CC BY 4.0), aiwc/q3x/geo/us-atlas-3.0.1/counties-10m.json,
power.json, aiwcredit.json (pw2c only), aiwc/gpu/site_power.json, dockets/inventory.json,
dockets/out/<key>.json (73), aiwc/state/events.json, aiwc/memo/<CASE>.json (covered cases). No
price is read (dcmap/cat/site px fields are ignored).

Tags: F filed host (sec.gov, ferc.gov, state and county portals), R press / Epoch AI / the archive's
own research (source 'Marden research archive' when the record has no URL), M Marden: fixed rules
here (``seed-facilities-rules@2026-10-21``) or the archive's judgments (``legacy-<file>@2026-10-20``).
valid_from = the record's date (Epoch status date, timeline date, sqft as-of, event date), else the
file as-of. Producer ``seed:facilities``; facts published; n/d values are not written. Confidence:
Epoch #confident 0.9 / #likely 0.6 / #speculative 0.3; archive high 0.9 / medium 0.6 / low 0.3.

| subject_type | field | unit | meaning |
|---|---|---|---|
| campus | county | - | {name, fips, state}: Census county containing the campus point (M, us-atlas) |
| campus | state | - | USPS code from that county when the row has no state (M) |
| campus | country | - | country (Epoch CSV; M 'United States' from a US state code otherwise) |
| campus | first_seen | date | earliest dated observation in the archive: Epoch timeline, Sentinel-2, sqft (M) |
| campus | location_text, address | - | covered-row location text (archive); Epoch street address |
| campus | owner | - | owning company (Epoch Owner with confidence; covered rows without Epoch: archive) |
| campus | developer | - | covered: the case dossier's Developer (else Sponsor), tag by basis; |
| | | | Epoch-only: Epoch's owner (M rule) |
| campus | users | - | [{name, confidence}] compute users (Epoch labels; archive text on covered rows) |
| campus | tenant | - | lease tenant text (covered rows) |
| campus | project_name | - | Epoch project label (e.g. Stargate) |
| campus | investors, construction_companies, energy_companies, chip_types | - | Epoch lists |
| campus | capex_usd | USD | Epoch current total capital cost (2025 USD) |
| campus | chips, chips_planned | count | {chip model: units} current / planned (Epoch) |
| campus | facility_mw | MW | facility power: covered gross MW (archive); Epoch power at its last timeline date |
| campus | it_mw | MW | Epoch IT power at its last timeline date |
| campus | planned_it_mw, delivered_mw | MW | covered rows: planned IT MW; MW delivered to date |
| campus | operating_mw, operating_it_mw | MW | Epoch operating facility / IT power, one fact per timeline date |
| campus | buildings_operational, h100_equivalents | count | Epoch, one fact per timeline date |
| campus | capacity_plan | - | Epoch projected rows after the download: [{date, facility_mw, it_mw, ...}] |
| campus | online_year | year | first Epoch timeline year with power > 0 |
| campus | online_year_basis | - | 'observed' (before the download) or 'projected' |
| campus | epoch_status, epoch_construction_note | - | Epoch status narrative; latest timeline construction note |
| campus | epoch_operational | bool | Epoch 'operational' flag (map facts) |
| campus | status | - | types.ts DcStatus by rule (M, see facilities_epoch.campus_status) |
| campus | backup_generator_count, substation_count | count | Epoch annotated polygons of that type (M count) |
| campus | copyright_notice | - | Epoch attribution (CC BY 4.0) on every campus with Epoch data |
| campus | power_source | - | types.ts PowerSource by rule from the archive's power class (M) |
| campus | power_source_text, power_status, power_detail | - | the archive's power class, status, detail |
| campus | utility, iso | - | utility and ISO/RTO (pw_found with URL; covered: gpu/site_power, else dcmap pw) |
| campus | onsite_generation | - | covered: on-site generation text (gpu/site_power) |
| campus | grid_mw | MW | utility / grid capacity stated by pw_found |
| campus | schedule_text, original_schedule_text | - | covered: delivery schedule now / originally, as stated |
| campus | imagery_note | - | covered: {date, read, flag, roof_ha} Marden read of Sentinel-2 imagery (M) |
| campus | sqft_total, building_count, acres, sqft_note | sqft, count, acres | sqft.json (F permit/filing/county, |
| | | | R company/press) |
| campus, site | large_load_request | - | one per linked large-load project: {project_id, name, utility, iso, mw, |
| | | | stage, queue_ids, ...} |
| campus, site | grid_project | - | one per linked transmission / solar+storage project (same shape) |
| campus | site_link_evidence | - | {site_slug, case, campus_id, basis} behind each proposed site_link |
| building | construction_complete | bool | Epoch footprint marked complete (complete=1 or a past endDate) |
| building | status | - | DcStatus by rule: Operating when complete, else Under construction (M) |
| building | construction_start, construction_end | date | Epoch startDate / endDate |
| building | alt_label, footprint_area_m2 | -, m2 | Epoch building promo label; Epoch area |
| building | copyright_notice | - | Epoch attribution |
| plant | legacy_id, technology_type | - | power.json project id and type |
| plant | technology | - | types.ts PlantTech by rule from type and text (M) |
| plant | operator, operator_entity_id | - | developer / operator name; its entity id (M join) |
| plant | utility, iso | - | serving utility; ISO/RTO |
| plant | capacity_mw | MW | project MW as stated (not written for GW-as-MW and year-as-MW values) |
| plant | stage, status | - | archive stage; types.ts PlantStatus by rule (M; 'stalled' has none) |
| plant | serves_text, debt_text, expected_cod, original_cod, latest_change | - | as stated |
| plant | risk_level, risk_note | - | the archive's risk judgment (M) |
| plant | queue_ids | - | interconnection queue serials in the project name |
| plant | parties | - | [{kind, key, role, role_code, name, note}] chain tuples (role legend inferred: M) |
| plant | dockets | - | [{agency, number, title, latest_action, latest_date, url}] |
| plant | serves_campus | - | {campus_id, case, basis, pw2c_case}: evidence of each plant_serves row |
| plant | serves_sites | - | financed-site slugs pw2c links the plant to |
| plant | equipment_mentions | - | [{oem, model, quote}] equipment named without a unit count |
| plant | air_permit | - | {agency, number, ids, type, applicant, status, proceeding_key, url} |
| site | air_permits | - | every state air permit watched for the case (same shape) |
| unit | oem, model, count, rating_mw_each | -, -, count, MW | parsed unit statement (quote kept) |
| unit | fuel, count_is_maximum | - | fuel named next to the unit; true when the text says 'up to' |
| entity | proceeding, proceeding_kind, agency, list_url, status | - | docket entities (inventory, power.json) |
| entity | next_step, next_dates, flags, linked_sites | - | docket entities: next steps and dates, 30-day flags, sites |
| entity | why_it_matters | - | the archive's note on a power.json docket (M) |
| site, campus, plant | materiality_note | - | {event_date, kind, title, judgment, rule, event_source_url}: |
| | | | material history the archive reviewed (M) |
| site, instrument | next_catalyst | - | earliest catalyst on or after 2026-10-20: {date, event, certainty, |
| | | | category, ...} |

Rows: campus (102), building (Epoch Building polygons), power_plant (power.json gas plant / fuel cell /
SMR / co-location / data center campus power / nuclear restart), power_unit, plant_serves,
site_link ('proposed' + review_item), docket entities, plant operator entities, event, review_item,
document (seed files + SEC exhibit records). Idempotent: rows upsert, facts/events dedupe on keys.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from marden import cli
from marden.core import db, heartbeat
from marden.core.db import Conn
from marden.core.sources import get_spec, upsert_spec
from marden.seed.common import SEED_SOURCE_ID
from marden.seed.facilities_archive import CATALYSTS, DCMAP, EVENTS, POWER, Archive
from marden.seed.facilities_campus import campus_record, upsert_campus, write_campus_facts
from marden.seed.facilities_catalysts import (
    catalyst_stats,
    exhibit_records,
    write_catalysts,
    write_exhibit_documents,
    write_next_catalysts,
)
from marden.seed.facilities_epoch import campus_status, write_buildings, write_epoch_campus
from marden.seed.facilities_events import write_docket, write_docket_items, write_power_dockets, write_state_events
from marden.seed.facilities_geo import COUNTIES, counties
from marden.seed.facilities_links import propose_links
from marden.seed.facilities_plants import PowerLinks, plant_stats, write_plants, write_serves
from marden.seed.facilities_power_events import (
    new_stats,
    write_docket_filings,
    write_feed_events,
    write_project_events,
)
from marden.seed.facilities_run import Sections
from marden.seed.facilities_sink import PRODUCER, EventSink, FactSink
from marden.seed.facilities_units import write_air_permits, write_units

CHUNK = 40  # writes per committed section (see chunked)
TABLES = ("campus", "building", "power_plant", "power_unit", "plant_serves", "site_link", "event",
          "review_item", "entity", "document")


def table_counts(conn: Conn) -> dict[str, Any]:
    out: dict[str, Any] = {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in TABLES}
    out["fact_facilities"] = {r["subject_type"]: r["n"] for r in conn.execute(
        "SELECT subject_type, count(*) AS n FROM fact WHERE producer = %s GROUP BY 1 ORDER BY 1", (PRODUCER,))}
    out["event_by_kind"] = {f"{r['kind']}:{r['status']}": r["n"] for r in conn.execute(
        "SELECT kind, status, count(*) AS n FROM event GROUP BY 1, 2 ORDER BY 1, 2")}
    out["docket_entities"] = conn.execute("SELECT count(*) AS n FROM entity WHERE kind = 'docket'").fetchone()["n"]
    return out


def chunked[T](items: list[T], size: int) -> list[list[T]]:
    """Slices of at most `size` (>= 1). Each write marks read models dirty inside a savepoint, and
    a transaction with more than 64 written savepoints overflows Postgres' subtransaction cache
    (every later visibility check then reads pg_subtrans); short sections stay under it."""
    size = max(1, size)
    return [items[i: i + size] for i in range(0, len(items), size)]


def _campuses(conn: Conn, facts: FactSink, arc: Archive, rows: list[dict[str, Any]], index: list[Any],
              stats: dict[str, Any]) -> None:
    for row in rows:
        rec = campus_record(arc, row, index)
        upsert_campus(conn, rec)
        write_campus_facts(facts, arc, row, rec)
        stats["campuses"] += 1


def _epoch(conn: Conn, facts: FactSink, arc: Archive, name: str, stats: dict[str, Any]) -> None:
    cid = arc.campus_of_epoch.get(name)
    if cid is None:
        stats["skipped"].setdefault("epoch_without_campus", []).append(name)
        return
    row = next(r for r in arc.dcmap_rows if arc.campus_of_key[r["key"]] == cid)
    write_epoch_campus(facts, arc, name, cid, row)
    stats["buildings"] += write_buildings(conn, facts, arc, name, cid)


def _statuses(facts: FactSink, arc: Archive, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        facts.add("campus", arc.campus_of_key[row["key"]], "status", campus_status(arc, row),
                  src=arc.sources[DCMAP], rule=True)


def load_into(conn: Conn, *, cases: list[str] | None = None, campus_keys: tuple[str, ...] = (),
              exhibit_limit: int | None = None) -> dict[str, Any]:
    """Load the facilities seed on `conn`, section by section (see facilities_run). On an
    autocommit connection every section commits; inside a caller's transaction they are savepoints."""
    arc = Archive(conn, frozenset(cases) if cases is not None else None, frozenset(campus_keys), exhibit_limit)
    facts = FactSink(conn)
    events = EventSink(conn, facts)
    sections = Sections(conn, (facts, events))
    run = sections.run
    run("archive", arc.load_all)  # documents first, so every later section can cite them
    existing_sites = {r["slug"] for r in conn.execute("SELECT slug FROM site").fetchall()}
    index = counties(arc.data[COUNTIES])
    stats: dict[str, Any] = {"campuses": 0, "buildings": 0, "skipped": {}}
    for rows in chunked(arc.dcmap_rows, 2):
        run("campuses", lambda rows=rows: _campuses(conn, facts, arc, rows, index, stats))
    for name in arc.epoch_names:
        run("epoch", lambda name=name: _epoch(conn, facts, arc, name, stats))
    for rows in chunked(arc.dcmap_rows, CHUNK):
        run("status", lambda rows=rows: _statuses(facts, arc, rows))
    links = PowerLinks.build(arc, existing_sites)
    stats["skipped"]["power_links"] = links.skipped
    stats["plants"] = plant_stats()
    for pids in chunked(sorted(arc.projects), 2):
        run("plants", lambda pids=pids: write_plants(conn, facts, arc, links, pids, stats["plants"]))
    stats["serves"] = run("serves", lambda: write_serves(conn, facts, arc, links))
    stats["units"] = run("units", lambda: write_units(conn, facts, arc, links))
    stats["air_permits"] = run("air_permits", lambda: write_air_permits(facts, arc, existing_sites))
    stats["site_links"] = run("site_links", lambda: propose_links(conn, facts, arc, existing_sites))
    dockets: dict[str, Any] = {"docket_entities": 0, "items": 0, "skipped": []}
    for key in sorted(arc.inventory_by_key):
        sites = run("dockets", lambda key=key: write_docket(conn, facts, arc, key, existing_sites, dockets))
        for items in chunked(arc.docket_file(key).get("items") or [], CHUNK // len(sites)) if sites else []:
            run("docket_items", lambda key=key, items=items, sites=sites:
                write_docket_items(events, arc, key, items, sites, dockets))
    for part in chunked(arc.data[POWER].get("dockets") or [], CHUNK // 6):
        run("power_dockets", lambda part=part: write_power_dockets(conn, facts, arc, part, dockets))
    stats["dockets"] = dockets
    stats["state_events"] = {"events": 0, "skipped": []}
    for rows in chunked(arc.data[EVENTS], CHUNK):
        run("state_events", lambda rows=rows: write_state_events(events, arc, existing_sites, rows,
                                                                 stats["state_events"]))
    pev = stats["power_events"] = new_stats()
    for pids in chunked(sorted(arc.projects), 3):
        run("power_events", lambda pids=pids: write_project_events(events, arc, links, pids, pev))
    for feed in chunked(arc.data[POWER].get("feed") or [], CHUNK // 2):
        run("power_feed", lambda feed=feed: write_feed_events(events, arc, links, existing_sites, feed, pev))
    run("power_filings", lambda: write_docket_filings(events, arc, links, existing_sites, pev))
    cat = catalyst_stats(conn)
    for rows in chunked(arc.data[CATALYSTS]["rows"], CHUNK // 4):
        run("catalysts", lambda rows=rows: write_catalysts(events, arc, existing_sites, rows, cat))
    for items in chunked(sorted(cat["next"].items()), CHUNK):
        run("next_catalysts", lambda items=items: write_next_catalysts(facts, arc, items))
    stats["catalysts"] = {k: cat[k] for k in ("catalysts", "events", "skipped")} | {"next": len(cat["next"])}
    stats["exhibits"] = {"documents": 0, "new": 0}
    for recs in chunked(exhibit_records(arc), CHUNK):
        run("exhibits", lambda recs=recs: write_exhibit_documents(conn, arc, recs, stats["exhibits"]))
    stats.update({
        "facts_seen": facts.seen, "facts_new": facts.created, "events_new": events.created,
        "event_reviews_new": events.reviews, "materiality_notes_new": events.notes,
        "events_skipped": dict(events.skipped), "documents_read": len(arc.sources),
        "fields": facts.fields(), "skipped_counts": {f: n for (st, f), n in facts.counts.items() if st == "skipped"},
        "tables": table_counts(conn), "timings": sections.timings, "retries": sections.retries,
        "power_events": {**pev, "skipped": dict(pev["skipped"])},
        "plants": {**stats["plants"], "non_plant_linked": dict(stats["plants"]["non_plant_linked"])},
    })
    return stats


def load(**scope: Any) -> dict[str, Any]:
    """One recorded run of the loader (source_run of `seed_marden_data`). `scope` (cases,
    campus_keys, exhibit_limit) cuts a slice of the archive for tests; the CLI loads everything."""
    upsert_spec(get_spec(SEED_SOURCE_ID))
    with heartbeat.run(SEED_SOURCE_ID) as hb:
        # A dedicated autocommit connection: each section's `transaction()` is then a real
        # transaction that commits (on a pooled connection in implicit-transaction mode they would
        # all be savepoints of one long transaction).
        with db.connect(autocommit=True) as conn:
            stats = load_into(conn, **scope)
        hb.seen(stats["facts_seen"])
        hb.new(stats["facts_new"] + stats["events_new"])
        hb.meta.update({"loader": "facilities", "campuses": stats["campuses"], "buildings": stats["buildings"],
                        "plants": stats["plants"]["plants"], "events_new": stats["events_new"]})
    return stats


@cli.action("seed", "facilities", help="Load campuses, buildings, power plants, site links, events and exhibits")
def seed_facilities(args: argparse.Namespace) -> int:
    stats = load()
    print(json.dumps(stats, indent=2, default=str))
    return 0
