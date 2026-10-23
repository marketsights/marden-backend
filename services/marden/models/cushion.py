"""Cushion model (spec 22 "Cushion"; specs 03, 04): months between likely rent start and the lease outside date.

    cd services && uv run python -m marden model cushion [--as-of YYYY-MM-DD] [--site SLUG ...]

For every V1 site (or the slugs given): the eight critical-path milestones from its facts and events
(cushion_milestones, cushion_power), each pending milestone's slip drawn from history (cushion_history:
EIA-860M planned vs actual and the archive's dated plan revisions), 10,000 simulated schedules
(cushion_sim) -> P10/P50/P90 rent-start months, the outside date (a year-only date is a range across that
year: `oddsOfMissingRange` and `cushionMonthsRange` from best case Dec 31 to worst case Jan 1, and the
midpoint, Jul 1, in `outsideDateAssumed` is what cushionMonths and oddsOfMissing, the single values other
models and alerts read, use; the price model reads only the P50 rent start, which the deadline does not move), cushionMonths = outside - P50, deltaWeek against the stored run 7 days
earlier, oddsOfMissing ('odds v0', uncalibrated: 'model v0' label) and costPerMonthLate with its parts in
`lateCost` (marden.models.late_cost: construction interest on the site's debt, plus rent not collected while
rent start is ahead; who bears each part as the site's structure terms state it). Output: one `model_output`
per site and as_of (model 'cushion', version 'cushion v0.1') =
{projection: CushionProjection, milestones: Milestone[], driver, missing, notApplicable, basis,
inputsHash, leaseOutcomes, leasePosition (the Lease terms tab's 'what the terms mean' and 'today')}; a
missing input gives null fields and a `missing` entry, never a guess.

Runs: daily 09:35 UTC (05:35 EDT; spec 22 'Daily, 5-6 am ... cushions'), which also gives the weekly run
and keeps one output per day for deltaWeek and cushionHistory; and within a minute of any published fact
or event on its inputs (marden.models.triggers: the site's schedule, power, lease and permit facts and
events; serving plants; any site's dated plan revisions and EIA-860M rows feed every site's slip history).
After a run the site's read models are marked dirty (`mark_dirty_for('site', slug, '@model:cushion')`).
A past --as-of is an honest backtest: inputs are what was public that day (marden.models.pit).
"""

from __future__ import annotations

import argparse
import logging
from contextlib import nullcontext
from datetime import date, timedelta
from typing import Any

from marden import cli
from marden.core import db
from marden.core.db import Conn
from marden.core.jsonutil import canonical_json, hash_parts
from marden.core.queue import app
from marden.models import cushion_output as out
from marden.models import late_cost, pit, runs, triggers
from marden.models.cushion_history import SlipHistory, load_history
from marden.models.cushion_inputs import PLANT_FIELDS, SITE_FIELDS, SiteInputs, load_sites
from marden.models.cushion_power import build
from marden.models.cushion_sim import simulate
from marden.readmodels.context import business_today
from marden.readmodels.deps import mark_dirty_for, via

log = logging.getLogger(__name__)

MODEL, VERSION = out.MODEL, out.VERSION
DAILY_CRON = "35 9 * * *"
HISTORY_FIELDS = ("rfs_statement", "original_energization", "expected_energization")


def v1_sites(conn: Conn) -> list[str]:
    return [r["slug"] for r in conn.execute("SELECT slug FROM site WHERE in_v1 ORDER BY slug").fetchall()]


def project_site(site: SiteInputs, history: SlipHistory, prior: dict[str, Any] | None,
                 history_hash: str, late: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    """One site's output and its inputs hash (the simulation seed, so equal inputs give equal outputs)."""
    milestones, mode = build(site)
    inputs_hash = hash_parts(MODEL, VERSION, canonical_json(site.fingerprint()), history_hash)
    sim = simulate(milestones, site.as_of, history, site.slug, seed=int(inputs_hash[:15], 16))
    rent = next(m for m in milestones if m.key == "rent_start")
    projection = out.projection_doc(site, sim, prior, late, rent.is_done)
    outcomes, position = out.lease_view(site, sim)
    basis: dict[str, Any] = {
        "label": out.LABEL,
        "methodVersion": VERSION,
        "simulations": len(sim.rent) if sim is not None else 0,
        "powerMode": mode,
        "rentStart": rent.note or "rent_start fact",
        "outsideDate": ({"basis": site.outside.basis, "quote": site.outside.quote[:300],
                         **({"yearOnly": f"the lease gives {site.outside.value} only: tested from Jan 1 (worst case) "
                                         f"to Dec 31 (best case); cushionMonths and oddsOfMissing, the single values "
                                         f"other models and alerts read, use the midpoint {site.outside.assumed}"}
                            if site.outside.year_only else {})} if site.outside else None),
        "rent": ({"basis": site.rent.basis, "monthlyUsd": round(site.rent.monthly_usd)} if site.rent else None),
        "slipHistory": {**history.counts(), "byMilestone": sim.slip_basis if sim else {}},
        "simulated": sim.simulated if sim else [],
        "bindingShare": sim.binding_share if sim else {},
        "delayMonths": sim.delay_months if sim else {},
        "p50ChangeWeek": out.p50_change(projection["p50"], prior),
        "assumptions": [
            "no lag between a milestone and its successors (finish no earlier than predecessors)",
            "milestone slips are drawn independently from the slip history",
            "a stated window is uniform over its months; 'early' = Jan-Apr, 'mid' = May-Aug, 'late' = Oct-Dec",
        ],
    }
    output = {
        "projection": projection,
        "milestones": out.milestones_doc(milestones, sim),
        "driver": sim.driver if sim is not None else None,
        "missing": out.missing_list(site, milestones, sim, history),
        "leaseOutcomes": outcomes,
        "leasePosition": position,
        "notApplicable": [m.key for m in milestones if not m.applicable],
        "basis": basis,
        "inputsHash": inputs_hash,
    }
    return output, inputs_hash


def run(as_of: date | None = None, slugs: list[str] | None = None, *, trigger: str = "manual",
        mark: bool = True) -> dict[str, Any]:
    """Run the model for `slugs` (default: every V1 site) as of `as_of` (default: today, US Eastern)."""
    day = as_of or business_today()
    if day > business_today():
        raise ValueError(f"cannot run the cushion model for a future day ({day.isoformat()})")
    run_id = runs.start_run(MODEL, VERSION, trigger=trigger)
    try:
        with db.transaction() as conn:
            targets = slugs or v1_sites(conn)
            history = load_history(conn, day)
            history_hash = hash_parts(canonical_json([r.doc() for r in history.records]))
            sites = load_sites(conn, day, targets)
            results: dict[str, Any] = {}
            hashes = []
            for site in sites:
                week_ago = runs.output_on(conn, MODEL, "site", site.slug, day - timedelta(days=7))
                prior = week_ago["output"] if week_ago is not None else None  # the run exactly 7 days earlier
                late = late_cost.debt_part(conn, site.slug, day)
                output, inputs_hash = project_site(site, history, prior, history_hash, late)
                runs.write_output(conn, run_id, MODEL, VERSION, "site", site.slug, day, output)
                if mark:
                    mark_dirty_for("site", site.slug, f"@model:{MODEL}", conn=conn, reason=f"model_run:{run_id}")
                results[site.slug] = output
                hashes.append(inputs_hash)
        runs.finish_run(run_id, ok=True, inputs_hash=hash_parts(*sorted(hashes)))
    except Exception as exc:
        runs.finish_run(run_id, ok=False, error=f"{type(exc).__name__}: {exc}")
        raise
    return {"run_id": run_id, "as_of": day.isoformat(), "sites": results,
            "unknown": sorted(set(targets) - set(results))}


# ---------------------------------------------------------------- triggers and schedule
def run_keys(keys: list[str], trigger: str) -> dict[str, Any]:
    slugs = None if "*" in keys else keys
    result = run(slugs=slugs, trigger=trigger)
    return {"run_id": result["run_id"], "sites": len(result["sites"])}


_PLANT_SITES = via(
    """SELECT DISTINCT s.slug FROM fact f, jsonb_array_elements_text(
              CASE WHEN jsonb_typeof(f.value) = 'array' THEN f.value ELSE '[]' END) AS s(slug)
        WHERE f.subject_type = 'plant' AND f.subject_id = %s AND f.field = 'serves_sites' AND f.status = 'published'"""
)
_OBLIGATION_SITES = via(
    """SELECT DISTINCT s.slug FROM fact f, jsonb_array_elements_text(
              CASE WHEN jsonb_typeof(f.value) = 'array' THEN f.value ELSE '[]' END) AS s(slug)
        WHERE f.subject_type = 'obligation' AND f.subject_id = %s AND f.field = 'linked_sites'
          AND f.status = 'published'"""
)


_INSTRUMENT_SITE = via("SELECT site_slug AS slug FROM instrument WHERE id = %s AND site_slug IS NOT NULL")


def _install_triggers() -> None:
    for fld in SITE_FIELDS:
        triggers.model_depends(MODEL, "site", fld)
    triggers.model_depends(MODEL, "site", "@event")
    for fld in HISTORY_FIELDS:  # every site's slip pool
        triggers.model_depends(MODEL, "site", fld, triggers.everything)
    for fld in PLANT_FIELDS:
        triggers.model_depends(MODEL, "plant", fld, _PLANT_SITES)
    triggers.model_depends(MODEL, "plant", "@event", _PLANT_SITES)
    triggers.model_depends(MODEL, "obligation", "outside_date", _OBLIGATION_SITES)
    for fld in late_cost.INSTRUMENT_FIELDS:  # the month-late cost's interest part
        triggers.model_depends(MODEL, "instrument", fld, _INSTRUMENT_SITE)
    for fld in ("planned_online", "operating_month"):
        triggers.model_depends(MODEL, "eia_generator", fld, triggers.everything)
    triggers.register_runner(MODEL, run_keys)


_install_triggers()


@app.periodic(cron=DAILY_CRON, periodic_id="cushion_daily")
@app.task(name="models.cushion_daily", queue="models", queueing_lock="models.cushion_daily", lock="models.cushion")
def cushion_daily_task(timestamp: int | None = None) -> dict[str, Any]:
    result = run(trigger="schedule")
    return {"run_id": result["run_id"], "sites": len(result["sites"])}


@app.task(name="models.cushion", queue="models", lock="models.cushion")
def cushion_task(as_of: str | None = None, slugs: list[str] | None = None) -> dict[str, Any]:
    result = run(date.fromisoformat(as_of) if as_of else None, slugs, trigger="task")
    return {"run_id": result["run_id"], "sites": len(result["sites"])}


# ---------------------------------------------------------------- CLI
def _configure(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as-of", type=date.fromisoformat, default=None, help="run date (default: today, US Eastern)")
    p.add_argument("--site", action="append", dest="sites", help="site slug (repeatable; default: every V1 site)")
    p.add_argument("--no-mark", action="store_true", help="do not mark the sites' read models dirty")
    p.add_argument("--audit-inputs", action="store_true",
                   help="list the inputs read by public date; exit 1 if one was not yet public on the as-of day")


def _dash(value: Any) -> Any:
    return "—" if value is None else value


def summary_rows(result: dict[str, Any]) -> list[str]:
    lines = [f"{'site':28} {'p10':8} {'p50':8} {'p90':8} {'outside':8} {'cushion':>7} {'odds':>5} {'dWk':>4} "
             f"{'driver':8} missing"]
    for slug, o in sorted(result["sites"].items()):
        p = o["projection"]
        cushion = (p.get("cushionMonths") or {}).get("value")
        odds = (p.get("oddsOfMissing") or {}).get("value")
        lines.append(
            f"{slug:28} {p['p10'] or 'n/d':8} {p['p50'] or 'n/d':8} {p['p90'] or 'n/d':8} "
            f"{p['outsideDate']['value'] or 'n/d':8} {'—' if cushion is None else cushion:>7} "
            f"{'—' if odds is None else odds:>5} {_dash(p['deltaWeek']['value']):>4} "
            f"{o['driver'] or 'n/d':8} {len(o['missing'])}")
    return lines


@cli.action("model", "cushion", help="Run the cushion model (rent-start P10/P50/P90 and cushion per site)",
            configure=_configure)
def cushion_command(args: argparse.Namespace) -> int:
    with pit.audit() if args.audit_inputs else nullcontext([]) as used:
        result = run(args.as_of, args.sites, trigger="manual", mark=not args.no_mark)
    print(f"cushion run {result['run_id']} as of {result['as_of']}: {len(result['sites'])} sites")
    for line in summary_rows(result):
        print(line)
    if result["unknown"]:
        print(f"unknown site slugs: {', '.join(result['unknown'])}")
        return 1
    if args.audit_inputs:
        lines, late = pit.audit_report(used, result["as_of"])
        print("\n".join(lines))
        return 1 if late else 0
    return 0
