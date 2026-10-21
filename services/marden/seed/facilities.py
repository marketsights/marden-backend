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
| campus | developer | - | covered: the case dossier's Developer (else Sponsor), tag by basis; Epoch-only: Epoch owner (M rule) |
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
| campus | sqft_total, building_count, acres, sqft_note | sqft, count, acres | sqft.json (F permit/filing/county, R company/press) |
| campus, site | large_load_request | - | one per linked large-load project: {project_id, name, utility, iso, mw, stage, queue_ids, ...} |
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
| unit | oem, model, count, rating_mw_each, fuel, count_is_maximum | -, -, count, MW | parsed unit statement (quote kept) |
| entity | proceeding, proceeding_kind, agency, list_url, status, next_step, next_dates, flags, linked_sites | - | docket entities (inventory, power.json dockets) |
| entity | why_it_matters | - | the archive's note on a power.json docket (M) |
| site, campus, plant | materiality_note | - | {event_date, kind, title, judgment, rule, event_source_url}: material history (M) |
| site, instrument | next_catalyst | - | earliest catalyst on or after 2026-10-20: {date, event, certainty, category, ...} |

Rows: campus (102), building (Epoch Building polygons), power_plant (power.json gas plant / fuel cell /
SMR / co-location / data center campus power / nuclear restart), power_unit, plant_serves,
site_link ('proposed' + review_item), docket entities, plant operator entities, event, review_item,
document (seed files + SEC exhibit records). Idempotent: rows upsert, facts/events dedupe on keys.
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any

import psycopg

from marden import cli
from marden.core import db, heartbeat
from marden.core.db import Conn
from marden.core.sources import get_spec, upsert_spec
from marden.seed.common import SEED_SOURCE_ID
from marden.seed.facilities_archive import DCMAP, Archive
from marden.seed.facilities_campus import campus_record, upsert_campus, write_campus_facts
from marden.seed.facilities_catalysts import write_catalysts, write_exhibit_documents
from marden.seed.facilities_epoch import campus_status, write_buildings, write_epoch_campus
from marden.seed.facilities_events import write_dockets, write_state_events
from marden.seed.facilities_geo import COUNTIES, counties
from marden.seed.facilities_links import propose_links
from marden.seed.facilities_plants import PowerLinks, write_plants, write_serves
from marden.seed.facilities_power_events import write_power_events
from marden.seed.facilities_sink import PRODUCER, EventSink, FactSink
from marden.seed.facilities_units import write_air_permits, write_units

DEADLOCK_RETRIES = 3
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


def load_into(conn: Conn) -> dict[str, Any]:
    """Load the facilities seed on `conn` (the caller owns the transaction)."""
    arc = Archive(conn)
    arc.load_all()
    existing_sites = {r["slug"] for r in conn.execute("SELECT slug FROM site").fetchall()}
    index = counties(arc.data[COUNTIES])
    facts = FactSink(conn)
    events = EventSink(conn, facts)
    stats: dict[str, Any] = {"campuses": 0, "buildings": 0, "skipped": {}}
    for row in arc.dcmap_rows:
        rec = campus_record(arc, row, index)
        upsert_campus(conn, rec)
        write_campus_facts(facts, arc, row, rec)
        stats["campuses"] += 1
    unmatched = []
    for name in sorted(set(arc.epoch_rows) | set(arc.map_dcs)):
        cid = arc.campus_of_epoch.get(name)
        if cid is None:
            unmatched.append(name)
            continue
        row = next(r for r in arc.dcmap_rows if arc.campus_of_key[r["key"]] == cid)
        write_epoch_campus(facts, arc, name, cid, row)
        stats["buildings"] += write_buildings(conn, facts, arc, name, cid)
    stats["skipped"]["epoch_without_campus"] = unmatched
    for row in arc.dcmap_rows:
        facts.add("campus", arc.campus_of_key[row["key"]], "status", campus_status(arc, row),
                  src=arc.sources[DCMAP], rule=True)
    links = PowerLinks.build(arc, existing_sites)
    stats["skipped"]["power_links"] = links.skipped
    stats["plants"] = write_plants(conn, facts, arc, links)
    stats["serves"] = write_serves(conn, facts, arc, links)
    stats["units"] = write_units(conn, facts, arc, links)
    stats["air_permits"] = write_air_permits(facts, arc, existing_sites)
    stats["site_links"] = propose_links(conn, facts, arc, existing_sites)
    stats["dockets"] = write_dockets(conn, facts, events, arc, existing_sites)
    stats["state_events"] = write_state_events(events, arc, existing_sites)
    stats["power_events"] = write_power_events(events, arc, links, existing_sites)
    stats["catalysts"] = write_catalysts(conn, facts, events, arc, existing_sites)
    stats["exhibits"] = write_exhibit_documents(conn, arc)
    stats.update({
        "facts_seen": facts.seen, "facts_new": facts.created, "events_new": events.created,
        "event_reviews_new": events.reviews, "materiality_notes_new": events.notes,
        "events_skipped": dict(events.skipped), "documents_read": len(arc.sources),
        "fields": facts.fields(), "skipped_counts": {f: n for (st, f), n in facts.counts.items() if st == "skipped"},
        "tables": table_counts(conn),
    })
    return stats


def _load_once() -> dict[str, Any]:
    """The whole load in one transaction; retried when a concurrent writer deadlocks on the shared
    read_model_dirty rows (the transaction rolls back completely, so a retry is safe)."""
    for attempt in range(1, DEADLOCK_RETRIES + 1):
        try:
            with db.transaction() as conn:
                return load_into(conn)
        except (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure):
            if attempt == DEADLOCK_RETRIES:
                raise
            time.sleep(2 * attempt)
    raise AssertionError("unreachable")


def load() -> dict[str, Any]:
    """One recorded run of the loader (source_run of `seed_marden_data`), in one transaction."""
    upsert_spec(get_spec(SEED_SOURCE_ID))
    with heartbeat.run(SEED_SOURCE_ID) as hb:
        stats = _load_once()
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
