"""Seed loader `sites`: financed sites, their parties, chain links and obligations.

    cd services && uv run python -m marden seed sites

Reads (all stored raw first as `document` rows of `seed_marden_data`): site_map.json (curated,
in this package), aiwc/memo/{titles,shortnames,<CASE>}.json, aiwc/cases/<CASE>.json,
aiwc/q3x/{site,ctr,dcmap,docs}.json, aiwc/q3x/docs/{library,tickers}.json,
aiwc/gpu/{site_power,operators}.json, aidebt.json, aiwcredit.json, power.json and the aichain
JSON block of parallax-credit-marks.html. No price, TRACE or exchange value is read into a fact.

Writes `site` (29), `entity`, `chain_party`, `obligation` and the facts below. Every fact: tag F
(filed URL / basis disclosed), R (press, research notes, basis reported) or M (presumed, inferred,
estimated: method ``legacy-<file>@2026-10-20``; values mapped by a fixed rule here:
``seed-sites-rules@2026-10-21``); valid_from = the record's date, else its file's as-of; producer
``seed:sites``; status published. Missing values are not written (n/d). Idempotent: rows upsert,
facts dedupe on their key.

| subject_type | field | unit | meaning |
|---|---|---|---|
| site | place | - | place label (memo/titles.json [1]) |
| site | headline | - | one-line description of the project (memo.headline) |
| site | county / state | - | county and state as written in the dossier (may list several) |
| site | location / land | - | location text; land / title arrangement |
| site | coordinates | deg | {lat, lon, accuracy_note, coord_src, campus_key} (q3x/site.json); M when the campus point is shared with another case |
| site | critical_it_mw | MW | critical IT capacity (dossier capacity; gpu/site_power it_mw.value) |
| site | gross_mw | MW | gross / utility MW (dossier; site_power; dcmap) |
| site | delivered_mw | MW | MW delivered to date |
| site | planned_it_mw | MW | planned IT MW (dcmap it_mw_plan) |
| site | it_mw_estimate | MW | third-party IT MW estimate (M) |
| site | pct_complete | pct | construction percent complete |
| site | expected_energization | - | scheduled completion / energization, as stated (free text) |
| site | original_energization | - | the original schedule, as stated |
| site | first_delivery | - | {text, source_text}: first capacity delivery as stated (q3x/site.json) |
| site | construction_stage | - | Marden read of build progress ('on track', 'behind schedule') (M) |
| site | power_iso / power_utility | - | ISO / RTO and serving utility (site_power, dcmap) |
| site | power_supply / onsite_generation / power_contract | - | supply type, on-site plant, power contract text |
| site | power_source | - | types.ts PowerSource from the supply text: Grid / On-site gas / Hybrid (M rule) |
| site | power_source_class / power_status / power_detail | - | dcmap power class, status and detail text |
| site | power_payer | - | who pays for power |
| site | rent_start | - | rent commencement / start as stated (ctr.start; the single dossier lease) |
| site | outside_date | - | lease outside date as stated (ctr.outside; dossier lease[].outside_date) |
| site | lease_tenant / lease_term / lease_rent_basis / lease_termination / lease_renewal / lease_guarantee | - | main contract terms (q3x/ctr.json) |
| site | lease_term_years | years | main contract term |
| site | lease_tcv_usd / lease_annual_rent_usd | USD | contract value / annual rent |
| site | lease_kind | - | contract kind: lease, gpu, eq, pool (M) |
| site | leases | - | every dossier lease row: [{tenant, mw, term_years, start, tcv_usd, annual_rent_usd, rent_basis, termination_rights, outside_date, renewals, inferred, source_url, quote}] |
| site | chain_parties | - | memo.chain parties (disclosed / reported): [{role, name, what, basis, ticker, group, entity_ids, tag}] |
| site | chain_parties_presumed | - | memo.chain parties whose basis is presumed (M) |
| obligation | edge_type | - | types.ts EdgeType of the edge |
| obligation | linked_sites | - | site slugs the source links the edge to |
| obligation | linked_instruments | - | the linked cases' own instruments (aiwcredit.rowinfo.c) (M) |
| obligation | payer_instruments | - | the payer's instruments exposed to the site (rowinfo.x tenant links) (M) |
| obligation | conditions | - | contract, backstop, termination, renewal and coverage terms (text list) |
| obligation | contracts | - | per source record: aichain edge, ctr contract or credit-support item details |
| obligation | total_usd / annual_usd | USD | total contract (or guaranteed) value / annual payment, when one record gives it |
| obligation | start / outside_date | - | start and outside date as stated |
| obligation | term_years | years | contract term |
| obligation | status | - | types.ts EdgeStatus: Started (start text says commenced / accepted / live) or Signed (filed contract) (M rule) |
| obligation | evidence | - | {date, text, url}, one row per source record (read every published row, not only the current one) |

Entity, chain_party and obligation rows hold identity only (names, CIK, ticker, joins).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from marden import cli
from marden.core import db, heartbeat
from marden.core.db import Conn
from marden.core.sources import get_spec, upsert_spec
from marden.seed.common import SEED_SOURCE_ID
from marden.seed.sites_archive import Archive
from marden.seed.sites_chain import write_chain
from marden.seed.sites_edges import EdgeBuilder
from marden.seed.sites_entities import Registry
from marden.seed.sites_facts import PRODUCER, FactSink
from marden.seed.sites_obligations import write_edges
from marden.seed.sites_parties import seed_all
from marden.seed.sites_rows import upsert_site, write_site_facts

SITE_MAP = Path(__file__).with_name("site_map.json")


def site_map() -> list[tuple[str, str, bool]]:
    """(case id, site slug, in_v1) for every financed case."""
    return [(s["case"], s["slug"], bool(s["in_v1"])) for s in json.loads(SITE_MAP.read_text())["sites"]]


def _table_counts(conn: Conn) -> dict[str, Any]:
    one = lambda sql: conn.execute(sql).fetchone()["n"]  # noqa: E731
    facts = conn.execute(
        "SELECT subject_type, count(*) AS n FROM fact WHERE producer = %s GROUP BY 1 ORDER BY 1", (PRODUCER,)
    ).fetchall()
    return {
        "site": one("SELECT count(*) AS n FROM site"),
        "chain_party": one("SELECT count(*) AS n FROM chain_party"),
        "obligation": one("SELECT count(*) AS n FROM obligation"),
        "entity": one("SELECT count(*) AS n FROM entity"),
        "document_seed": one(f"SELECT count(*) AS n FROM document WHERE source_id = '{SEED_SOURCE_ID}'"),
        "fact_sites": {r["subject_type"]: r["n"] for r in facts},
    }


def load_into(conn: Conn, cases: list[str] | None = None) -> dict[str, Any]:
    """Load the sites seed on `conn` (the caller owns the transaction). `cases` limits the run."""
    sites = [s for s in site_map() if cases is None or s[0] in cases]
    slugs = {case: slug for case, slug, _ in sites}
    arc = Archive(conn)
    arc.load_all(list(slugs))
    reg = Registry(conn, arc.tickers)
    chain = seed_all(arc, reg, list(slugs))
    sink = FactSink(conn)
    links = 0
    for case, slug, in_v1 in sites:
        upsert_site(conn, arc, case, slug, in_v1)
    for case, slug, _ in sites:
        write_site_facts(sink, arc, case, slug)
        links += write_chain(conn, sink, arc, reg, case, slug, chain[case])
    builder = EdgeBuilder(arc, reg, chain, slugs)
    edges = builder.build()
    write_edges(conn, sink, arc, edges, slugs)
    return {
        "sites": len(sites), "chain_links": links, "obligations": len(edges),
        "facts_seen": sink.seen, "facts_new": sink.created, "documents": len(arc.sources),
        "fields": sink.fields(), "skipped_parties": sorted(reg.skipped), "skipped_edges": builder.skipped,
        "tables": _table_counts(conn),
    }


def load(cases: list[str] | None = None) -> dict[str, Any]:
    """One recorded run of the loader (source_run of `seed_marden_data`), in one transaction."""
    upsert_spec(get_spec(SEED_SOURCE_ID))
    with heartbeat.run(SEED_SOURCE_ID) as hb:
        with db.transaction() as conn:
            stats = load_into(conn, cases)
        hb.seen(stats["facts_seen"])
        hb.new(stats["facts_new"])
        hb.meta.update({"loader": "sites", "sites": stats["sites"], "obligations": stats["obligations"]})
    return stats


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--case", action="append", dest="cases", help="load only this case id (repeatable)")


@cli.action("seed", "sites", help="Load financed sites, parties, chain links and obligations", configure=_args)
def seed_sites(args: argparse.Namespace) -> int:
    stats = load(args.cases)
    print(json.dumps(stats, indent=2, default=str))
    return 0
