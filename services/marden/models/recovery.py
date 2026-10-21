"""Model `recovery` (spec 22 "Recovery"): what lenders get, cents per dollar, if the tenant or customer walks.

    python -m marden model recovery [--as-of YYYY-MM-DD] [--instrument ID ...] [--json]

Collateral waterfall per instrument, by what secures it:
- GPU-backed facilities (marden.models.gpu_facilities): units x value per unit (rental index, decline and
  depreciation life; gpu_value) less the liquidation haircut, over the pool's claims (pari passu, nothing
  known ahead of the first lien), for a customer walking today to a year out;
- site-backed debt (marden.models.recovery_shell): finished powered capacity x value per MW from the cost
  comps (25th / 50th / 75th percentile), less claims ranking ahead, over the claims at its rank;
- anything else (corporate unsecured notes, converts): n/d, the issuer waterfall is not modelled.
One `model_output` per instrument (subject ('instrument', id)) = {recovery: Sourced<string> ('62-74' with an en dash,
cents per dollar; InstrumentDetail.recovery), valueB: Sourced<number> (collateral value, $B, base case),
method: 'gpu_collateral' | 'powered_shell' | null, missing, basis}. Ranges are uncalibrated ('model v0').

Runs: weekly, Monday 09:41 UTC (spec 22 "Weekly ... recovery"), and within a minute of an input: the
instrument's terms, its site's capacity, cost and completion facts, any site's cost comps, the GPU market
inputs and the gpu_collateral output. A run for today marks the instrument's read models
(`@model:recovery`: instrument_detail, site debt tab, ...); a past --as-of is a backtest.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from typing import Any

from marden import cli
from marden.core.db import Conn
from marden.core.queue import app
from marden.models import triggers
from marden.models.gpu_facilities import FACILITY_FIELDS, pools, range_text, value_facility
from marden.models.gpu_facilities import load as load_facilities
from marden.models.gpu_inputs import ENTITY_FIELDS, SERIES_FIELDS, SERIES_PREFIXES
from marden.models.gpu_inputs import load as load_market
from marden.models.gpu_value import HAIRCUT, fit_decline
from marden.models.recovery_shell import (
    INSTRUMENT_FIELDS,
    SITE_FIELDS,
    comps,
    load_sites,
    pue_mid,
    shell_recovery,
    site_claims,
)
from marden.models.sourced import Batch, m_value, method_version, rounded, run_outputs
from marden.readmodels.deps import via

MODEL = "recovery"
VERSION = "0.1"
METHOD = method_version(MODEL, VERSION)
CRON_UTC = "41 9 * * 1"
SOURCE = "Marden recovery model"
UNSECURED = "issuer balance-sheet waterfall (unsecured or unsited debt is not modelled in v0)"


def _m(value: Any, day: date) -> dict[str, Any]:
    return m_value(value, day, METHOD, source_name=SOURCE, uncalibrated=True)


def _targets(conn: Conn, ids: list[str] | None) -> list[str]:
    rows = conn.execute(
        """SELECT id FROM instrument WHERE (%(ids)s::text[] IS NULL AND (in_v1 OR site_slug IS NOT NULL OR id IN (
                  SELECT subject_id FROM fact WHERE subject_type = 'instrument'
                     AND field IN ('gpu_model', 'gpu_collateral'))))
               OR id = ANY(%(ids)s) ORDER BY id""",
        {"ids": ids},
    ).fetchall()
    return [r["id"] for r in rows]


def compute(conn: Conn, day: date, ids: list[str] | None) -> Batch:
    targets = _targets(conn, ids)
    market = load_market(conn, day)
    fit = fit_decline(market, market.median_life())
    facilities = load_facilities(conn, day)
    by_id = {f.instrument_id: f for f in facilities}
    by_pool = pools(facilities)
    sites = load_sites(conn, day)
    comp = comps(sites)
    pue, _ = pue_mid(conn, day)
    site_of = {r["id"]: r["site_slug"] for r in conn.execute(
        "SELECT id, site_slug FROM instrument WHERE id = ANY(%s)", (targets,)).fetchall()}
    batch = Batch()
    for iid in targets:
        out: dict[str, Any]
        if iid in by_id:
            f = by_id[iid]
            v = value_facility(f, market, fit, day, by_pool[f.pool_key])
            out = {"recovery": _m(range_text(v.recovery_low, v.recovery_high), day),
                   "valueB": _m(rounded(v.value / 1e9 if v.value else None, 4), day), "method": "gpu_collateral",
                   "missing": v.missing,
                   "basis": {"methodVersion": f"{METHOD} (model v0)", "poolBalanceB": rounded(
                       v.pool_balance / 1e9 if v.pool_balance else None, 4), "pool": v.pool,
                       "units": rounded(v.units, 0), "haircut": HAIRCUT,
                       "valuePerUnitUsd": rounded(v.per_unit, 0), "seniorClaimsB": 0.0,
                       "rule": "collateral value / pool balance, capped at 100c, default today to a year out"}}
        elif site_of.get(iid):
            slug = site_of[iid]
            claims = site_claims(conn, day, slug, set(by_id))
            res = shell_recovery(sites.get(slug), comp, claims, iid, pue)
            out = {"recovery": _m(range_text(res.low, res.high) if res.base is not None else None, day),
                   "valueB": _m(rounded(res.value[1] / 1e9, 4) if res.value else None, day),
                   "method": "powered_shell", "missing": res.missing,
                   "basis": {"methodVersion": f"{METHOD} (model v0)", "site": slug,
                             "baseCents": None if res.base is None else round(res.base * 100),
                             "seniorClaimsB": rounded(res.ahead / 1e9 if res.ahead is not None else None, 4),
                             "pariClaimsB": rounded(res.pari / 1e9 if res.pari is not None else None, 4), **res.basis}}
        else:
            out = {"recovery": _m(None, day), "valueB": _m(None, day), "method": None, "missing": [UNSECURED],
                   "basis": {"methodVersion": f"{METHOD} (model v0)"}}
        batch.add("instrument", iid, out)
    batch.inputs = [market.origins, sorted(comp.per_mw), targets]
    return batch


def run(as_of: date | None = None, instruments: list[str] | None = None, *, trigger: str = "manual",
        mark: bool = True) -> dict[str, Any]:
    return run_outputs(MODEL, VERSION, as_of, lambda conn, day: compute(conn, day, instruments), trigger=trigger,
                       mark=mark)


def run_keys(keys: list[str], trigger: str) -> dict[str, Any]:
    result = run(instruments=None if "*" in keys else keys, trigger=trigger)
    return {"instruments": len(result["outputs"]), "changed": len(result["changed"]), "run_id": result["run_id"]}


_SITE_INSTRUMENTS = via("SELECT id FROM instrument WHERE site_slug = %s")


def _gpu_series(conn: Conn, subject_id: str) -> list[str]:
    return ["*"] if subject_id.startswith(SERIES_PREFIXES) else []


def _install_triggers() -> None:
    for fld in (*INSTRUMENT_FIELDS, *FACILITY_FIELDS):
        triggers.model_depends(MODEL, "instrument", fld, _sited_instruments)
    triggers.model_depends(MODEL, "instrument", "@model:gpu_collateral")
    for fld in SITE_FIELDS:  # a site's own facts, and every site's cost comps
        triggers.model_depends(MODEL, "site", fld, triggers.everything if fld in ("total_project_cost_usd", "gross_mw",
                                                                                  "cost_basis") else _SITE_INSTRUMENTS)
    for fld in (*SERIES_FIELDS, "value"):
        triggers.model_depends(MODEL, "series", fld, _gpu_series)
    for fld in ENTITY_FIELDS:
        triggers.model_depends(MODEL, "entity", fld, triggers.everything)
    triggers.register_runner(MODEL, run_keys)


def _sited_instruments(conn: Conn, subject_id: str) -> list[str]:
    """The instrument and the others at its site (a new claim changes their share)."""
    rows = conn.execute(
        """SELECT o.id FROM instrument i JOIN instrument o ON o.site_slug = i.site_slug WHERE i.id = %s
           UNION SELECT %s""", (subject_id, subject_id)).fetchall()
    return [r["id"] for r in rows]


_install_triggers()


@app.periodic(cron=CRON_UTC, periodic_id="recovery_weekly")
@app.task(name="models.recovery", queue="models", queueing_lock="models.recovery", lock="models.recovery")
def recovery_task(timestamp: int | None = None) -> dict[str, Any]:
    return run_keys(["*"], "schedule")


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as-of", type=date.fromisoformat, help="YYYY-MM-DD (default today, US Eastern)")
    p.add_argument("--instrument", action="append", dest="instruments", help="instrument id (repeatable)")
    p.add_argument("--no-mark", action="store_true", help="do not mark read models dirty")
    p.add_argument("--json", action="store_true", help="print every output as JSON")


@cli.action("model", MODEL, help="Recovery if the tenant or customer walks (collateral waterfall)", configure=_args)
def run_cli(args: argparse.Namespace) -> int:
    result = run(args.as_of, args.instruments, trigger="manual", mark=not args.no_mark)
    if args.json:
        print(json.dumps(result["outputs"], indent=2, ensure_ascii=False))
        return 0
    print(f"recovery {VERSION} as of {result['as_of']} (run {result['run_id']}): {len(result['outputs'])} instruments")
    for iid, o in result["outputs"].items():
        value = o["valueB"]["value"]
        print(f"  {iid:40} {o['method'] or '-':15} recovery {o['recovery']['value'] or 'n/d':8} "
              f"value {'n/d' if value is None else f'${value}B'}; missing {o['missing'][:2]}")
    return 0
