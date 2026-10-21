"""Model `gpu_collateral` (spec 19 steps 3-5; Instrument detail "Collateral" card): what the GPUs behind each
GPU-backed facility are worth today and as they age, its loan-to-value, and the recovery if the customer
walks.

    python -m marden model gpu_collateral [--as-of YYYY-MM-DD] [--instrument ID ...] [--json]

One `model_output` per facility (subject ('instrument', id)) = GpuCollateral: {instrumentId, model, units,
valueTodayB, balanceB, ltvToday, ltvByQuarter, recoveryIfCustomerWalks} plus missing and basis (unit value,
useful life, unit age, assumptions). Inputs and arithmetic: marden.models.gpu_facilities (facility facts)
and gpu_value (index, decline, implied utilization, value per unit). Values are M, 'model v0'
(uncalibrated); a missing input leaves the value null and names it in `missing`. The `deployment` piece
(delivered vs energized) is the gpu_deployment model's, joined by the read model.

Runs: daily 09:27 UTC, after gpu_index / gpu_model, and within a minute of a facility fact (GPU model,
collateral record, drawn amount, coupon, maturity) or a GPU market input. A run for today marks the
facility's read models (`@model:gpu_collateral`: instrument_detail, gpu_collateral, ...) and the recovery
model; a past --as-of is a backtest.
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
from marden.models.gpu_facilities import FACILITY_FIELDS, Facility, Valuation, pools, range_text, value_facility
from marden.models.gpu_facilities import load as load_facilities
from marden.models.gpu_inputs import ENTITY_FIELDS, SERIES_FIELDS, SERIES_PREFIXES, Market
from marden.models.gpu_inputs import load as load_market
from marden.models.gpu_value import HAIRCUT, Fit, fit_decline
from marden.models.sourced import Batch, fact_value, m_value, method_version, rounded, run_outputs

MODEL = "gpu_collateral"
VERSION = "0.1"
METHOD = method_version(MODEL, VERSION)
CRON_UTC = "27 9 * * *"
SOURCE = "Marden GPU collateral model"


def _m(value: Any, day: date) -> dict[str, Any]:
    return m_value(value, day, METHOD, source_name=SOURCE, uncalibrated=True)


def facility_output(f: Facility, v: Valuation, market: Market, fit: Fit | None) -> dict[str, Any]:
    day = market.as_of
    units_row = f.facts.get("gpu_collateral")
    return {
        "instrumentId": f.instrument_id,
        "model": f.model_text or (v.model or ""),
        "units": (_m(round(v.units), day) if v.units_scaled else fact_value(units_row, v.units, as_of=day, tag="M"))
        if v.units is not None else _m(None, day),
        "valueTodayB": _m(rounded(v.value / 1e9 if v.value is not None else None, 4), day),
        "balanceB": fact_value(v.balance_row, round(v.balance / 1e9, 4), as_of=day) if v.balance is not None
        else fact_value(None, as_of=day),
        "ltvToday": _m(rounded(v.ltv, 4), day),
        "ltvByQuarter": v.ltv_by_quarter,
        "recoveryIfCustomerWalks": _m(range_text(v.recovery_low, v.recovery_high), day),
        "missing": v.missing,
        "basis": {
            "methodVersion": f"{METHOD} (model v0)",
            "valuedModel": v.model,
            "valuePerUnitUsd": rounded(v.per_unit, 0),
            "unitAgeYears": rounded(v.unit_age, 2),
            "usefulLifeYears": v.life,
            "usefulLifeSource": None if v.life_row is None else {
                "entity": v.life_row["subject_id"], "tag": v.life_row["tag"],
                "sourceUrl": v.life_row.get("source_url")},
            "haircut": HAIRCUT,
            "utilization": None if fit is None or fit.realization is None else round(fit.realization, 4),
            "annualDecline": None if fit is None else round(fit.annual_decline, 4),
            "gpuLoanSpreadBp": market.gpu_loan_spread_bp(),
            "recovery": "collateral value / balance, capped at 100c, customer walking today to a year out; "
                        "no claims known ahead of the facility's first lien",
            "balanceBasis": None if v.balance_row is None else v.balance_row["field"],
            "unitsScaledToDrawn": v.units_scaled,
            "unitsInRecord": f.units,
            "pool": v.pool,
            "poolBalanceB": rounded(v.pool_balance / 1e9 if v.pool_balance else None, 4),
        },
    }


def run(as_of: date | None = None, instruments: list[str] | None = None, *, trigger: str = "manual",
        mark: bool = True) -> dict[str, Any]:
    def compute(conn: Conn, day: date) -> Batch:
        market = load_market(conn, day)
        fit = fit_decline(market, market.median_life())
        batch = Batch()
        origins: list[Any] = [market.origins]
        everything = load_facilities(conn, day)
        by_pool = pools(everything)
        for f in everything:
            if instruments is not None and f.instrument_id not in instruments:
                continue
            v = value_facility(f, market, fit, day, by_pool[f.pool_key])
            batch.add("instrument", f.instrument_id, facility_output(f, v, market, fit))
            origins.append([f.instrument_id, sorted(int(r["id"]) for r in f.facts.values())])
        batch.inputs = origins
        return batch

    return run_outputs(MODEL, VERSION, as_of, compute, trigger=trigger, mark=mark)


def run_keys(keys: list[str], trigger: str) -> dict[str, Any]:
    result = run(instruments=None if "*" in keys else keys, trigger=trigger)
    return {"facilities": len(result["outputs"]), "changed": len(result["changed"]), "run_id": result["run_id"]}


def _gpu_series(conn: Conn, subject_id: str) -> list[str]:
    return ["*"] if subject_id.startswith(SERIES_PREFIXES) else []


def _install_triggers() -> None:
    for fld in FACILITY_FIELDS:
        triggers.model_depends(MODEL, "instrument", fld)
    for fld in (*SERIES_FIELDS, "value"):
        triggers.model_depends(MODEL, "series", fld, _gpu_series)
    for fld in ENTITY_FIELDS:
        triggers.model_depends(MODEL, "entity", fld, triggers.everything)
    triggers.register_runner(MODEL, run_keys)


_install_triggers()


@app.periodic(cron=CRON_UTC, periodic_id="gpu_collateral_daily")
@app.task(name="models.gpu_collateral", queue="models", queueing_lock="models.gpu_collateral",
          lock="models.gpu_collateral")
def gpu_collateral_task(timestamp: int | None = None) -> dict[str, Any]:
    return run_keys(["*"], "schedule")


# ---------------------------------------------------------------- CLI
def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as-of", type=date.fromisoformat, help="YYYY-MM-DD (default today, US Eastern)")
    p.add_argument("--instrument", action="append", dest="instruments", help="instrument id (repeatable)")
    p.add_argument("--no-mark", action="store_true", help="do not mark read models dirty")
    p.add_argument("--json", action="store_true", help="print every output as JSON")


@cli.action("model", MODEL, help="GPU collateral value, LTV and recovery per GPU-backed facility", configure=_args)
def run_cli(args: argparse.Namespace) -> int:
    result = run(args.as_of, args.instruments, trigger="manual", mark=not args.no_mark)
    if args.json:
        print(json.dumps(result["outputs"], indent=2, ensure_ascii=False))
        return 0
    print(f"gpu_collateral {VERSION} as of {result['as_of']} (run {result['run_id']}): {len(result['outputs'])} "
          "facilities")
    for iid, o in result["outputs"].items():
        print(f"  {iid:16} {o['model']:10} units {o['units']['value'] or 'n/d':>8} "
              f"value ${o['valueTodayB']['value'] or 0:.2f}B "
              f"balance ${o['balanceB']['value'] or 0:.2f}B LTV {o['ltvToday']['value'] or 'n/d'} "
              f"recovery {o['recoveryIfCustomerWalks']['value'] or 'n/d'}; missing {o['missing']}")
    return 0
