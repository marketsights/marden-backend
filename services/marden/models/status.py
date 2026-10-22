"""Model `status`: the chain status of every financed site (00-global "Status rules").

    python -m marden model status [--as-of YYYY-MM-DD] [--site jupiter ...] [--json]

For each site the four ChainLinks (end_user, tenant, developer, lenders) with LinkStatus, ruleCode
(E1/E2, T1/T2, D1/D2, L1/L2), the reason and the statement that tripped the rule, and the ChainStatus
(worst link). L2's price test is rate-neutral from v0.2: a fixed coupon's spread move since issue (else its
first Marden price) worth 5+ points, a floating-rate loan's price below 95 (status_credit). One
`model_output` per site and as-of day: model 'status', version VERSION, subject ('site', slug), output =
{chain: ChainLink[], status: ChainStatus, missing: [...]} - the `chain` and `status` pieces of types.ts
SiteSummary, plus the inputs that were not on file. Rules: status_rules.py; inputs: status_inputs.py;
method: docs/methods/status-rules-v0.1.md.

A run for a past day is a backtest: it reads only what was public by that day (marden.models.pit) and
marks nothing. A run for today marks the read models that show the chain (site, lists, payers,
instruments, data-center rows) dirty for each site whose output changed, through
`mark_dirty_for('site', slug, '@model:status')`, which also marks models that take the status as input.

When it runs (spec 22 "What runs when"): daily at 09:53 UTC (05:53 EDT / 04:53 EST), after the overnight
collectors and before the 10:05 UTC screen rebuild; and within a minute of any published input
(`models.run_dirty`): the site facts and events it reads, its chain parties' funding facts and events, its
instruments' terms, status and events, the price model's output on them, and events on approved campuses
and plants.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date
from typing import Any

from marden import cli
from marden.core import db
from marden.core.db import Conn
from marden.core.jsonutil import canonical_json, sha256_hex, to_jsonable
from marden.core.queue import app
from marden.models import runs, triggers
from marden.models.status_inputs import ENTITY_FIELDS, INSTRUMENT_FIELDS, PRICE_MODEL, SITE_FIELDS, Inputs, load
from marden.models.status_rules import METHOD_VERSION, evaluate, rating_pool
from marden.readmodels.context import business_today
from marden.readmodels.deps import mark_dirty_for, via

log = logging.getLogger(__name__)

MODEL = "status"
VERSION = "0.2"  # 0.2: L2 reads the Marden price rate-neutral (status_credit)
CRON_UTC = "53 9 * * *"
OUTPUT_FIELD = f"@model:{MODEL}"


def _inputs_hash(inputs: Inputs, slugs: list[str]) -> str:
    used: list[Any] = []
    for slug in slugs:
        site = inputs.sites[slug]
        used.append([slug, sorted({it.origin for it in site.items + site.instrument_items}),
                     [[p.instrument_id, p.value, p.as_of.isoformat()] for p in site.prices]])
    used.append(sorted({it.origin for items in inputs.entity_items.values() for it in items}))
    return sha256_hex(canonical_json(used))


def _changed(conn: Conn, slug: str, as_of: date, output: dict[str, Any]) -> bool:
    prev = conn.execute(
        """SELECT output FROM model_output WHERE model = %s AND subject_type = 'site' AND subject_id = %s
           AND as_of <= %s ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1""",
        (MODEL, slug, as_of),
    ).fetchone()
    return prev is None or canonical_json(prev["output"]) != canonical_json(to_jsonable(output))


def run(as_of: date | None = None, sites: list[str] | None = None, *, trigger: str = "manual") -> dict[str, Any]:
    """Compute and store the status of every site (or `sites`) as of `as_of` (default today, US Eastern).
    Returns {as_of, run_id, outputs: {slug: output}, changed: [slugs], counts: {ChainStatus: n}}."""
    day = as_of or business_today()
    if day > business_today():
        raise ValueError(f"status as of {day.isoformat()} is after today")
    run_id = runs.start_run(MODEL, VERSION, trigger=trigger)
    try:
        with db.transaction() as conn:
            inputs = load(conn, day)  # every site: tenant ratings are pooled across sites
            slugs = sorted(s for s in inputs.sites if sites is None or s in sites)
            pool = rating_pool(inputs)
            outputs, changed = {}, []
            live = day == business_today()
            for slug in slugs:
                output = evaluate(inputs.sites[slug], inputs, pool)
                is_new = _changed(conn, slug, day, output)
                runs.write_output(conn, run_id, MODEL, VERSION, "site", slug, day, output)
                outputs[slug] = output
                if is_new:
                    changed.append(slug)
                    if live:
                        mark_dirty_for("site", slug, OUTPUT_FIELD, conn=conn, reason=f"model:{MODEL}@{run_id}")
            digest = _inputs_hash(inputs, slugs)
    except Exception as exc:
        runs.finish_run(run_id, ok=False, error=f"{type(exc).__name__}: {exc}")
        raise
    runs.finish_run(run_id, ok=True, inputs_hash=digest)
    counts: dict[str, int] = {}
    for out in outputs.values():
        counts[out["status"]] = counts.get(out["status"], 0) + 1
    return {"as_of": day.isoformat(), "run_id": run_id, "outputs": outputs, "changed": changed, "counts": counts,
            "method_version": METHOD_VERSION}


def run_keys(keys: list[str], trigger: str) -> dict[str, Any]:
    """The `models.run_dirty` runner: re-run the marked sites as of today."""
    with db.transaction() as conn:
        known = {r["slug"] for r in conn.execute("SELECT slug FROM site").fetchall()}
    wanted = sorted(k for k in keys if k in known) if "*" not in keys else None
    if wanted == []:
        return {"sites": 0}
    result = run(sites=wanted, trigger=trigger)
    return {"sites": len(result["outputs"]), "changed": result["changed"], "run_id": result["run_id"]}


# ---------------------------------------------------------------- triggers (model dependency table)
_ENTITY_SITES = via("SELECT DISTINCT site_slug FROM chain_party WHERE entity_id = %s")
_INSTRUMENT_SITE = via("SELECT site_slug FROM instrument WHERE id = %s AND site_slug IS NOT NULL")
_CAMPUS_SITES = via("SELECT site_slug FROM site_link WHERE campus_id = %s AND status = 'approved'")
_PLANT_SITES = via(
    """SELECT l.site_slug FROM plant_serves ps JOIN site_link l ON l.campus_id = ps.campus_id
       WHERE ps.plant_id = %s AND l.status = 'approved'"""
)


def _install_triggers() -> None:
    for field in (*SITE_FIELDS, "@event"):
        triggers.model_depends(MODEL, "site", field)
    for field in (*ENTITY_FIELDS, "@event"):
        triggers.model_depends(MODEL, "entity", field, _ENTITY_SITES)
    for field in (*INSTRUMENT_FIELDS, "@event", f"@model:{PRICE_MODEL}"):
        triggers.model_depends(MODEL, "instrument", field, _INSTRUMENT_SITE)
    triggers.model_depends(MODEL, "campus", "@event", _CAMPUS_SITES)
    triggers.model_depends(MODEL, "plant", "@event", _PLANT_SITES)
    triggers.register_runner(MODEL, run_keys)


_install_triggers()


@app.periodic(cron=CRON_UTC, periodic_id="daily_0953_utc")
@app.task(name="models.status", queue="models", queueing_lock="models.status", lock="models.status")
def status_task(timestamp: int | None = None) -> dict[str, Any]:
    result = run(trigger="schedule")
    log.info("models.status %s: %s, changed %d", result["as_of"], result["counts"], len(result["changed"]))
    return {"counts": result["counts"], "changed": len(result["changed"]), "run_id": result["run_id"]}


# ---------------------------------------------------------------- CLI
def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as-of", type=date.fromisoformat, help="YYYY-MM-DD (default today, US Eastern): a past day "
                   "is a backtest on what was public that day")
    p.add_argument("--site", action="append", dest="sites", help="site slug (repeatable; default all)")
    p.add_argument("--json", action="store_true", help="print every output as JSON")


@cli.action("model", MODEL, help="Chain status per financed site (status rules E/T/D/L)", configure=_args)
def run_cli(args: argparse.Namespace) -> int:
    result = run(args.as_of, args.sites, trigger="manual")
    if args.json:
        print(json.dumps(result["outputs"], indent=2, ensure_ascii=False))
        return 0
    print(f"status {VERSION} as of {result['as_of']} (run {result['run_id']}): {result['counts']}; "
          f"changed {len(result['changed'])}")
    for slug, out in result["outputs"].items():
        links = " ".join(
            f"{link['role'][0].upper()}={link['status']}" + (f"/{link['ruleCode']}" if "ruleCode" in link else "")
            for link in out["chain"]
        )
        print(f"  {slug:28} {out['status']:9} {links}")
    return 0
