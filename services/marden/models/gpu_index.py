"""Models `gpu_index` (subject ('series', 'all')) and `gpu_model` (subject ('gpu_model', <model>)): the Marden
GPU price index and the per-model page (spec 19 "Screens": /gpus and /gpus/:model).

    python -m marden model gpu_index [--as-of YYYY-MM-DD]
    python -m marden model gpu_model [--as-of YYYY-MM-DD] [--gpu H100 ...]

gpu_index output: {index: GpuPricePoint[] (every capture day up to the as-of day), providers:
GpuProviderPrice[] (each provider and model's newest price), models: [model keys with a page], decline,
missing, basis}. gpu_model output = GpuModelDetail: launch, newPrice (midpoint of the published range),
index and providers for the model, resale observations, valueByAgeYears (gpu_value: a unit bought new at
launch, each whole year of age), assumptions {utilization, annualDecline, haircut, usefulLifeYears,
methodVersion}, facilities secured by the model; plus missing and basis (resale and residual-band cross
checks). Values are 'model v0' (uncalibrated) until back-tested against 50+ resale prices per model.

Runs: daily 09:23 UTC (after the overnight collectors; spec 22 lists GPU prices weekly, spec 19 daily
pages), and within a minute of a new GPU price, chip, resale, residual or useful-life fact, a GPU-loan issue
spread or a Treasury par yield. A run for today marks the 'gpus' and 'gpu_model' views (rebuilt by this
task: views.SCHEDULED_VIEWS) and, through '@model:gpu_model', the gpu_collateral model of facilities on it.
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
from marden.models.gpu_facilities import load as load_facilities
from marden.models.gpu_inputs import ENTITY_FIELDS, SERIES_FIELDS, SERIES_PREFIXES, Market
from marden.models.gpu_inputs import load as load_market
from marden.models.gpu_value import HAIRCUT, REGION, TERM, UTILIZATION, Fit, fit_decline, index_points, resale_check
from marden.models.gpu_value import value_by_age
from marden.models.sourced import Batch, fact_value, m_value, method_version, num, run_outputs

INDEX, MODEL = "gpu_index", "gpu_model"
VERSION = "0.1"
INDEX_METHOD, MODEL_METHOD = method_version(INDEX, VERSION), method_version(MODEL, VERSION)
CRON_UTC = "23 9 * * *"
SOURCE = "Marden GPU model"


def providers_doc(market: Market, model: str | None = None) -> list[dict[str, Any]]:
    return [{"provider": p.provider, "model": p.model, "term": TERM, "region": REGION,
             "usdHr": fact_value(p.row, round(p.usd_hr, 4)), "capturedAt": p.captured.isoformat(),
             "pageUrl": p.row.get("source_url") or ""}
            for p in market.latest_prices() if model is None or p.model == model]


def decline_doc(fit: Fit | None) -> dict[str, Any] | None:
    if fit is None:
        return None
    return {"annualDecline": round(fit.annual_decline, 4), "slopePerYear": round(fit.slope, 5), "r2": round(fit.r2, 3),
            "models": fit.models, "captured": fit.captured.isoformat(),
            "basis": "ln(median on-demand price) vs years since first volume shipment, across models"}


def index_output(market: Market, fit: Fit | None) -> dict[str, Any]:
    points = index_points(market.prices)
    missing = [] if points else ["GPU rental prices captured on or before the day"]
    if points and fit is None:
        missing.append("prices for at least three models with a launch date (decline fit)")
    models = sorted({p["model"] for p in points} | {k for k, c in market.chips.items() if c.launch})
    return {"index": points, "providers": providers_doc(market), "models": models, "decline": decline_doc(fit),
            "missing": missing,
            "basis": {"methodVersion": f"{INDEX_METHOD} (model v0)", "term": TERM, "region": REGION,
                      "note": "median, 25th and 75th percentile of the providers' published on-demand list prices "
                              "per capture day; one price per provider and day"}}


def _facilities(market: Market, facilities: list[Any], model: str) -> list[dict[str, Any]]:
    out = []
    for f in facilities:
        if model not in f.models:
            continue
        row = f.facts.get("gpu_collateral")
        units = fact_value(row, f.units, as_of=market.as_of, tag="M") if f.units is not None else \
            m_value(None, market.as_of, MODEL_METHOD)
        out.append({"instrumentId": f.instrument_id, "name": f.name, "units": units})
    return out


def model_output(market: Market, fit: Fit | None, model: str, facilities: list[Any]) -> dict[str, Any]:
    day, chip = market.as_of, market.chips.get(model)
    rows = chip.rows if chip else {}
    life = market.median_life()
    missing: list[str] = []
    if chip is None or chip.launch is None:
        missing.append("first volume shipment (launch)")
    if fit is None:
        missing.append("GPU rental index with at least three models (decline fit)")
    if life is None:
        missing.append("useful life (filed depreciation policies)")
    curve = value_by_age(market, fit, model, day, life) if fit and life else []
    if fit and life and not curve:
        missing.append(f"{model} index price, launch date or discount rate on or before the day")
    method = f"{MODEL_METHOD} (model v0)"
    new_price = chip.new_price if chip else None
    resid = market.residual.get(model)
    return {
        "model": model,
        "launch": fact_value(rows.get("first_volume_ship"), as_of=day, tag="R"),
        "newPrice": fact_value(rows.get("new_price_usd"), None if new_price is None else round(new_price), as_of=day,
                               tag="R") if rows.get("new_price_usd") else fact_value(None, as_of=day, tag="R"),
        "index": [p for p in index_points(market.prices) if p["model"] == model],
        "providers": providers_doc(market, model),
        "resale": [{"date": r["valid_from"].isoformat(), "usd": float(r["value"]), "source": r["source_name"],
                    "url": r.get("source_url") or ""} for r in market.resale.get(model, [])],
        "valueByAgeYears": [{"age": age, "usd": m_value(round(v), day, MODEL_METHOD, source_name=SOURCE,
                                                        uncalibrated=True)} for age, v in curve],
        "assumptions": {
            "utilization": UTILIZATION,
            "annualDecline": round(fit.annual_decline, 4) if fit else None,
            "haircut": HAIRCUT,
            "usefulLifeYears": m_value(life, day, MODEL_METHOD, source_name=f"median of {len(market.lives)} companies' "
                                       "GPU depreciation policies"),
            "methodVersion": method,
        },
        "facilities": _facilities(market, facilities, model),
        "missing": missing,
        "basis": {
            "decline": decline_doc(fit),
            "discount": {"gpuLoanSpreadBp": market.gpu_loan_spread_bp(), "spreadDeals": len(market.gpu_loan_spreads),
                         "ust": {str(k): v[0] for k, v in sorted(market.ust.items())}},
            "resaleCheck": resale_check(market, fit, model, day, life) if fit and life else None,
            "residualBands": None if resid is None else {"pctOfNew": resid["value"], "tag": resid["tag"],
                                                         "sourceName": resid["source_name"]},
            "newPriceUsd": None if new_price is None else round(new_price),
            "valueToNew": [{"age": age, "share": round(v / new_price, 4)} for age, v in curve] if new_price else [],
        },
    }


def _models(market: Market) -> list[str]:
    priced = {p.model for p in market.prices}
    return sorted(priced | {k for k, c in market.chips.items() if c.launch or c.rows.get("new_price_usd")})


def run_index(as_of: date | None = None, *, trigger: str = "manual", mark: bool = True) -> dict[str, Any]:
    def compute(conn: Conn, day: date) -> Batch:
        market = load_market(conn, day)
        batch = Batch()
        batch.add("series", "all", index_output(market, fit_decline(market)))
        batch.views.add(("gpus", "all"))
        batch.inputs = market.origins
        return batch

    return run_outputs(INDEX, VERSION, as_of, compute, trigger=trigger, mark=mark)


def run_models(as_of: date | None = None, models: list[str] | None = None, *, trigger: str = "manual",
               mark: bool = True) -> dict[str, Any]:
    def compute(conn: Conn, day: date) -> Batch:
        market = load_market(conn, day)
        fit = fit_decline(market)
        facilities = load_facilities(conn, day)
        batch = Batch()
        for model in _models(market):
            if models is None or model in models:
                batch.add(MODEL, model, model_output(market, fit, model, facilities))
                batch.views.add(("gpu_model", model))
        batch.inputs = market.origins
        return batch

    return run_outputs(MODEL, VERSION, as_of, compute, trigger=trigger, mark=mark)


def run_both(keys: list[str], trigger: str) -> dict[str, Any]:
    a, b = run_index(trigger=trigger), run_models(trigger=trigger)
    return {"index_run": a["run_id"], "model_run": b["run_id"], "models": len(b["outputs"])}


def _gpu_series(conn: Conn, subject_id: str) -> list[str]:
    return ["*"] if subject_id.startswith(SERIES_PREFIXES) else []


def _install_triggers() -> None:
    for model in (INDEX, MODEL):
        for fld in (*SERIES_FIELDS, "value"):
            triggers.model_depends(model, "series", fld, _gpu_series)
        for fld in ENTITY_FIELDS:
            triggers.model_depends(model, "entity", fld, triggers.everything)
    triggers.model_depends(MODEL, "instrument", "gpu_", triggers.everything)  # facilities listed per model
    triggers.register_runner(INDEX, lambda keys, trigger: {"run_id": run_index(trigger=trigger)["run_id"]})
    triggers.register_runner(MODEL, lambda keys, trigger: {"run_id": run_models(trigger=trigger)["run_id"]})


_install_triggers()


@app.periodic(cron=CRON_UTC, periodic_id="gpu_index_daily")
@app.task(name="models.gpu_index", queue="models", queueing_lock="models.gpu_index", lock="models.gpu_index")
def gpu_index_task(timestamp: int | None = None) -> dict[str, Any]:
    return run_both(["*"], "schedule")


# ---------------------------------------------------------------- CLI
def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as-of", type=date.fromisoformat, help="YYYY-MM-DD (default today, US Eastern)")
    p.add_argument("--no-mark", action="store_true", help="do not mark read models dirty")
    p.add_argument("--json", action="store_true", help="print every output as JSON")


def _model_args(p: argparse.ArgumentParser) -> None:
    _args(p)
    p.add_argument("--gpu", action="append", dest="gpus", help="GPU model key, e.g. H100 (repeatable)")


@cli.action("model", INDEX, help="Marden GPU price index (median of public list prices per model)", configure=_args)
def index_cli(args: argparse.Namespace) -> int:
    result = run_index(args.as_of, trigger="manual", mark=not args.no_mark)
    out = result["outputs"]["all"]
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    print(f"gpu_index {VERSION} as of {result['as_of']} (run {result['run_id']}): {len(out['index'])} points, "
          f"decline {out['decline']['annualDecline'] if out['decline'] else 'n/d'}; missing {out['missing']}")
    for p in out["index"]:
        print(f"  {p['date']} {p['model']:7} median {p['medianUsdHr']:6.2f} p25 {p['p25']:6.2f} p75 {p['p75']:6.2f} "
              f"n={p['nProviders']}")
    return 0


@cli.action("model", MODEL, help="GPU value by age, resale and facilities per GPU model", configure=_model_args)
def model_cli(args: argparse.Namespace) -> int:
    result = run_models(args.as_of, args.gpus, trigger="manual", mark=not args.no_mark)
    if args.json:
        print(json.dumps(result["outputs"], indent=2, ensure_ascii=False))
        return 0
    print(f"gpu_model {VERSION} as of {result['as_of']} (run {result['run_id']}): {len(result['outputs'])} models")
    for key, out in result["outputs"].items():
        curve = " ".join(f"{p['age']}y:{num(p['usd']['value']) or 0:,.0f}" for p in out["valueByAgeYears"])
        print(f"  {key:6} launch {out['launch']['value'] or 'n/d':8} facilities {len(out['facilities'])} "
              f"value by age {curve or 'n/d'}; missing {out['missing']}")
    return 0
