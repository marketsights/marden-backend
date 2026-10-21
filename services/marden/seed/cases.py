"""Seed loader `cases`: the per-site research behind the 29 financed cases.

    cd services && uv run python -m marden seed cases [--case JUPITER ...]

Reads (each stored raw first as a `document` of `seed_marden_data`): aiwc/cases/<CASE>.json,
aiwc/memo/<CASE>.json, aiwc/params/<CASE>.json, aiwc/out/<CASE>.json, aiwc/aiw_proj.json,
aiwcredit.json (cases[], gpu) and aiwc/q3x/{underwater,rec,trg,contagion,oa,rv,comp,fac}.json.
Run after `sites` and `instruments`: facts land on existing sites (site_map.json slugs), loaded
instruments and existing parties (operator and useful-life companies are created if missing).

Tags: F filed URL or basis 'disclosed', R press / ratings / the archive's research without a URL
('Marden research archive'), M the archive's judgments and model values (method
``legacy-<file>@2026-10-20``). valid_from = the record's own date, else the file as-of (cases
2026-10-17; aiwcredit 2026-10-20; rec 10-19, trg 10-18, contagion and oa 10-15, rv and comp 10-20;
params/out 2026-10-17). Producer ``seed:cases``; facts published; n/d values are not written.
Price rule (docs/methods/price-sources.md): this loader writes no price. Prices, yields, spreads,
price moves and text quoting TRACE / Frankfurt / Yahoo values are removed (cases_quarantine).
Legacy model tables go to ``model_output`` (version ``legacy@2026-10-20``), never ``price_obs``.

| subject_type | field | unit | meaning (spec 26 checklist area) |
|---|---|---|---|
| site | debt_items | USD | dossier debt rows, no prices: [{name, issuer, amount_usd, coupon, maturity, amortization, |
| | | | rank, rating, drawn_usd, commitment_usd, holders, covenants, instrument_ids, source_url}] (Structure) |
| site | tranches, tranche_summary | USD | memo instruments [{name, borrower, amount_usd, terms, ranking, holders, |
| | | | modeled, instrument_id}] and the memo's short capital stack (Structure: tranches, seniority) |
| site | structure_term | - | one per memo security term {key, text, short, basis}, quote kept (Structure: security, |
| | | | guarantees, reserves, backstops; Lease: term, termination; Permits and land: title) |
| site | collateral, reserves | - | {description, excludes_gpus, inferred_gpus}; reserve text (Structure) |
| site | credit_support | USD | one per item {type, provider, amount_usd, covers} (Structure: completion guarantee) |
| site | covenants, deal_metrics | - | dossier covenant tests; rating-agency DSCR metrics (Structure: covenants) |
| site | sponsor, guarantor, lenders, fund_holders, sponsor_liquidity, sponsor_note | - | dossier parties (Parties) |
| site | tenant_rating, sponsor_rating, deal_rating | - | rating text, R (Parties; Structure: rating triggers) |
| site | contractors | - | memo chain 'Contractor' rows (Parties: general contractor; Contractor) |
| site | holders_text | USD | {funds: [{fund, par_usd}], bdcs, banks} (Parties: holders) |
| site | total_project_cost_usd, cost_to_complete_usd | USD | dossier cost (Budget and cost) |
| site | cost_overrun, cost_note, cost_basis, advance_rates | - | dossier cost text (Budget and cost) |
| site | facility_terms | USD | {commitment_usd, drawn_usd, drawn_asof, draw_period_end, ...} (Draws and funding) |
| site | debt_facility | USD | one per q3x fac row: {fid, size_usd, drawn_usd, lenders, collateral, advance_rate, |
| | | | covenants, chips, gpu_count, customer, ...} (Draws and funding; Equipment: GPUs) |
| site | risk_power, risk_permits, risk_water, risk_construction, risk_tenant, risk_sponsor, risk_other | - | |
| | | | {status, detail, level 0-3} (M) (Power, Permits, Construction, Opposition) |
| site | risk_evidence | - | one per dated item {driver, date, headline, url}, quote kept |
| site | watch_sources | - | [{kind, agency, id, url, query, why, page_ok}] pages and dockets to monitor (M) |
| site | status_summary, research_note, research_notes, model_summary, model_verdict | - | archive prose (M) |
| site | issue | - | one per scored issue {driver, category, level, title, what, why, impact, next_date, |
| | | | next_event, clears_if, sources} (M) |
| site | open_questions | - | [{item, short, area}] unknowns and terms marked not disclosed: checklist n/d (M) |
| site | delivery_project | USD | one per tracker entry {issuer, project, mw, counterparty, term, status, |
| | | | financing} (M) |
| site | rfs_statement | - | one per dated ready-for-service statement {project, which, text, date} |
| | | | (Construction: milestones) |
| site | schedule_change | - | one per dated schedule change {project, date, what}, quote kept (Construction) |
| site | power_cost_estimate | USD | {it_mw, pue, power price, annual cost 2026/2030, payer, gpu_generation} |
| | | | (M) (Power) |
| instrument | amortization, ranking_text, holders_text, covenants_text | - | dossier debt row / operator facility |
| instrument | drawn_usd, commitment_usd | USD | drawn and committed amounts (Draws and funding) |
| instrument | rate_text, collateral_text, customer_text | - | operator facility terms from the 10-Q |
| instrument | research_terms | - | memo terms, ranking, holders of the instrument |
| instrument | covenant_test | - | trigger {facility, metric, threshold, consequence, section}, F, quote kept |
| instrument | covenant_headroom | - | {current, headroom_pct, status, calculation, watch} (M) |
| instrument | agreement_term | - | one per quoted term {agreement, type, threshold, consequence, section}, F |
| instrument | gpu_collateral | USD | {chip, balance_usd, gpu_cost_usd, chips_value_*_usd, ltv_pct, underwater_year_*, |
| | | | units, ltv_by_quarter} (M) (Equipment: GPU collateral) |
| instrument | gpu_model | - | chip model, tag by its basis |
| instrument | recovery_waterfall, recovery_project, recovery_gpu | pct | recovery ranges (M) |
| instrument | end_user_share | USD | {end_user OpenAI, share, end_user_usd, bucket, chain} (M) (Parties: end user) |
| entity | site_role | - | one per memo chain row {site_slug, case, role, name, what, basis} (Parties) |
| entity | revenue_q_usd, revenue_ttm_usd, adj_ebitda_q_usd, interest_expense_q_usd, capex_q_usd, | USD | |
| | depreciation_q_usd, backlog_rpo_usd, gpu_secured_debt_usd, other_debt_usd | | operator profile |
| entity | active_it_mw, contracted_mw | MW | operator profile |
| entity | gpu_useful_life_years, gpu_useful_life_note | years | depreciation life (10-Q; American Compute, R) |
| entity | top_customers, contract_terms, power_cost, operator_role, operator_notes (M) | - | operator profile |
| entity | gpu_secured_facilities | USD | [{name, amount_usd, drawn_usd, rate, maturity, collateral, instrument_id}] |
| entity | rating_summary | - | {rating, notches_above_high_yield} (M) |
| entity | funding_fact | - | OpenAI revenue / burn / contracts as reported {label, value, note} (R) |
| series | issue_comp:<fid> issue_spread_bp | bp | spread at issue (SOFR margin as stated; fixed: yield - UST, M) |
| series | issue_comp:<fid> tier, issue_date, deal_terms | - | tier (M), close date, terms and documents |
| series | gpu_list_price:<provider>:<model> usd_per_gpu_hr, label | USD/GPU-hr | provider list price (R) |
| series | gpu_resale:<model> usd_per_gpu, condition | USD | resale point (R) |
| series | gpu_residual:<model> residual_pct_of_new | pct | residual bands by year (M) |
| series | gpu_model:<model> name, first_volume_ship, tdp_w, fp8_dense_pflops, new_price_usd | - | chip facts |
| series | data_center_pue pue_range; eia_industrial_price:<ST> cents_per_kwh; gpu_market licence_notes | - | |

model_output: legacy_credit_sim (site and modeled instruments: outputs, sensitivities, buildup,
inputs), legacy_relative_value (site), legacy_recovery (coreweave; sites), legacy_gpu_ltv,
legacy_rating_triggers, legacy_issue_comps (series), legacy_contagion (tenant entities),
legacy_openai_exposure (openai). event: kind 'catalyst' on the site for each memo issue's dated next
step. Spec 26 areas with no source in these files (n/p or other loaders): Insurance, Independent
engineer, mechanic's liens; permits, power and opposition records are the `facilities` loader's.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from marden import cli
from marden.core import db, heartbeat
from marden.core.db import Conn
from marden.core.sources import get_spec, upsert_spec
from marden.seed.cases_archive import COMP, OA, TRG, Archive, Parties
from marden.seed.cases_credit import (
    write_agreement_terms,
    write_comp_summary,
    write_comps,
    write_contagion,
    write_openai_shares,
    write_relative_value,
    write_trigger_rows,
    write_trigger_summary,
)
from marden.seed.cases_dossier import write_dossier
from marden.seed.cases_gpu import (
    ensure_operator_entities,
    write_collateral,
    write_market,
    write_operators,
    write_useful_lives,
)
from marden.seed.cases_memo import write_memo
from marden.seed.cases_model import write_case_notes, write_model
from marden.seed.cases_schedule import write_facilities, write_power_costs, write_projects
from marden.seed.cases_sink import PRODUCER, EventSink, FactSink, ModelSink, Sections
from marden.seed.common import SEED_SOURCE_ID

LOADER = "cases"


def chunked[T](items: list[T], size: int) -> list[list[T]]:
    return [items[i: i + size] for i in range(0, len(items), max(1, size))]


def table_counts(conn: Conn) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT subject_type, count(*) AS n FROM fact WHERE producer = %s GROUP BY 1 ORDER BY 1", (PRODUCER,))
    out: dict[str, Any] = {"fact_cases": {r["subject_type"]: r["n"] for r in rows}}
    out["fact_cases_by_tag"] = {r["tag"]: r["n"] for r in conn.execute(
        "SELECT tag, count(*) AS n FROM fact WHERE producer = %s GROUP BY 1 ORDER BY 1", (PRODUCER,))}
    out["model_output"] = {r["model"]: r["n"] for r in conn.execute(
        "SELECT model, count(*) AS n FROM model_output WHERE version = 'legacy@2026-10-20' GROUP BY 1 ORDER BY 1")}
    out["model_run"] = conn.execute(
        "SELECT count(*) AS n FROM model_run WHERE trigger = %s", (PRODUCER,)).fetchone()["n"]
    out["event_catalyst_from_memos"] = conn.execute(
        """SELECT count(*) AS n FROM event e JOIN document d ON d.id = e.document_id
           WHERE d.source_id = %s AND d.url LIKE %s AND e.kind = 'catalyst'""",
        (SEED_SOURCE_ID, "%/aiwc/memo/%")).fetchone()["n"]
    for t in ("entity", "document"):
        out[t] = conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]
    return out


def load_into(conn: Conn, *, cases: list[str] | None = None) -> dict[str, Any]:
    """Load the cases seed on `conn`, section by section (cases_sink.Sections)."""
    arc = Archive(conn, frozenset(cases) if cases is not None else None)
    facts, events, models = FactSink(conn), EventSink(conn), ModelSink(conn)
    stats: dict[str, Any] = {"skipped": {}}
    sections = Sections(conn, (facts, events, models), stats)
    run = sections.run
    run("archive", arc.load_all)

    def entities() -> tuple[Parties, dict[str, str]]:
        parties = Parties(conn)  # rebuilt on a retry, so it never holds a rolled-back entity
        return parties, ensure_operator_entities(arc, parties)

    parties, eids = run("entities", entities)
    for case, slug in sorted(arc.slugs.items()):
        run("dossier", lambda c=case, s=slug: write_dossier(facts, arc, c, s, stats))
        run("memo", lambda c=case, s=slug: write_memo(facts, events, arc, parties, c, s, stats))
        run("model", lambda c=case, s=slug: write_model(models, arc, c, s, stats))
        run("case_notes", lambda c=case, s=slug: write_case_notes(facts, arc, c, s))
        run("projects", lambda c=case, s=slug: write_projects(facts, arc, c, s, stats))
    stats["facility_rows"] = run("facilities", lambda: write_facilities(facts, arc, arc.slugs))
    stats["power_cost_rows"] = run("power_costs", lambda: write_power_costs(facts, arc, arc.slugs))
    run("gpu_collateral", lambda: write_collateral(facts, models, arc))
    run("gpu_market", lambda: write_market(facts, arc, stats))
    for ops in chunked(arc.gpu.get("ops") or [], 2):
        run("operators", lambda ops=ops: write_operators(facts, arc, eids, ops))
    run("useful_lives", lambda: write_useful_lives(facts, arc, eids))
    for rows in chunked(arc.data[TRG].get("rows") or [], 5):
        run("triggers", lambda rows=rows: write_trigger_rows(facts, arc, rows, stats))
    for agreements in chunked(arc.data[TRG].get("terms") or [], 2):
        run("agreement_terms", lambda a=agreements: write_agreement_terms(facts, arc, a, stats))
    run("trigger_summary", lambda: write_trigger_summary(facts, models, arc, parties, stats))
    run("contagion", lambda: write_contagion(facts, models, arc, parties, stats))
    for rows in chunked(arc.data[OA].get("rows") or [], 30):
        run("openai_shares", lambda rows=rows: write_openai_shares(facts, arc, rows))
    run("relative_value", lambda: write_relative_value(models, arc, stats))
    for rows in chunked(arc.data[COMP].get("rows") or [], 10):
        run("comps", lambda rows=rows: write_comps(facts, arc, rows))
    run("comp_summary", lambda: write_comp_summary(models, arc))
    stats.update({
        "sites": len(arc.slugs), "cases_without_site": arc.unsited, "documents_read": len(arc.sources),
        "facts_seen": facts.seen, "facts_new": facts.created, "facts_by_subject": facts.by_subject(),
        "model_outputs_seen": sum(models.counts.values()), "model_outputs_new": models.created,
        "model_outputs": dict(sorted(models.counts.items())), "events_new": events.created,
        "events": dict(events.counts), "fields": facts.fields(), "nd_skipped": dict(sorted(facts.skipped.items())),
        "unresolved_party_names": sorted(parties.unresolved), "retries": sections.retries,
        "tables": table_counts(conn),
    })
    return stats


def load(cases: list[str] | None = None) -> dict[str, Any]:
    """One recorded run of the loader (source_run of `seed_marden_data`)."""
    upsert_spec(get_spec(SEED_SOURCE_ID))
    with heartbeat.run(SEED_SOURCE_ID) as hb:
        # Autocommit: each section's transaction commits on its own (see cases_sink.Sections).
        with db.connect(autocommit=True) as conn:
            stats = load_into(conn, cases=cases)
        hb.seen(stats["facts_seen"] + stats["model_outputs_seen"])
        hb.new(stats["facts_new"] + stats["model_outputs_new"] + stats["events_new"])
        hb.meta.update({"loader": LOADER, "sites": stats["sites"], "facts_new": stats["facts_new"],
                        "model_outputs_new": stats["model_outputs_new"], "events_new": stats["events_new"]})
    return stats


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--case", action="append", dest="cases", help="load only this case id (repeatable)")


@cli.action("seed", LOADER, help="Load per-site case research: structure, risks, schedule, GPU collateral, "
            "triggers, comps and legacy model outputs", configure=_args)
def seed_cases(args: argparse.Namespace) -> int:
    print(json.dumps(load(args.cases), indent=2, default=str))
    return 0
