"""Running the stress model (marden.models.stress): stored runs, saved org scenarios, custom requests, the
schedule, triggers and the CLI.

    python -m marden model stress [--as-of YYYY-MM-DD] [--scenario NAME --severity base|severe --param k=v ...]

- `run(as_of)`: one model_run writing every saved scenario's outputs (marden.models.stress.compute); on a run
  for today it then fills `stress_saved.result` / `ran_at` for every org-saved request (identical requests
  computed once) with the day's cached book, and computes every parameter set the screen offers
  (marden.models.stress_grid), so a slider or dropdown change is served from a stored output at once;
- `models.stress_custom(request, key)`: the job the SQL function stress_run(jsonb) queues for a custom
  request; writes subject ('scenario', key) as of today (no read model is marked: nothing serves it but
  stress_run itself), reusing the worker's cached book (stress_price.cached_book) so a request after the
  first runs in seconds;
- schedule: nightly 06:17 UTC; triggers: a new price or cushion output, an obligation edge's amounts.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from typing import Any

from marden import cli
from marden.core import db
from marden.core.db import Conn
from marden.core.jsonutil import jsonb
from marden.core.queue import app
from marden.models import runs, stress_grid, triggers
from marden.models.sourced import Batch, run_outputs
from marden.models.stress import CRON_UTC, MODEL, SEVERITIES, VERSION, compute, run_request
from marden.models.stress_price import Book, cached_book
from marden.readmodels.context import business_today


def fill_saved(conn: Conn, book: Book) -> int:
    """Fill `stress_saved.result` for every saved request (identical requests computed once)."""
    done: dict[str, dict[str, Any]] = {}
    rows = conn.execute("SELECT id, request FROM stress_saved ORDER BY created_at").fetchall()
    for r in rows:
        key = json.dumps(r["request"], sort_keys=True)
        if key not in done:
            try:
                done[key] = run_request(conn, book, r["request"])
            except (KeyError, ValueError) as exc:
                done[key] = {"error": f"{type(exc).__name__}: {exc}"}
        conn.execute("UPDATE stress_saved SET result = %s, ran_at = now() WHERE id = %s", (jsonb(done[key]), r["id"]))
    return len(rows)


def run(as_of: date | None = None, *, trigger: str = "manual", mark: bool = True) -> dict[str, Any]:
    result = run_outputs(MODEL, VERSION, as_of, compute, trigger=trigger, mark=mark)
    if (as_of or business_today()) == business_today():
        with db.transaction() as conn:
            result["saved"] = fill_saved(conn, cached_book(conn, business_today()))
        result["grid"] = stress_grid.run(trigger=f"{trigger}:grid")
    return result


def run_custom(request: dict[str, Any], key: str) -> dict[str, Any]:
    """One custom request (the job `stress_run` queues), as of today, stored under subject `key`."""
    day = business_today()

    def compute_one(conn: Conn, d: date) -> Batch:
        batch = Batch()
        batch.add("scenario", key, run_request(conn, cached_book(conn, d), request))
        return batch

    out = run_outputs(MODEL, VERSION, day, compute_one, trigger="custom", mark=False)
    return {"key": key, "run_id": out["run_id"]}


def _run_keys(keys: list[str], trigger: str) -> dict[str, Any]:
    r = run(trigger=trigger)
    return {"outputs": len(r["outputs"]), "changed": len(r["changed"]), "run_id": r["run_id"]}


def _install_triggers() -> None:
    # Price and cushion outputs are what a stress re-run re-prices; holders and recoveries change with them or
    # weekly, and the nightly run picks them up.
    triggers.model_depends(MODEL, "instrument", "@model:price", triggers.everything)
    triggers.model_depends(MODEL, "site", "@model:cushion", triggers.everything)
    for fld in ("total_usd", "annual_usd", "start", "term_years"):
        triggers.model_depends(MODEL, "obligation", fld, triggers.everything)
    triggers.register_runner(MODEL, _run_keys)


_install_triggers()


@app.periodic(cron=CRON_UTC, periodic_id="stress_nightly")
@app.task(name="models.stress", queue="models", queueing_lock="models.stress", lock="models.stress")
def stress_task(timestamp: int | None = None) -> dict[str, Any]:
    return _run_keys(["*"], "schedule")


@app.task(name="models.stress_custom", queue="stress")  # the worker's reserved stress lane (marden.worker)
def stress_custom_task(request: dict[str, Any], key: str) -> dict[str, Any]:
    return run_custom(request, key)


def _params(items: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items or []:
        k, _, v = item.partition("=")
        try:
            out[k] = float(v)
        except ValueError:
            out[k] = v
    return out


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as-of", type=date.fromisoformat, help="YYYY-MM-DD (default today, US Eastern)")
    p.add_argument("--scenario", help="run one request instead of the saved set (nothing is stored)")
    p.add_argument("--severity", default="base", choices=SEVERITIES)
    p.add_argument("--param", action="append", help="k=v (repeatable)")
    p.add_argument("--no-mark", action="store_true", help="do not mark read models dirty")
    p.add_argument("--json", action="store_true")


@cli.action("model", MODEL, help="Stress scenarios: shocks re-priced by the price engine", configure=_args)
def stress_cli(args: argparse.Namespace) -> int:
    if args.scenario:
        with db.transaction() as conn:
            day = args.as_of or business_today()
            out = run_request(
                conn,
                Book.build(conn, day),
                {"scenario": args.scenario, "params": _params(args.param), "severity": args.severity},
            )
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    r = run(args.as_of, trigger="manual", mark=not args.no_mark)
    if args.json:
        print(json.dumps(r["outputs"], indent=2, ensure_ascii=False))
        return 0
    print(f"stress {VERSION} as of {r['as_of']} (run {r['run_id']}): {len(r['outputs'])} outputs")
    for sid, o in r["outputs"].items():
        if "kpis" in o:
            k = o["kpis"]
            print(
                f"  {sid:34} exposed ${k['exposedB']}B lost ${k['valueLostB']}B ({k['valueLostPct']}%) notes "
                f"{k.get('tenantNotesLossB')} <95 {len(k['loansBelow95'])} funds {k['fundsHit']} rows {len(o['rows'])}"
            )
        elif "hops" in o:
            print(f"  {sid:34} at risk ${o['totalAtRiskB']}B hops {len(o['hops'])} instruments {len(o['instruments'])}")
    return 0


def stored_result(conn: Conn, subject: str, day: date) -> dict[str, Any] | None:
    out = runs.latest_output(conn, MODEL, "scenario", subject, day)
    return out["output"] if out is not None else None
