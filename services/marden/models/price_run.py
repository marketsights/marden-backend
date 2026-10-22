"""Running the price engine (marden.models.price): writes, schedule, triggers and the CLI.

    cd services && uv run python -m marden model price [--as-of YYYY-MM-DD] [--instrument ID ...] [--no-mark]

Writes in one transaction under one model_run ('price', 'price v0.2'): model_output 'price' per instrument,
'cds' per issuer entity, 'curve' for series 'all' (exactly CurveTier[]) and 'curve_fit' (the tier fit, its
sample and what is missing) on full runs; one price_obs per priced instrument
and day (kind 'marden', tag M, method_version 'price v0.2', source 'Marden model price'; a rerun of the
same day replaces the model's own row). change1w is taken against the stored output exactly 7 days
earlier when there is one, else from this run's filtered path. After the writes the read models of
every subject are marked dirty ('@model:price', '@model:cds', '@model:curve').

Runs (spec 22 "What runs when"): daily 09:40 UTC (05:40 EDT, after the 09:35 cushion run whose
projection sets completion dates, before the 09:53 status run that reads the price), and within a
minute of any published input (marden.models.triggers): a price observation other than the model's
own, a Treasury / SOFR / HY (ICE BofA, or its etf_oas fund-OAS proxy) / equity series value, an issue
comp, an instrument's terms, a site's power, permit or schedule facts and events, a cushion run, a
recovery run (the site collateral waterfall the facilities price with, v0.2), an issuer's XBRL debt or shares.
"""

from __future__ import annotations

import argparse
import logging
import time
from contextlib import nullcontext
from datetime import date
from typing import Any

import psycopg

from marden import cli
from marden.core import db
from marden.core.db import Conn
from marden.core.queue import app
from marden.core.records import PriceWriter
from marden.models import pit, price_equity, price_signals, price_terms, runs, triggers
from marden.models.price import MODEL, PRICE_LABEL, VERSION, inputs_hash, is_target, run_day, week_change
from marden.readmodels.context import business_today
from marden.readmodels.deps import mark_dirty_for

log = logging.getLogger(__name__)

DAILY_CRON = "40 9 * * *"
WRITE_TRIES = 5
PRICE_SOURCE = PRICE_LABEL


def _write_price_obs(conn: Conn, iid: str, day: date, out: dict[str, Any]) -> None:
    conn.execute("DELETE FROM price_obs WHERE instrument_id = %s AND obs_date = %s AND kind = 'marden' "
                 "AND method_version = %s", (iid, day, VERSION))
    if out.get("price") is None:
        return
    PriceWriter(conn).add(iid, day, "marden", out["price"], tag="M", source_name=PRICE_SOURCE,
                          yield_pct=out.get("yieldPct"), spread_bp=out.get("spreadBp"), method_version=VERSION)


def _write(run_id: int, day: date, result: dict[str, Any], *, partial: bool, mark: bool) -> None:
    """Every write of one run, in one short transaction."""
    with db.transaction() as conn:
        reason = f"model_run:{run_id}"
        for entity, doc in result["cds"].items():
            runs.write_output(conn, run_id, "cds", VERSION, "entity", entity, day, doc)
            if mark:
                mark_dirty_for("entity", entity, "@model:cds", conn=conn, reason=reason)
        for iid, out in result["prices"].items():
            stored = week_change(conn, iid, day, out.get("price"))
            if stored is not None:
                out["change1w"] = stored
                out.setdefault("basis", {})["change1wBasis"] = "stored output 7 days earlier"
            elif out.get("change1w") is not None:
                out.setdefault("basis", {})["change1wBasis"] = "this run's filtered path 7 days earlier"
            runs.write_output(conn, run_id, MODEL, VERSION, "instrument", iid, day, out)
            _write_price_obs(conn, iid, day, out)
            if mark:
                mark_dirty_for("instrument", iid, "@model:price", conn=conn, reason=reason)
        if not partial:  # 'curve' is exactly CurveTier[]; the fit behind it is 'curve_fit'
            runs.write_output(conn, run_id, "curve", VERSION, "series", "all", day,
                              result["curve"])  # type: ignore[arg-type]
            runs.write_output(conn, run_id, "curve_fit", VERSION, "series", "all", day,
                              {**result["fit"], "missing": result["fit_missing"], "label": "Marden tier curves",
                               "methodVersion": VERSION})
            if mark:
                mark_dirty_for("series", "all", "@model:curve", conn=conn, reason=reason)


def run(as_of: date | None = None, ids: list[str] | None = None, *, trigger: str = "manual",
        mark: bool = True) -> dict[str, Any]:
    """Run the price engine as of `as_of` (default today, US Eastern) for every target instrument or `ids`.
    The computation reads in one transaction; the writes go in a second, short one, retried when it
    deadlocks with another writer marking the same read models (it is idempotent)."""
    day = as_of or business_today()
    if day > business_today():
        raise ValueError(f"cannot run the price model for a future day ({day.isoformat()})")
    run_id = runs.start_run(MODEL, VERSION, trigger=trigger)
    try:
        with db.transaction() as conn:
            result = run_day(conn, day, ids)
        for attempt in range(WRITE_TRIES):
            try:
                _write(run_id, day, result, partial=bool(ids), mark=mark)
                break
            except psycopg.errors.DeadlockDetected:
                if attempt == WRITE_TRIES - 1:
                    raise
                log.warning("price run %s: deadlock while writing, retrying", run_id)
                time.sleep(1.0 + attempt)
        runs.finish_run(run_id, ok=True, inputs_hash=inputs_hash(result))
    except Exception as exc:
        runs.finish_run(run_id, ok=False, error=f"{type(exc).__name__}: {exc}")
        raise
    result["run_id"] = run_id
    return result


# ---------------------------------------------------------------- triggers and schedule
def _observed_price(conn: Conn, instrument_id: str) -> list[str]:
    """A new price observation of an instrument the engine prices or reads re-runs the model, except the
    model's own 'marden' rows (no loop)."""
    row = conn.execute("SELECT kind FROM price_obs WHERE instrument_id = %s ORDER BY id DESC LIMIT 1",
                       (instrument_id,)).fetchone()
    if row is None or row["kind"] == "marden":
        return []
    return ["*"] if is_target(conn, instrument_id) else []


def _price_series(conn: Conn, subject_id: str) -> list[str]:
    prefixes = ("ust_par_", "sofr", "ice_bofa_", "etf_oas_", "equity:", "issue_comp:", "gpu_residual:")
    return ["*"] if subject_id.startswith(prefixes) else []


def _priced(conn: Conn, instrument_id: str) -> list[str]:
    return ["*"] if is_target(conn, instrument_id) else []


def _site_with_debt(conn: Conn, slug: str) -> list[str]:
    row = conn.execute("SELECT 1 FROM instrument WHERE site_slug = %s LIMIT 1", (slug,)).fetchone()
    return ["*"] if row else []


def run_keys(keys: list[str], trigger: str) -> dict[str, Any]:
    result = run(trigger=trigger)  # issuer curves and tiers are shared: every mark re-runs the whole engine
    return {"run_id": result["run_id"], "instruments": len(result["prices"]), "issuers": len(result["cds"])}


def _install_triggers() -> None:
    triggers.model_depends(MODEL, "instrument", "@price", _observed_price)
    for fld in price_terms.TERM_FIELDS:
        triggers.model_depends(MODEL, "instrument", fld, triggers.everything)
    for fld in ("value", "close", "deal_terms", "issue_spread_bp", "residual_pct_of_new"):
        triggers.model_depends(MODEL, "series", fld, _price_series)
    for fld in price_signals.SITE_FIELDS:
        triggers.model_depends(MODEL, "site", fld, _site_with_debt)
    triggers.model_depends(MODEL, "site", "@event", _site_with_debt)
    triggers.model_depends(MODEL, "site", "@model:cushion", _site_with_debt)
    triggers.model_depends(MODEL, "instrument", "@model:recovery", _priced)
    for fld in (*price_equity.DEBT_FIELDS, *price_equity.SHARE_FIELDS):
        triggers.model_depends(MODEL, "entity", fld, triggers.everything)
    triggers.register_runner(MODEL, run_keys)


_install_triggers()


@app.periodic(cron=DAILY_CRON, periodic_id="price_daily")
@app.task(name="models.price_daily", queue="models", queueing_lock="models.price_daily", lock="models.price")
def price_daily_task(timestamp: int | None = None) -> dict[str, Any]:
    result = run(trigger="schedule")
    return {"run_id": result["run_id"], "instruments": len(result["prices"]), "issuers": len(result["cds"])}


@app.task(name="models.price", queue="models", lock="models.price")
def price_task(as_of: str | None = None, ids: list[str] | None = None) -> dict[str, Any]:
    result = run(date.fromisoformat(as_of) if as_of else None, ids, trigger="task")
    return {"run_id": result["run_id"], "instruments": len(result["prices"]), "issuers": len(result["cds"])}


# ---------------------------------------------------------------- CLI
def _configure(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as-of", type=date.fromisoformat, default=None, help="run date (default: today, US Eastern)")
    p.add_argument("--instrument", action="append", dest="ids", help="instrument id (repeatable; default: all)")
    p.add_argument("--no-mark", action="store_true", help="do not mark read models dirty")
    p.add_argument("--all-bonds", action="store_true", help="list every priced bond (default: V1 and CDS only)")
    p.add_argument("--audit-inputs", action="store_true",
                   help="list the inputs read by public date; exit 1 if one was not yet public on the as-of day")


def _fmt(v: Any, nd: int = 1) -> str:
    return "n/d" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))


def summary(result: dict[str, Any], v1: set[str], all_bonds: bool = False) -> list[str]:
    lines = [f"{'issuer':24} {'CDS bp':>8} {'upfront':>8} {'band':>13} {'raw date':10} {'raw bp':>7}  missing"]
    for e, c in sorted(result["cds"].items()):
        band = c["band"] or {}
        raw = c["raw"] or {}
        lines.append(f"{e:24} {_fmt(c['spreadBp']):>8} {_fmt(c['upfrontPct'], 2):>8} "
                     f"{_fmt(band.get('low'), 0) + '-' + _fmt(band.get('high'), 0):>13} {raw.get('date') or 'n/d':10} "
                     f"{_fmt(raw.get('spreadBp')):>7}  {'; '.join(c['missing'])[:80]}")
    lines.append("")
    lines.append(f"{'instrument':36} {'price':>7} {'band':>13} {'sprd':>6} {'yld':>6} {'1w':>6} {'mode':12} missing")
    for iid, o in sorted(result["prices"].items()):
        if iid not in v1 and not all_bonds:
            continue
        band = o["band"] or {}
        mode = (o.get("basis") or {}).get("mode") or "-"
        rng = _fmt(band.get("low"), 1) + "-" + _fmt(band.get("high"), 1)
        lines.append(f"{iid:36} {_fmt(o['price'], 2):>7} {rng:>13} "
                     f"{_fmt(o['spreadBp'], 0):>6} {_fmt(o['yieldPct'], 2):>6} {_fmt(o['change1w'], 2):>6} {mode:12} "
                     f"{len(o['missing'])}")
    return lines


@cli.action("model", MODEL, help="Run the price engine (bond-implied CDS, tier curves, facility prices)",
            configure=_configure)
def price_command(args: argparse.Namespace) -> int:
    with pit.audit() if args.audit_inputs else nullcontext([]) as used:
        result = run(args.as_of, args.ids, trigger="manual", mark=not args.no_mark)
    with db.transaction() as conn:
        v1 = {r["id"] for r in conn.execute("SELECT id FROM instrument WHERE in_v1").fetchall()}
    print(f"price run {result['run_id']} as of {result['as_of']}: {len(result['prices'])} instruments, "
          f"{len(result['cds'])} issuers, {len(result['curve'])} tiers")
    for line in summary(result, v1 | set(args.ids or []), args.all_bonds):
        print(line)
    return _audit_exit(args, used, result["as_of"])


def _audit_exit(args: argparse.Namespace, used: list[pit.Input], as_of: date) -> int:
    if not args.audit_inputs:
        return 0
    lines, late = pit.audit_report(used, as_of)
    print("\n".join(lines))
    return 1 if late else 0
