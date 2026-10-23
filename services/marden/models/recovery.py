"""Model `recovery` (spec 22 "Recovery"): what lenders get, cents per dollar, if the tenant or customer walks.

    python -m marden model recovery [--as-of YYYY-MM-DD] [--instrument ID ...] [--json]

Collateral waterfall per instrument, by what secures it:
- GPU-backed facilities (marden.models.gpu_facilities): units x value per unit (rental index, decline and
  depreciation life; gpu_value) less the liquidation haircut, over the pool's claims (pari passu, nothing
  known ahead of the first lien), for a customer walking today to a year out;
- site-backed debt (marden.models.recovery_shell): powered capacity x value per MW from the cost comps (25th /
  50th / 75th percentile) x the share finished (stated, else estimated: recovery_progress), unfinished works
  less a haircut, plus funded notes' unspent proceeds, less claims ranking ahead, over the claims at its rank;
- issuer-level debt (corporate notes, converts, unsecured loans, parent-secured facilities; v0.3): the
  issuer's balance-sheet waterfall from its latest 10-Q / 10-K (marden.models.recovery_issuer): assets after
  stated haircuts, less SPV and site debt on its own collateral and the parent's secured debt, shared by
  the claims at the instrument's rank, clamped to 5-95c. An issuer without SEC filings keeps a stated
  assumption (n/d here; the price model's 40c convention) and says why.
One `model_output` per instrument (subject ('instrument', id)) = {recovery: Sourced<string> ('62-74' with an en dash,
cents per dollar; InstrumentDetail.recovery), valueB: Sourced<number> (collateral value, $B, base case; for the
issuer waterfall the value left for the instrument's rank), method: 'gpu_collateral' | 'powered_shell' |
'issuer_waterfall' | null, missing, basis}, plus one per issuer entity (subject ('entity', id)) with the
whole waterfall (recovery_issuer.issuer_doc). Ranges are uncalibrated ('model v0').
The basis carries the central estimate the price model prices with (`baseCents`: the 50th-percentile comp
for a powered shell, a default today for GPUs, the base haircuts for an issuer), `rangeCents` [low, high], and
for GPUs `pathCents`, the recovery at each scheduled balance over the loan's life ({years, cents}: the GPUs age
as the loan amortizes).

Runs: weekly, Monday 09:41 UTC (spec 22 "Weekly ... recovery"), and within a minute of an input: the
instrument's terms, its site's capacity, cost and completion facts, any site's cost comps, the GPU market
inputs and the gpu_collateral output, an issuer's XBRL balance sheet, lease commitments and role. A run for
today marks the instrument's read models (`@model:recovery`: instrument_detail, site debt tab, ...); a past
--as-of is a backtest.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from typing import Any

from marden import cli
from marden.core.db import Conn
from marden.core.queue import app
from marden.models import recovery_issuer, recovery_progress, triggers
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
from marden.models.sourced import Batch, m_value, method_version, num, rounded, run_outputs, stored_outputs
from marden.readmodels.deps import via

MODEL = "recovery"
VERSION = "0.3"  # v0.3 (2026-10-22): issuer balance-sheet waterfall for corporate and issuer-level debt
# v0.2 (2026-10-22): estimated completion share, unfinished-works haircut, unspent proceeds
METHOD = method_version(MODEL, VERSION)
CRON_UTC = "41 9 * * 1"
SOURCE = "Marden recovery model"
UNSECURED = "a collateral or issuer waterfall (no site, GPU pool or issuer-level seniority on file)"


def _m(value: Any, day: date) -> dict[str, Any]:
    return m_value(value, day, METHOD, source_name=SOURCE, uncalibrated=True)


def _cents_pair(low: float | None, high: float | None) -> list[int] | None:
    return None if low is None or high is None else [round(min(low, high) * 100), round(max(low, high) * 100)]


def _path(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """One point per time from the day (scheduled months already past all sit at 0: the latest one wins)."""
    by: dict[float, float] = {}
    for years, rec in points:
        by[years] = rec
    return sorted(by.items())


def _targets(conn: Conn, ids: list[str] | None, level: dict[str, str]) -> list[str]:
    rows = conn.execute(
        """SELECT id FROM instrument WHERE (%(ids)s::text[] IS NULL AND (in_v1 OR site_slug IS NOT NULL OR id IN (
                  SELECT subject_id FROM fact WHERE subject_type = 'instrument'
                     AND field IN ('gpu_model', 'gpu_collateral'))))
               OR id = ANY(%(ids)s) ORDER BY id""",
        {"ids": ids},
    ).fetchall()
    out = {r["id"] for r in rows}
    if ids is None:
        out |= set(level)  # issuer-level debt of every issuer (recovery_issuer.issuer_level)
    return sorted(out)


def _collateral(out: dict[str, Any], sites: dict[str, Any]) -> dict[str, Any] | None:
    """An SPV instrument's collateral for the issuer waterfall, from its recovery output: the GPU pool's value
    or the site's value (works, unspent proceeds) and cost spent (total project cost x share finished, else
    the works value)."""
    basis: dict[str, Any] = out["basis"] if isinstance(out.get("basis"), dict) else {}
    value_b = num((out.get("valueB") or {}).get("value"))
    if out.get("method") == "gpu_collateral" and basis.get("pool"):
        pool = sorted(str(x) for x in basis["pool"])
        return {"kind": "gpus", "pool": "gpus:" + ",".join(pool), "label": "GPU pool " + ", ".join(pool),
                "valueUsd": None if value_b is None else value_b * 1e9}
    if out.get("method") == "powered_shell" and basis.get("site"):
        slug = str(basis["site"])
        works = basis.get("worksUsd")
        works_mid = num(works[1]) if isinstance(works, list) and len(works) == 3 else None
        site = sites.get(slug)
        cost_row = site.best("total_project_cost_usd") if site is not None else None
        share = num(basis.get("completion"))
        cost = num(cost_row["value"]) if cost_row is not None else None
        # the cost spent, else (no project cost on file) the works value at the cost comps stands for it
        book = cost * share if cost is not None and share is not None else works_mid
        return {"kind": "site", "pool": f"site:{slug}", "label": f"site {slug}",
                "valueUsd": None if value_b is None else value_b * 1e9, "worksUsd": works_mid, "bookUsd": book}
    return None


def _issuer_outputs(conn: Conn, day: date, batch: Batch, wanted: list[str], level: dict[str, str],
                    sites: dict[str, Any], full: bool) -> None:
    """The issuer waterfall: one entity output per issuer group touched, one instrument output per wanted
    issuer-level instrument (recovery_issuer)."""
    grp, of = recovery_issuer.groups(conn, day)
    keys = sorted({of[i] for i in wanted if i in of})
    if not keys:
        return
    member_ids = [r["id"] for k in keys for r in grp[k].instruments]
    stored = stored_outputs(conn, MODEL, "instrument", member_ids, day)
    for key in keys:
        g = grp[key]
        collateral: dict[str, dict[str, Any]] = {}
        for r in g.instruments:
            this = batch.outputs.get(("instrument", r["id"]))
            prior = stored.get(r["id"])
            doc = this if this is not None else (prior["output"] if prior is not None and isinstance(
                prior["output"], dict) else None)
            got = _collateral(doc, sites) if doc else None
            if got:
                collateral[r["id"]] = got
        inp = recovery_issuer.load(conn, day, g, collateral, sites)
        doc = recovery_issuer.issuer_doc(inp, day, f"{METHOD} (model v0)")
        if full or any(of.get(i) == key for i in wanted):
            batch.add("entity", g.issuer, doc)
        for iid in wanted:
            if of.get(iid) != key:
                continue
            batch.add("instrument", iid, _issuer_output(doc, iid, level.get(iid, "unsecured"), day))


def _issuer_output(doc: dict[str, Any], iid: str, rank: str, day: date) -> dict[str, Any]:
    head = {"methodVersion": f"{METHOD} (model v0)", "issuer": doc["issuer"], "issuerName": doc["name"],
            "filer": doc.get("filer"), "rank": rank}
    if doc.get("method") is None or not doc.get("ranks"):
        why = "; ".join(doc.get("missing") or []) or "no balance sheet for the issuer waterfall"
        return {"recovery": _m(None, day), "valueB": _m(None, day), "method": None, "missing": [why],
                "basis": {**head, "assumedCents": doc.get("assumedCents"), "why": why}}
    use = rank if rank in doc["ranks"] else "unsecured"
    got = doc["ranks"].get(use) or {}
    lo, hi = (got.get("rangeCents") or [None, None])
    base = got.get("cents")
    text = None if base is None else (str(base) if lo is None or hi is None or lo == hi else f"{lo}\u2013{hi}")
    value = doc["leftUsd"] if use == "unsecured" else None
    return {"recovery": _m(text, day), "valueB": _m(rounded(value / 1e9 if value is not None else None, 4), day),
            "method": recovery_issuer.METHOD_KEY, "missing": list(doc.get("missing") or []),
            "basis": {**head, "rank": use, "baseCents": base, "rangeCents": [lo, hi], "filing": doc.get("filing"),
                      "periodEnd": doc.get("periodEnd"), "assetsUsd": doc.get("assetsUsd"),
                      "securedUsd": doc.get("securedUsd"), "leftUsd": doc.get("leftUsd"),
                      "unsecuredClaimsUsd": doc.get("unsecuredClaimsUsd"), "lines": doc["lines"].get(use) or [],
                      "rule": doc.get("rule")}}


def compute(conn: Conn, day: date, ids: list[str] | None) -> Batch:
    level = recovery_issuer.issuer_level(conn)
    targets = _targets(conn, ids, level)
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
            path = _path(v.recovery_path)
            out = {"recovery": _m(range_text(v.recovery_low, v.recovery_high), day),
                   "valueB": _m(rounded(v.value / 1e9 if v.value else None, 4), day), "method": "gpu_collateral",
                   "missing": v.missing,
                   "basis": {"methodVersion": f"{METHOD} (model v0)", "poolBalanceB": rounded(
                       v.pool_balance / 1e9 if v.pool_balance else None, 4), "pool": v.pool,
                       "units": rounded(v.units, 0), "haircut": HAIRCUT,
                       "valuePerUnitUsd": rounded(v.per_unit, 0), "seniorClaimsB": 0.0,
                       "rule": "collateral value / pool balance, capped at 100c, default today to a year out",
                       "baseCents": round(path[0][1] * 100) if path else None,
                       "rangeCents": _cents_pair(v.recovery_low, v.recovery_high),
                       "pathCents": [{"years": y, "cents": round(r * 100, 1)} for y, r in path]}}
        elif site_of.get(iid):
            slug = site_of[iid]
            claims = site_claims(conn, day, slug, set(by_id))
            site = sites.get(slug)
            stated = site.completion() if site else (None, "no site facts")
            progress = recovery_progress.estimate(conn, day, slug, site.rows if site else {}, stated,
                                                  site.energized() if site else None)
            res = shell_recovery(site, comp, claims, iid, pue, progress)
            out = {"recovery": _m(range_text(res.low, res.high) if res.base is not None else None, day),
                   "valueB": _m(rounded(res.value[1] / 1e9, 4) if res.value else None, day),
                   "method": "powered_shell", "missing": res.missing,
                   "basis": {"methodVersion": f"{METHOD} (model v0)", "site": slug,
                             "baseCents": None if res.base is None else round(res.base * 100),
                             "rangeCents": _cents_pair(res.low, res.high),
                             "seniorClaimsB": rounded(res.ahead / 1e9 if res.ahead is not None else None, 4),
                             "pariClaimsB": rounded(res.pari / 1e9 if res.pari is not None else None, 4), **res.basis}}
        elif iid in level:
            continue  # the issuer waterfall, below: it needs every collateral value of this run first
        else:
            out = {"recovery": _m(None, day), "valueB": _m(None, day), "method": None, "missing": [UNSECURED],
                   "basis": {"methodVersion": f"{METHOD} (model v0)"}}
        batch.add("instrument", iid, out)
    _issuer_outputs(conn, day, batch, [i for i in targets if i in level and ("instrument", i) not in batch.outputs],
                    level, sites, ids is None)
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
_CAMPUS_INSTRUMENTS = via("SELECT i.id FROM site_link sl JOIN instrument i ON i.site_slug = sl.site_slug "
                          "WHERE sl.campus_id = %s")
_BUILDING_INSTRUMENTS = via("SELECT i.id FROM building b JOIN site_link sl ON sl.campus_id = b.campus_id "
                            "JOIN instrument i ON i.site_slug = sl.site_slug WHERE b.id = %s")


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
    # the completion estimate (recovery_progress): campus plans and imagery, building starts and stages,
    # the site's cushion projection
    for fld in recovery_progress.CAMPUS_FIELDS:
        triggers.model_depends(MODEL, "campus", fld, _CAMPUS_INSTRUMENTS)
    for fld in recovery_progress.BUILDING_FIELDS:
        triggers.model_depends(MODEL, "building", fld, _BUILDING_INSTRUMENTS)
    triggers.model_depends(MODEL, "site", "@model:cushion", _SITE_INSTRUMENTS)
    for fld in ENTITY_FIELDS:
        triggers.model_depends(MODEL, "entity", fld, triggers.everything)
    # the issuer waterfall: a filer's balance sheet, lease commitments and role move every issuer-level
    # instrument of the issuer (all of them: the filer may be a ticker twin of the issuing holding company);
    # a claim's amount, rank or status moves its whole issuer group
    for fld in (*recovery_issuer.XBRL_FIELDS, *recovery_issuer.ENTITY_FIELDS):
        triggers.model_depends(MODEL, "entity", fld, triggers.everything)
    for fld in recovery_issuer.INSTRUMENT_FIELDS:
        triggers.model_depends(MODEL, "instrument", fld, recovery_issuer.group_instruments)
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
        if "valueB" not in o:  # an issuer's balance-sheet waterfall (subject entity)
            print(f"  {iid:40} {'issuer':15} unsecured {o.get('unsecuredCents') or 'n/d'}c; "
                  f"missing {(o.get('missing') or [])[:1]}")
            continue
        value = o["valueB"]["value"]
        print(f"  {iid:40} {o['method'] or '-':15} recovery {o['recovery']['value'] or 'n/d':8} "
              f"value {'n/d' if value is None else f'${value}B'}; missing {o['missing'][:2]}")
    return 0
