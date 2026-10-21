"""Model `payer_capacity` (spec 24): can the payers behind the debt afford their leases and contracts, and how
close is each to a downgrade. Labelled M 'payer_capacity v0.1 (model v0)' until the backtest checks it.

    python -m marden model payer_capacity [--as-of YYYY-MM-DD] [--entity ID ...] [--json]

One output per payer (the payer / payer_capacity read models' keys: chain tenants and end users, and the
clouds, AI labs and neoclouds on the obligation map), subject ('entity', id) = PayerCapacity (types.ts) +
`missing`, `basis`:
- ratings: RatingState per agency the rating facts name (credit_inputs: rating, outlook, last action; tag
  R as reported). `triggers` stays empty: the agencies' stated downgrade thresholds are not collected yet,
  and a threshold is never taken from memory;
- spreadBp: the price engine's bond-implied CDS (model 'cds') for the payer;
- leaseAdjLeverageToday = (debt + lease liabilities) / EBITDA (SEC XBRL, trailing twelve months);
  leaseAdjLeveragePeak, years[], fundingGap3yB: the forward path (payer_years);
- triggerHeadroomTurns, breachYear: n/d until agency triggers are collected (listed in `missing`);
- aiRevenueCoverage: for an AI lab, its reported annualized revenue (funding facts) / its contracted
  compute payments next year on the obligation map; n/d where AI revenue is not disclosed (never estimated);
- topCounterparty: the filed customer concentration (`top_customers`, "Customer A 36%"), else the
  largest payer's share of the payer's filed backlog (obligation map);
- downgradeRisk (Marden view, never a rating): points for each negative outlook (up to 2), a downgrade in
  the last 12 months, sitting on the last investment-grade rung (BBB- / Baa3), leverage rising one turn or
  more along the path, and a 3-year funding gap above a quarter of three years' operating cash flow;
  3+ = High, 1-2 = Watch, 0 = Low; n/d when no
  rating and no filed financials are public;
- exposed: ExposedInstrument per instrument behind a lease, compute contract or guarantee the payer owes,
  with the rating-tied term its documents quote (agreement_term / covenant_test, tag F) and its holders.

Runs: daily 09:29 UTC and within a minute of the payer's XBRL, rating, funding or obligation facts or a
new 'cds' output (marden.models.triggers). A past --as-of is a backtest (inputs public that day: XBRL 40
days after the period end).
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date, timedelta
from typing import Any

from marden import cli
from marden.core.db import Conn
from marden.core.queue import app
from marden.models import graph, pit, stress_inputs, triggers
from marden.models.credit_inputs import FIN_FIELDS, IG_FLOOR, AgencyRating, load_financials, lowest_notch, ratings
from marden.models.graph import usd_of_text
from marden.models.payer_years import COMPUTE_TYPES, path
from marden.models.runs import latest_output
from marden.models.sourced import Batch, fact_value, m_value, method_version, num, run_outputs
from marden.readmodels.context import building
from marden.readmodels.views.payers_common import LINK_LABELS, OWED_TYPES, payer_ids

MODEL, VERSION = "payer_capacity", "0.1"
METHOD = method_version(MODEL, VERSION)
CRON_UTC = "29 9 * * *"
KIND_LABELS = {"cloud": "Cloud", "ai_lab": "AI lab", "neocloud": "Neocloud"}
_SHARE = re.compile(r"([A-Z][\w&.\- ]{1,40}?)\s*~?(\d{1,3}(?:\.\d+)?)%")
_RATING_TERM = re.compile(r"rating|investment[- ]grade|\bIG\b|\bBBB|\bBaa", re.I)
DOWNGRADE = re.compile(r"\b(cut|downgraded|lowered)\b", re.I)
GAP_SHARE = 0.25  # a funding gap counts once it exceeds this share of three years' operating cash flow
ENTITY_FIELDS = (*FIN_FIELDS, "funding_fact", "top_customers")


def _m(value: Any, day: date) -> dict[str, Any]:
    return m_value(value, day, METHOD, source_name="Marden payer capacity", uncalibrated=True)


def _nd(day: date, tag: str = "R") -> dict[str, Any]:
    return fact_value(None, as_of=day, tag=tag)


def rating_states(rs: dict[str, AgencyRating], day: date) -> list[dict[str, Any]]:
    out = []
    for agency in ("S&P", "Moody's", "Fitch"):
        ar = rs.get(agency)
        if ar is None:
            continue
        out.append({"agency": agency, "rating": fact_value(ar.row, ar.rating),
                    "outlook": fact_value(ar.row, ar.outlook) if ar.outlook else _nd(day),
                    "lastAction": fact_value(ar.row, ar.action) if ar.action else _nd(day), "triggers": []})
    return out


def _action_date(text: str | None) -> date | None:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", text or "")
    return date.fromisoformat(m.group(1)) if m else None


def downgrade_band(rs: dict[str, AgencyRating], lev_today: float | None, lev_peak: float | None,
                   gap_3y: float | None, ocf_b: float | None, has_financials: bool, day: date) -> tuple[str, list[str]]:
    reasons = []
    neg = [a for a, r in rs.items() if r.outlook == "Negative"]
    reasons += [f"{a} outlook negative" for a in neg[:2]]
    cuts = [a for a, r in rs.items() if r.action and DOWNGRADE.search(r.action)
            and (d := _action_date(r.action)) is not None and day - timedelta(days=365) <= d <= day]
    if cuts:
        reasons.append(f"downgraded in the last 12 months ({', '.join(cuts)})")
    low = lowest_notch(rs)
    if low == IG_FLOOR:
        reasons.append("on the last investment-grade rung (BBB- / Baa3)")
    if lev_today is not None and lev_peak is not None and lev_peak - lev_today >= 1.0:
        reasons.append(f"lease-adjusted leverage rises {lev_peak - lev_today:.1f}x along the path")
    if gap_3y is not None and ocf_b is not None and gap_3y > GAP_SHARE * 3 * max(ocf_b, 0.0):
        reasons.append(f"3-year funding gap ${gap_3y:.1f}B, over a quarter of 3 years' operating cash flow")
    if not rs and not has_financials:
        return "n/d", reasons
    return ("High" if len(reasons) >= 3 else "Watch" if reasons else "Low"), reasons


def top_counterparty(conn: Conn, eid: str, day: date, edges: list[graph.Edge], backlog: Any,
                     names: dict[str, str]) -> dict[str, Any] | None:
    row = pit.current_of(pit.fact_rows(conn, day, "entity", [eid], ["top_customers"]))
    if row is not None and isinstance(row["value"], str):
        m = _SHARE.search(row["value"])
        if m:
            return {"name": m.group(1).strip(" :"), "share": fact_value(row, round(float(m.group(2)) / 100, 4))}
    owed = [e for e in edges if e.payee == eid and e.pays and e.total is not None]
    if not owed or backlog is None:
        return None
    best = max(owed, key=lambda e: e.total.usd)  # type: ignore[union-attr]
    return {"name": names.get(best.payer, best.payer), "share": _m(round(min(best.total.usd / backlog.usd, 1.0), 4),
                                                                   day)}  # type: ignore[union-attr]


def ai_coverage(conn: Conn, eid: str, kind: str, day: date, edges: list[graph.Edge]) -> tuple[Any, list[str]]:
    if kind != "ai_lab":
        return None, ["AI revenue not disclosed separately (coverage not estimated)"]
    revenue = None
    for r in pit.fact_rows(conn, day, "entity", [eid], ["funding_fact"]):
        label = str((r["value"] or {}).get("label") or "") if isinstance(r["value"], dict) else ""
        if "revenue" in label.lower() and (usd := usd_of_text(r["value"].get("value"))):
            revenue = usd
            break
    year = day.year + 1
    pay = sum(e.annual_usd(year) or 0.0 for e in edges if e.payer == eid and e.type in COMPUTE_TYPES)
    if revenue is None or not pay:
        return None, [f"entity {eid}: {'reported revenue' if revenue is None else f'compute payments in {year}'}"]
    return round(revenue / pay, 3), []


def exposed(conn: Conn, eid: str, edges: list[graph.Edge], day: date) -> list[dict[str, Any]]:
    links: dict[str, str] = {}
    for e in sorted((e for e in edges if e.payer == eid and e.type in OWED_TYPES),
                    key=lambda e: (OWED_TYPES.index(e.type), e.id)):
        for iid in e.instruments():
            links.setdefault(iid, LINK_LABELS[e.type])
    info = stress_inputs.load(conn, day, list(links))
    terms: dict[str, list] = {}
    for r in pit.fact_rows(conn, day, "instrument", list(links), ["agreement_term", "covenant_test"]):
        terms.setdefault(r["subject_id"], []).append(r)
    rows = []
    for iid, link in links.items():
        if iid not in info:
            continue
        term = next((r for r in terms.get(iid, []) if isinstance(r["value"], dict) and _RATING_TERM.search(
            f"{r['value'].get('type') or ''} {r['value'].get('threshold') or r['value'].get('metric') or ''}")), None)
        v = term["value"] if term else {}
        rows.append({"instrumentId": iid, "name": info[iid].name, "link": link, "amountB": info[iid].size_b(day),
                     "ratingTrigger": fact_value(term, str(v.get("threshold") or v.get("metric"))) if term
                     else _nd(day, "F"),
                     "onTrip": fact_value(term, str(v["consequence"])) if term and v.get("consequence")
                     else _nd(day, "F"),
                     "holdersCount": info[iid].holder_count})
    rows.sort(key=lambda r: (-(r["amountB"]["value"] or 0), r["name"]))
    return rows


def payer_doc(conn: Conn, eid: str, day: date, edges: list[graph.Edge], fin: Any, ent: dict[str, Any],
              names: dict[str, str]) -> dict[str, Any]:
    rs = ratings(conn, day, eid)
    p = path(fin, edges, day, METHOD)
    missing = list(p.missing)
    cds = latest_output(conn, "cds", "entity", eid, day)
    spread = num(cds["output"].get("spreadBp")) if cds is not None and isinstance(cds["output"], dict) else None
    spread_doc = m_value(round(spread, 1), cds["as_of"], "price v0.1", source_name="Marden bond-implied CDS") \
        if spread is not None else _nd(day, "M")
    if spread is None:
        missing.append(f"entity {eid}: bond-implied CDS (price engine 'cds' output)")
    cover, cmiss = ai_coverage(conn, eid, ent["kind"], day, edges)
    missing += cmiss
    has_fin = fin.debt() is not None or fin.ttm("operating_cash_flow") is not None
    ocf = fin.ttm("operating_cash_flow")
    band, reasons = downgrade_band(rs, p.leverage_today, p.leverage_peak, p.gap_3y,
                                   ocf.usd / 1e9 if ocf else None, has_fin, day)
    missing.append("agency-stated downgrade triggers (agency releases not collected): headroom and breach year n/d")
    if not rs:
        missing.append(f"entity {eid}: agency ratings (no rating fact public on {day})")
    return {
        "slug": eid, "name": ent["name"],
        "kind": KIND_LABELS.get(ent["kind"], ent["kind"].replace("_", " ").capitalize()),
        "ratings": rating_states(rs, day),
        "spreadBp": spread_doc,
        "leaseAdjLeverageToday": _m(p.leverage_today, day),
        "leaseAdjLeveragePeak": _m(p.leverage_peak, day),
        "triggerHeadroomTurns": _nd(day, "M"),
        "breachYear": _nd(day, "M"),
        "fundingGap3yB": _m(p.gap_3y, day),
        "aiRevenueCoverage": _m(cover, day),
        "topCounterparty": top_counterparty(conn, eid, day, edges, fin.backlog(), names),
        "downgradeRisk": _m(band, day),
        "years": p.years,
        "exposed": exposed(conn, eid, edges, day),
        "missing": sorted(set(missing)),
        "basis": {"methodVersion": f"{METHOD} (model v0)", "bandReasons": reasons, **p.basis,
                  "ratingsFrom": sorted({f"{r.row['subject_type']}:{r.row['subject_id']}" for r in rs.values()})},
    }


def compute(conn: Conn, day: date, ids: list[str] | None = None) -> Batch:
    with building(day):
        targets = [i for i in payer_ids(conn) if not ids or i in ids]
        edges = graph.load_edges(conn, day)
        fins = load_financials(conn, day, targets)
        ents = {r["id"]: r for r in conn.execute("SELECT id, name, kind FROM entity WHERE id = ANY(%s)",
                                                 (targets,)).fetchall()}
        names = graph.entity_names(conn)
        batch = Batch()
        for eid in targets:
            if eid in ents:
                batch.add("entity", eid, payer_doc(conn, eid, day, edges, fins[eid], ents[eid], names))
        batch.views.add(("payers", "all"))
        batch.views.add(("leases", "all"))
        batch.inputs = [targets, [e.id for e in edges]]
        return batch


def run(as_of: date | None = None, ids: list[str] | None = None, *, trigger: str = "manual",
        mark: bool = True) -> dict[str, Any]:
    return run_outputs(MODEL, VERSION, as_of, lambda c, d: compute(c, d, ids), trigger=trigger, mark=mark)


def _run_keys(keys: list[str], trigger: str) -> dict[str, Any]:
    r = run(ids=None if "*" in keys else keys, trigger=trigger)
    return {"payers": len(r["outputs"]), "changed": len(r["changed"]), "run_id": r["run_id"]}


def _install_triggers() -> None:
    for fld in ENTITY_FIELDS:
        triggers.model_depends(MODEL, "entity", fld)
    triggers.model_depends(MODEL, "entity", "@model:cds")
    for fld in ("rating", "agreement_term", "covenant_test", "amount_usd"):
        triggers.model_depends(MODEL, "instrument", fld, triggers.everything)
    triggers.model_depends(MODEL, "site", "tenant_rating", triggers.everything)
    for fld in ("edge_type", "total_usd", "annual_usd", "start", "term_years", "linked_instruments", "status"):
        triggers.model_depends(MODEL, "obligation", fld, triggers.everything)
    triggers.register_runner(MODEL, _run_keys)


_install_triggers()


@app.periodic(cron=CRON_UTC, periodic_id="payer_capacity_daily")
@app.task(name="models.payer_capacity", queue="models", queueing_lock="models.payer_capacity",
          lock="models.payer_capacity")
def payer_capacity_task(timestamp: int | None = None) -> dict[str, Any]:
    return _run_keys(["*"], "schedule")


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as-of", type=date.fromisoformat, help="YYYY-MM-DD (default today, US Eastern)")
    p.add_argument("--entity", action="append", dest="entities", help="entity id (repeatable)")
    p.add_argument("--no-mark", action="store_true", help="do not mark read models dirty")
    p.add_argument("--json", action="store_true")


@cli.action("model", MODEL, help="Payer capacity and downgrade risk (spec 24)", configure=_args)
def payer_capacity_cli(args: argparse.Namespace) -> int:
    r = run(args.as_of, args.entities, trigger="manual", mark=not args.no_mark)
    if args.json:
        print(json.dumps(r["outputs"], indent=2, ensure_ascii=False))
        return 0
    print(f"payer_capacity {VERSION} as of {r['as_of']} (run {r['run_id']}): {len(r['outputs'])} payers")
    for eid, o in r["outputs"].items():
        rs = "/".join(f"{x['agency']} {x['rating']['value']}" for x in o["ratings"]) or "n/d"
        print(f"  {eid:28} {o['downgradeRisk']['value']:5} lev {o['leaseAdjLeverageToday']['value']} -> "
              f"{o['leaseAdjLeveragePeak']['value']} gap3y {o['fundingGap3yB']['value']} spread "
              f"{o['spreadBp']['value']} {rs}; {'; '.join(o['basis']['bandReasons'])}")
    return 0
