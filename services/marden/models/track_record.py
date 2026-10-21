"""Model `track_record`: the backtest of past repricings (spec 12; spec 00a "Proof before selling").

    cd services && uv run python -m marden model track_record [--as-of YYYY-MM-DD] [--case KEY ...] [--workers N]

For each locked case (track_record_cases.CASES) the market side gives the day its price moved (price
below 95 or spread +200bp, public by the as-of day) and the signal side (track_record_scan) the first day
Marden's status, price or cushion signal fired, from snapshots where a live run existed that day and
point-in-time reconstructions otherwise. One output: model 'track_record', subject ('series', 'all'),
version VERSION =

    {asOf, methodVersion, window, total, published, cases: [{name, instrumentId, instrumentIds, flagged,
     moved, leadDays, backtestRunning, outcome, driver, signals, evidence, flagBasis, sites, missing}],
     controls, summary, earlierCalls (RiskList.earlierCalls), rules (the fixed status rules), models,
     assumptions, missing, scan}

`cases` are the past repricings (and the spec-named ones whose market move is not on file, still
running); `controls` the instruments that did not move (a flag on one is a false alarm). The read
model `track_record` drops `cases` into TrackRecord; `risk_list` takes `earlierCalls`.

Runs: weekly, Sunday 21:17 UTC (before the Sunday-night risk ranking, spec 22) as a full scan; and
within a minute of a new market price or a live status / price / cushion run on a case subject
(`models.run_dirty`), as a refresh: the market side again plus today's snapshots on the stored scan.
After a run for today the `track_record` view is marked dirty.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
from datetime import date
from typing import Any

from marden import cli
from marden.core import db
from marden.core.db import Conn
from marden.core.jsonutil import canonical_json, hash_parts
from marden.core.queue import app
from marden.models import runs, triggers
from marden.models import track_record_cases as tc
from marden.models import track_record_scan as ts
from marden.readmodels.context import business_today
from marden.readmodels.deps import mark_dirty

log = logging.getLogger(__name__)

MODEL, VERSION = "track_record", "0.1"
METHOD_VERSION = f"{MODEL} v{VERSION}"
METHOD_URL = "docs/methods/track-record-v0.1.md"
WEEKLY_CRON = "17 21 * * 0"
DEFAULT_WORKERS = 3
VIEW = ("track_record", "all")
FLAG_BASIS = ("Flags come from the models' stored outputs where a live run was made that day (a snapshot) and "
              "otherwise from point-in-time reconstructions: the model code run on the day with only what was "
              "public then. Marden's live runs began 2026-10-21, so every earlier flag is a reconstruction.")


def model_rows() -> list[dict[str, Any]]:
    from marden.models import cushion_output, price, status_rules

    return [
        {"name": "status", "version": status_rules.METHOD_VERSION,
         "calibration": "fixed rules (00-global status rules), not fitted"},
        {"name": "price", "version": price.VERSION,
         "calibration": "bond-implied CDS graded against vendor composites (pricing-v0.1: CoreWeave 12% average "
                        "error, Oracle 21%); price bands 'model v0', uncalibrated"},
        {"name": "cushion", "version": cushion_output.VERSION,
         "calibration": "P10-P90 ranges and odds 'model v0', uncalibrated"},
        {"name": MODEL, "version": METHOD_VERSION,
         "calibration": "assumptions locked 2026-10-21 before the first run; no fitted parameter"},
    ]


def status_rules_table() -> list[dict[str, Any]]:
    from marden.readmodels.views.track_record import status_rules  # the table the screen shows

    return status_rules()


# ---------------------------------------------------------------- plan and assembly
def plan(conn: Conn, keys: list[str] | None) -> dict[str, Any]:
    cases = [c for c in tc.CASES if keys is None or c.key in keys]
    insts = tc.case_instruments(conn, cases)
    sites = {c.key: tc.case_sites(conn, c, insts) for c in cases}
    slugs = sorted({s for v in sites.values() for s in v})
    on_sites = conn.execute("SELECT id FROM instrument WHERE site_slug = ANY(%s)", (slugs,)).fetchall()
    price_ids = sorted(set(insts) | {r["id"] for r in on_sites})
    return {"cases": cases, "insts": insts, "sites": sites, "slugs": slugs, "price_ids": price_ids}


def _kinds(p: dict[str, Any]) -> dict[str, str]:
    return {i: v["kind"] for i, v in p["insts"].items()}


def assess(case: tc.Case, market: tc.Market, sites: list[str], states: dict[date, ts.State]) -> dict[str, Any]:
    """One case's document from its market side and the scan states."""
    ids = list(case.instruments)
    first = ts.first_fires(ids, sites, states)
    flag = min(first.values(), key=lambda f: (f.day, f.signal), default=None)
    move = market.move
    lead = (move.day - flag.day).days if move is not None and flag is not None else None
    if move is None:
        outcome = "pending" if case.named else "control"
    elif flag is None:
        outcome = "missed"
    else:
        outcome = "early" if lead > 0 else ("same day" if lead == 0 else "late")
    evidence: list[dict[str, Any]] = []
    if move is not None:
        evidence.append({"date": move.day.isoformat(), "kind": "market",
                         "text": f"{move.instrument_id}: {move.kind} {move.price} ({move.source}); rule {move.rule}, "
                                 f"baseline {move.baseline_price} ({move.baseline_basis})", **move.doc()})
    evidence += [{"kind": f.signal, "text": f.detail, **f.doc()} for f in sorted(first.values(), key=lambda f: f.day)]
    days = sorted(states)
    base = states[days[0]] if days else None
    missing = list(market.missing)
    if not sites:
        missing.append(f"{case.name}: no site linked (no chain status or cushion signal)")
    return {
        "key": case.key, "name": case.name,
        "instrumentId": case.instruments[0] if case.instruments else None,
        "instrumentIds": list(case.instruments),
        "flagged": flag.day.isoformat() if flag else None,
        "moved": move.day.isoformat() if move else None,
        "leadDays": lead,
        "backtestRunning": move is None,
        "outcome": outcome,
        "driver": move.driver if move else None,
        "signals": {k: f.doc() for k, f in sorted(first.items())},
        "evidence": evidence,
        "flagBasis": flag.basis if flag else None,
        "sites": sites,
        "baseline": {"marketDay": tc.WINDOW_START.isoformat(), "market": market.baselines,
                     "signals": _baseline(base, ids, sites)},
        "missing": missing,
        "pending": (case.note or "no market move on file in the window") if move is None and case.named else None,
    }


def _baseline(base: ts.State | None, ids: list[str], sites: list[str]) -> dict[str, Any] | None:
    if base is None:
        return None
    return {"day": base["day"], "price": {i: base["price"].get(i) for i in ids},
            "status": {s: (base["status"].get(s) or {}).get("status") for s in sites},
            "cushion": {s: base["cushion"].get(s) for s in sites}}


def summary(cases: list[dict[str, Any]], controls: list[dict[str, Any]]) -> dict[str, Any]:
    done = [c for c in cases if not c["backtestRunning"]]
    leads = [c["leadDays"] for c in done if c["leadDays"] is not None]
    credit = [c for c in done if c["driver"] == "credit"]
    credit_leads = [c["leadDays"] for c in credit if c["leadDays"] is not None]
    return {
        "repricings": len(done), "pending": len(cases) - len(done),
        "flaggedFirst": sum(c["outcome"] == "early" for c in done),
        "sameDay": sum(c["outcome"] == "same day" for c in done),
        "late": sum(c["outcome"] == "late" for c in done), "missed": sum(c["outcome"] == "missed" for c in done),
        "medianLeadDays": statistics.median(leads) if leads else None,
        "creditDriven": {"repricings": len(credit), "flaggedFirst": sum(c["outcome"] == "early" for c in credit),
                         "medianLeadDays": statistics.median(credit_leads) if credit_leads else None},
        "controls": len(controls), "falseAlarms": sum(c["flagged"] is not None for c in controls),
    }


def assemble(day: date, p: dict[str, Any], markets: dict[str, tc.Market], states: dict[date, ts.State],
             scan_meta: dict[str, Any]) -> dict[str, Any]:
    docs = [assess(c, markets[c.key], p["sites"][c.key], states) for c in p["cases"]]
    cases = [d for d in docs if d["outcome"] != "control"]
    controls = [{k: d[k] for k in ("key", "name", "instrumentIds", "flagged", "signals", "sites", "missing")}
                for d in docs if d["outcome"] == "control"]
    calls = [{"name": c["name"], "flagged": c["flagged"], "moved": c["moved"], "leadDays": c["leadDays"]}
             for c in cases if c["outcome"] == "early"]
    missing = sorted({m for s in states.values() for m in s["missing"]})
    return {
        "asOf": day.isoformat(), "methodVersion": METHOD_VERSION, "methodUrl": METHOD_URL,
        "window": {"start": tc.WINDOW_START.isoformat(), "end": day.isoformat()},
        "total": len(cases), "published": sum(not c["backtestRunning"] for c in cases),
        "cases": sorted(cases, key=lambda c: (c["backtestRunning"], c["moved"] or "", c["name"])),
        "controls": sorted(controls, key=lambda c: c["name"]),
        "summary": summary(cases, controls),
        "earlierCalls": sorted(calls, key=lambda c: c["flagged"], reverse=True),
        "rules": status_rules_table(), "models": model_rows(),
        "assumptions": list(tc.ASSUMPTIONS), "flagBasis": FLAG_BASIS,
        "missing": missing,
        "scan": {**scan_meta, "states": [states[d] for d in sorted(states)]},
    }


def markets_of(conn: Conn, p: dict[str, Any], day: date) -> dict[str, tc.Market]:
    series = tc.market_series(conn, list(p["insts"]), day)
    return {c.key: tc.case_market(c, _kinds(p), series) for c in p["cases"]}


# ---------------------------------------------------------------- runs
def scan(p: dict[str, Any], day: date, workers: int, step: int) -> tuple[dict[date, ts.State], dict[str, Any]]:
    """Weekly states from the window start, then daily ones in the week before each case's first signal."""
    weekly = ts.scan_days(day, step)
    args = (p["price_ids"], p["slugs"], p["slugs"])
    states = ts.compute_days(weekly, *args, workers)
    extra: set[date] = set()
    for c in p["cases"]:
        first = ts.first_fires(list(c.instruments), p["sites"][c.key], states)
        earliest = min(first.values(), key=lambda f: f.day, default=None)
        extra |= {d for d in ts.refine_days(states, earliest) if d not in states}
    if extra:
        states.update(ts.compute_days(sorted(extra), *args, workers))
    return states, {"weekly": [d.isoformat() for d in weekly], "refined": sorted(d.isoformat() for d in extra),
                    "stepDays": step}


def _write(run_id: int, day: date, output: dict[str, Any], *, mark: bool) -> None:
    with db.transaction() as conn:
        runs.write_output(conn, run_id, MODEL, VERSION, "series", "all", day, output)
        if mark and day == business_today():
            mark_dirty(*VIEW, conn=conn, reason=f"model:{MODEL}@{run_id}")


def _digest(output: dict[str, Any]) -> str:
    return hash_parts(MODEL, VERSION, canonical_json([[c["key"], c["flagged"], c["moved"]] for c in output["cases"]]))


def run(as_of: date | None = None, cases: list[str] | None = None, *, trigger: str = "manual",
        workers: int = DEFAULT_WORKERS, step: int = 7, mark: bool = True) -> dict[str, Any]:
    """The full backtest as of `as_of` (default today, US Eastern): market moves and a fresh signal scan."""
    day = as_of or business_today()
    if day > business_today():
        raise ValueError(f"cannot run the backtest for a future day ({day.isoformat()})")
    if day <= tc.WINDOW_START:
        raise ValueError(f"the backtest window starts {tc.WINDOW_START.isoformat()}")
    run_id = runs.start_run(MODEL, VERSION, trigger=trigger)
    try:
        with db.transaction() as conn:
            p = plan(conn, cases)
            markets = markets_of(conn, p, day)
        states, meta = scan(p, day, workers, step)
        output = assemble(day, p, markets, states, {**meta, "mode": "full", "fullRunId": run_id})
        _write(run_id, day, output, mark=mark and cases is None)
        runs.finish_run(run_id, ok=True, inputs_hash=_digest(output))
    except Exception as exc:
        runs.finish_run(run_id, ok=False, error=f"{type(exc).__name__}: {exc}")
        raise
    return {"run_id": run_id, "as_of": day.isoformat(), "output": output}


def refresh(*, trigger: str = "dirty") -> dict[str, Any]:
    """Re-derive the market side and add today's live snapshots to the latest full scan (cheap)."""
    day = business_today()
    with db.transaction() as conn:
        prev = conn.execute(
            """SELECT output FROM model_output WHERE model = %s AND subject_id = 'all' AND as_of <= %s
                 AND output->'scan'->>'mode' = 'full' ORDER BY as_of DESC, created_at DESC LIMIT 1""",
            (MODEL, day),
        ).fetchone()
    if prev is None:
        return {"skipped": "no full backtest yet (the weekly run makes one)"}
    run_id = runs.start_run(MODEL, VERSION, trigger=trigger)
    try:
        with db.transaction() as conn:
            p = plan(conn, None)
            markets = markets_of(conn, p, day)
        old = prev["output"]["scan"]
        states = {date.fromisoformat(s["day"]): s for s in old["states"] if date.fromisoformat(s["day"]) < day}
        states[day] = ts.compute_day((day.isoformat(), p["price_ids"], p["slugs"], p["slugs"]))  # live runs: snapshots
        meta = {k: v for k, v in old.items() if k != "states"}
        output = assemble(day, p, markets, states, {**meta, "mode": "full", "refreshedBy": run_id})
        _write(run_id, day, output, mark=True)
        runs.finish_run(run_id, ok=True, inputs_hash=_digest(output))
    except Exception as exc:
        runs.finish_run(run_id, ok=False, error=f"{type(exc).__name__}: {exc}")
        raise
    return {"run_id": run_id, "cases": output["total"], "published": output["published"]}


# ---------------------------------------------------------------- triggers and schedule
def run_keys(keys: list[str], trigger: str) -> dict[str, Any]:
    return refresh(trigger=trigger)


def _case_instrument(conn: Conn, instrument_id: str) -> list[str]:
    return ["all"] if any(instrument_id in c.instruments for c in tc.CASES) else []


def _case_site(conn: Conn, slug: str) -> list[str]:
    insts = tc.case_instruments(conn, tc.CASES)
    return ["all"] if any(slug in tc.case_sites(conn, c, insts) for c in tc.CASES) else []


def _install_triggers() -> None:
    for fld in ("@price", "@model:price"):
        triggers.model_depends(MODEL, "instrument", fld, _case_instrument)
    for fld in ("@model:status", "@model:cushion"):
        triggers.model_depends(MODEL, "site", fld, _case_site)
    triggers.register_runner(MODEL, run_keys)


_install_triggers()


@app.periodic(cron=WEEKLY_CRON, periodic_id="track_record_weekly")
@app.task(name="models.track_record", queue="models", queueing_lock="models.track_record",
          lock="models.track_record")
def track_record_task(timestamp: int | None = None) -> dict[str, Any]:
    result = run(trigger="schedule")
    out = result["output"]
    log.info("models.track_record %s: %s", result["as_of"], out["summary"])
    return {"run_id": result["run_id"], "cases": out["total"], "published": out["published"]}


# ---------------------------------------------------------------- CLI
def _configure(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as-of", type=date.fromisoformat, default=None, help="backtest window end (default: today)")
    p.add_argument("--case", action="append", dest="cases", help="case key (repeatable; default: every case)")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="parallel scan processes")
    p.add_argument("--step", type=int, default=7, help="days between scan days before refinement")
    p.add_argument("--no-mark", action="store_true", help="do not mark the track_record view dirty")
    p.add_argument("--json", action="store_true", help="print the output (without the scan) as JSON")


@cli.action("model", MODEL, help="Backtest past repricings: first Marden signal vs market move", configure=_configure)
def track_record_command(args: argparse.Namespace) -> int:
    result = run(args.as_of, args.cases, trigger="manual", workers=args.workers, step=args.step,
                 mark=not args.no_mark)
    out = result["output"]
    if args.json:
        print(json.dumps({k: v for k, v in out.items() if k != "scan"}, indent=2, ensure_ascii=False))
        return 0
    print(f"track_record run {result['run_id']} as of {result['as_of']}: {out['published']} of {out['total']} "
          f"published; {out['summary']}")
    print(f"{'case':48} {'flagged':10} {'moved':10} {'lead':>5} {'outcome':9} {'driver':6} first signal")
    for c in out["cases"]:
        signals = [e["text"] for e in c["evidence"] if e["kind"] != "market"]
        first = (signals[0] if signals else c["pending"] or "no signal")[:70]
        print(f"{c['name'][:48]:48} {c['flagged'] or '—':10} {c['moved'] or '—':10} "
              f"{'—' if c['leadDays'] is None else c['leadDays']:>5} {c['outcome']:9} {c['driver'] or '—':6} {first}")
    for c in out["controls"]:
        print(f"  control {c['name'][:46]:46} flagged {c['flagged'] or '—'}")
    return 0
