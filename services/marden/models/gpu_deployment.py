"""Model `gpu_deployment` (spec 19 "Delivered vs energized"): how many of the GPUs a loan financed are
installed, powered and earning, and what each idle month costs the lender. Subject ('series', 'all').

    python -m marden model gpu_deployment [--as-of YYYY-MM-DD] [--json]

Output = GpuDeploymentPage {asOf, facilities: GpuDeployment[], companies: CompanyCip[], totals} plus
missing and basis. Per GPU-backed facility (marden.models.gpu_facilities):
- unitsFinanced: the collateral pool's units financed so far (gpu_collateral's count);
- unitsDelivered: filed or reported delivery counts ('received', 'accepted delivery', 'placed in service');
- unitsEnergized = min(delivered, energized IT MW at the destination site / (kW per unit x PUE));
- idle units and share, months idle, interest on the idle share (balance x idle share x coupon x months / 12),
  value lost while idle and LTV if energized on schedule follow from those;
- status: Earning (idle share < 10%), Partly idle (10-50%), Idle (> 50%), else n/d.
Marden holds no delivery counts and no OEM rack kW per unit yet, so every facility is n/d and says what is
missing (never a guess); the energized MW at its site, when filed or reported, is listed as evidence; LTV
today is the gpu_collateral valuation. Companies: construction in progress vs PP&E in service per quarter
from XBRL (CIP and PP&E net, F; in service = PP&E net - CIP, share and change computed, M), usable 40
days after the period end (marden.models.pit) and a restated value from the day its amended filing was
made (the filing date in the fact's quote location).

Runs: daily 09:31 UTC and within a minute of a facility fact, a site's delivered / energization facts, a
company CIP or PP&E fact, or a GPU market input. A run for today marks the 'gpu_deployment' view and, via
'@model:gpu_deployment', each facility's gpu_collateral document.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import date
from typing import Any

from marden import cli
from marden.core.db import Conn, Row
from marden.core.queue import app
from marden.models import pit, triggers
from marden.models.gpu_facilities import FACILITY_FIELDS, pools, quarter_label, value_facility
from marden.models.gpu_facilities import load as load_facilities
from marden.models.gpu_inputs import SERIES_FIELDS, SERIES_PREFIXES
from marden.models.gpu_inputs import load as load_market
from marden.models.gpu_value import fit_decline
from marden.models.sourced import Batch, fact_value, m_value, method_version, num, rounded, run_outputs

MODEL = "gpu_deployment"
VERSION = "0.1"
METHOD = method_version(MODEL, VERSION)
CRON_UTC = "31 9 * * *"
SOURCE = "Marden GPU deployment model"
SITE_FIELDS = ("delivered_mw", "critical_it_mw", "expected_energization")
COMPANY_KINDS = ("neocloud", "cloud", "developer", "company")
CIP, PPE = "construction_in_progress_usd", "ppe_net_usd"
QUARTERS_SHOWN = 8
MAX_QUARTER_GAP_DAYS = 100  # consecutive quarters only for the change
SOURCED_FIELDS = ("unitsFinanced", "unitsDelivered", "unitsEnergized", "idleUnits", "idleShare", "monthsIdle",
                  "idleInterestM", "valueLostWhileIdleM", "ltvToday", "ltvIfOnSchedule")


def _m(value: Any, day: date) -> dict[str, Any]:
    return m_value(value, day, METHOD, source_name=SOURCE, uncalibrated=True)


def _site_evidence(conn: Conn, day: date, slugs: list[str]) -> dict[str, list[dict[str, Any]]]:
    rows = pit.fact_rows(conn, day, "site", slugs, list(SITE_FIELDS)) if slugs else []
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for r in rows:
        if r["tag"] == "M" or (r["subject_id"], r["field"]) in seen:
            continue
        seen.add((r["subject_id"], r["field"]))
        label = {"delivered_mw": "MW delivered (energized) at", "critical_it_mw": "critical IT MW planned at",
                 "expected_energization": "expected energization at"}[r["field"]]
        out[r["subject_id"]].append({"date": r["valid_from"].isoformat(),
                                     "text": f"{r['value']} {label} {r['subject_id']}", "tag": r["tag"],
                                     "sourceName": r["source_name"] or "",
                                     "sourceUrl": r.get("source_url") or ""})
    return out


def _coupon(f: Any) -> Row | None:
    row = f.facts.get("coupon_rate_pct")
    return row if row is not None and num(row["value"]) is not None else None


def facility_doc(f: Any, v: Any, day: date, pue: float | None, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    nd = _m(None, day)
    missing = ["units delivered (8-K, 10-Q or press delivery counts)", "OEM rack kW per unit"]
    if not evidence:
        missing.append("energized IT MW at the destination site" + ("" if f.site_slug else " (no site linked)"))
    if _coupon(f) is None:
        missing.append("coupon")
    missing += [m for m in v.missing if m not in missing]
    units = _m(round(v.units), day) if v.units is not None else nd
    return {
        "scope": "facility", "id": f.instrument_id, "name": f.name, "model": f.model_text,
        "unitsFinanced": units, "unitsDelivered": nd, "unitsEnergized": nd, "idleUnits": nd, "idleShare": nd,
        "monthsIdle": nd, "idleInterestM": nd, "valueLostWhileIdleM": nd,
        "ltvToday": _m(rounded(v.ltv, 4), day), "ltvIfOnSchedule": nd,
        "status": "n/d", "missing": missing, "evidence": evidence,
        "assumptions": {"kwPerUnit": nd, "pue": pue, "couponPct": fact_value(_coupon(f), as_of=day),
                        "methodVersion": f"{METHOD} (model v0)"},
    }


_FILED = re.compile(r"filed (\d{4}-\d{2}-\d{2})")


def filed_on(row: Row) -> date | None:
    """The filing date an XBRL fact names in its quote location ('... 10-K/A ... filed 2026-03-02')."""
    m = _FILED.search(row.get("quote_loc") or "")
    return date.fromisoformat(m.group(1)) if m else None


def companies(conn: Conn, day: date) -> list[dict[str, Any]]:
    kinds = {r["id"]: r for r in conn.execute(
        "SELECT id, name, kind FROM entity WHERE kind = ANY(%s)", (list(COMPANY_KINDS),)).fetchall()}
    rows = pit.fact_rows(conn, day, "entity", list(kinds), [CIP, PPE]) if kinds else []
    by: dict[tuple[str, date], dict[str, Row]] = defaultdict(dict)
    for r in rows:  # a restated value (10-K/A, 10-Q/A) replaces the original once its filing is public
        filed = filed_on(r)
        if num(r["value"]) is None or (filed is not None and filed > day):
            continue
        have = by[(r["subject_id"], r["valid_from"])].get(r["field"])
        if have is None or (filed_on(have) or date.min) < (filed or date.min):
            by[(r["subject_id"], r["valid_from"])][r["field"]] = r
    out: list[dict[str, Any]] = []
    per_company: dict[str, list[tuple[date, dict[str, Row]]]] = defaultdict(list)
    for (eid, period), fields in by.items():
        if CIP in fields:
            per_company[eid].append((period, fields))
    for eid, periods in sorted(per_company.items()):
        periods.sort(key=lambda p: p[0])
        rows_out = []
        for k, (period, fields) in enumerate(periods):
            cip = float(fields[CIP]["value"])
            ppe = num(fields[PPE]["value"]) if PPE in fields else None
            prev = periods[k - 1] if k > 0 else None
            consecutive = prev is not None and (period - prev[0]).days <= MAX_QUARTER_GAP_DAYS
            prev_cip = float(prev[1][CIP]["value"]) if prev is not None and consecutive else None
            rows_out.append({
                "company": kinds[eid]["name"], "quarter": quarter_label(period.strftime("%Y-%m")),
                "cipB": fact_value(fields[CIP], round(cip / 1e9, 4)),
                "ppeInServiceB": m_value(round((ppe - cip) / 1e9, 4) if ppe is not None else None, period, METHOD,
                                         source_name="PP&E net - construction in progress (XBRL)"),
                "cipShare": round(cip / ppe, 4) if ppe else None,
                "changeQoQB": round((cip - prev_cip) / 1e9, 4) if prev_cip is not None else None,
            })
        out += rows_out[-QUARTERS_SHOWN:]
    return out


def _total(day: date, values: list[float | None]) -> dict[str, Any]:
    known = [v for v in values if v is not None]
    return _m(round(sum(known)) if known else None, day)


def page(conn: Conn, day: date) -> tuple[dict[str, Any], list[Any]]:
    market = load_market(conn, day)
    fit = fit_decline(market, market.median_life())
    facilities = load_facilities(conn, day)
    by_pool = pools(facilities)
    evidence = _site_evidence(conn, day, sorted({f.site_slug for f in facilities if f.site_slug}))
    pue_row = market.pue
    pue_val = pue_row["value"] if pue_row is not None and isinstance(pue_row["value"], dict) else {}
    lo, hi = num(pue_val.get("low")), num(pue_val.get("high"))
    pue = round((lo + hi) / 2, 3) if lo and hi else None
    docs = [facility_doc(f, value_facility(f, market, fit, day, by_pool[f.pool_key]), day, pue,
                         evidence.get(f.site_slug or "", [])) for f in facilities]
    missing = [] if docs else ["GPU-backed facilities (gpu_model / gpu_collateral facts)"]
    out = {
        "asOf": day.isoformat(), "facilities": docs, "companies": companies(conn, day),
        "totals": {"deliveredUnits": _total(day, [d["unitsDelivered"]["value"] for d in docs]),
                   "energizedUnits": _total(day, [d["unitsEnergized"]["value"] for d in docs]),
                   "idleShare": _m(None, day)},
        "missing": missing,
        "basis": {"methodVersion": f"{METHOD} (model v0)", "pue": {"value": pue, "basis": "midpoint of the published "
                  "data-center PUE range", "sourceUrl": pue_row.get("source_url") if pue_row else None},
                  "statusRule": "Earning < 10% idle; Partly idle 10-50%; Idle > 50%; n/d without deliveries and "
                                "energization", "companyKinds": list(COMPANY_KINDS)},
    }
    return out, [market.origins, [sorted(int(r["id"]) for r in f.facts.values()) for f in facilities]]


def run(as_of: date | None = None, *, trigger: str = "manual", mark: bool = True) -> dict[str, Any]:
    def compute(conn: Conn, day: date) -> Batch:
        doc, origins = page(conn, day)
        batch = Batch()
        batch.add("series", "all", doc)
        batch.views.add(("gpu_deployment", "all"))
        batch.inputs = origins
        return batch

    return run_outputs(MODEL, VERSION, as_of, compute, trigger=trigger, mark=mark)


def _gpu_series(conn: Conn, subject_id: str) -> list[str]:
    return ["*"] if subject_id.startswith(SERIES_PREFIXES) else []


def _install_triggers() -> None:
    for fld in FACILITY_FIELDS:
        triggers.model_depends(MODEL, "instrument", fld, triggers.everything)
    for fld in SITE_FIELDS:
        triggers.model_depends(MODEL, "site", fld, triggers.everything)
    for fld in (CIP, PPE, "gpu_useful_life_years"):
        triggers.model_depends(MODEL, "entity", fld, triggers.everything)
    for fld in (*SERIES_FIELDS, "value"):
        triggers.model_depends(MODEL, "series", fld, _gpu_series)
    triggers.register_runner(MODEL, lambda keys, trigger: {"run_id": run(trigger=trigger)["run_id"]})


_install_triggers()


@app.periodic(cron=CRON_UTC, periodic_id="gpu_deployment_daily")
@app.task(name="models.gpu_deployment", queue="models", queueing_lock="models.gpu_deployment",
          lock="models.gpu_deployment")
def gpu_deployment_task(timestamp: int | None = None) -> dict[str, Any]:
    return {"run_id": run(trigger="schedule")["run_id"]}


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as-of", type=date.fromisoformat, help="YYYY-MM-DD (default today, US Eastern)")
    p.add_argument("--no-mark", action="store_true", help="do not mark read models dirty")
    p.add_argument("--json", action="store_true", help="print the output as JSON")


@cli.action("model", MODEL, help="GPU delivered vs energized per facility, CIP vs PP&E per company", configure=_args)
def run_cli(args: argparse.Namespace) -> int:
    result = run(args.as_of, trigger="manual", mark=not args.no_mark)
    out = result["outputs"]["all"]
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    print(f"gpu_deployment {VERSION} as of {result['as_of']} (run {result['run_id']}): {len(out['facilities'])} "
          f"facilities, {len(out['companies'])} company quarters")
    for d in out["facilities"]:
        print(f"  {d['id']:16} financed {d['unitsFinanced']['value'] or 'n/d':>7} status {d['status']:4} "
              f"LTV {d['ltvToday']['value'] or 'n/d'} evidence {len(d['evidence'])}; missing {len(d['missing'])}")
    for c in out["companies"]:
        print(f"  {c['company']:28} {c['quarter']} CIP ${c['cipB']['value']:.2f}B in service "
              f"{c['ppeInServiceB']['value']} share {c['cipShare']} QoQ {c['changeQoQB']}")
    return 0
