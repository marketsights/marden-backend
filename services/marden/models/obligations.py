"""Models `obligations` and `obligation_company` (spec 18): the obligation graph and each company's side of it.

    python -m marden model obligations [--as-of YYYY-MM-DD] [--json]
    python -m marden model obligation_company [--as-of YYYY-MM-DD] [--entity ID ...] [--json]

`obligations` writes, under one run:
- subject ('series', 'all') = ObligationGraph {asOf, nodes, edges, kpis} (types.ts), every edge on the map
  on the as-of day (marden.models.graph), largest disclosed total first;
- subject ('obligation', <id>) = that ObligationEdge, whose `annual` schedule the obligations read model
  shows (graph.Edge.schedule: the filed or reported annual amount, else total / term from the start year,
  tag M 'obligations v0.1').
`obligation_company` writes subject ('entity', <id>) = CompanyObligations {node, owes, isOwed, owesByYear,
concentration, fundingGap} for every company at either end of an edge:
- concentration: each payer's share of the company's backlog (its edge total / the company's filed
  remaining performance obligations) when the backlog is filed, else of the incoming totals Marden maps
  (the basis says which), tag M;
- fundingGap per year (this year and the next four): committed = the company's outgoing payments that
  year (the edges' schedules); funded = trailing-twelve-month operating cash flow (SEC XBRL, held flat:
  no guidance is collected) plus cash on hand in the first year; gapB = committed - funded (tag M).
  A company without filed cash flow (OpenAI, Anthropic) has no funding-gap rows and says so.
Every output also carries `missing` and `basis`. KPIs: totalB sums the edges' totals; filedPct is the
share of those dollars whose total is filed (tag F); changedThisMonth counts edges with a fact dated this
month. Node status is the party's own worst link in the status model's outputs (graph.party_status).

Runs: daily 09:25 UTC and within a minute of a published edge fact, a company's funding facts or XBRL
cash flow (marden.models.triggers); a past --as-of is a backtest (inputs as public that day).
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date
from typing import Any

from marden import cli
from marden.core.db import Conn
from marden.core.queue import app
from marden.models import graph, triggers
from marden.models.credit_inputs import FIN_FIELDS, Financials, load_financials
from marden.models.sourced import Batch, m_value, method_version, run_outputs
from marden.readmodels.context import building
from marden.readmodels.views.payers_common import EDGE_FIELDS, EDGE_ROW_FIELDS

MODEL, COMPANY_MODEL, VERSION = "obligations", "obligation_company", "0.1"
METHOD, COMPANY_METHOD = method_version(MODEL, VERSION), method_version(COMPANY_MODEL, VERSION)
CRON_UTC = "25 9 * * *"
GAP_YEARS = 5


def _m(value: Any, day: date, method: str = COMPANY_METHOD) -> dict[str, Any]:
    return m_value(value, day, method, source_name="Marden obligation map")


def graph_doc(conn: Conn, day: date, edges: list[graph.Edge]) -> dict[str, Any]:
    names, kinds, sites = graph.entity_names(conn), graph.entity_kinds(conn), graph.known_sites(conn)
    edges = sorted(edges, key=lambda e: (-(e.total.usd if e.total else 0.0), e.payer, e.payee, e.type))
    ids = sorted({e.payer for e in edges} | {e.payee for e in edges})
    nodes = [graph.node_doc(conn, i, edges, day, names, kinds) for i in ids]
    nodes.sort(key=lambda n: (-(n["owedB"] + n["owesB"]), n["name"]))
    total = sum(e.total.usd for e in edges if e.total) / 1e9
    filed = sum(e.total.usd for e in edges if e.total and e.total.tag == "F") / 1e9
    month = day.replace(day=1)
    changed = {e.id for e in edges for rows in ([*e.facts.values()], *e.rows.values())
               for r in rows if month <= r["valid_from"] <= day}
    return {
        "asOf": day.isoformat(),
        "nodes": nodes,
        "edges": [e.doc(day, sites) for e in edges],
        "kpis": {"totalB": round(total, 3), "filedPct": round(100 * filed / total, 1) if total else 0.0,
                 "changedThisMonth": len(changed)},
        "missing": sorted({m for e in edges for m in e.missing}),
        "basis": {"methodVersion": METHOD, "edges": len(edges),
                  "amounts": "total_usd, else the payer's funding_fact naming the payee; annual = annual_usd, "
                             "else total / term from the start year (tag M)"},
    }


def _concentration(eid: str, incoming: list[graph.Edge], fin: Financials, day: date) -> tuple[list, dict]:
    backlog = fin.backlog()
    sized = [e for e in incoming if e.total is not None and e.pays]
    base = backlog.usd if backlog else sum(e.total.usd for e in sized)  # type: ignore[union-attr]
    rows: dict[str, float] = defaultdict(float)
    for e in sized:
        rows[e.payer] += e.total.usd  # type: ignore[union-attr]
    out = [{"payerId": p, "share": _m(round(min(v / base, 1.0), 4), day)} for p, v in sorted(rows.items()) if base]
    out.sort(key=lambda r: -r["share"]["value"])
    basis = {"denominator": "filed remaining performance obligations" if backlog else "incoming totals mapped",
             "denominatorB": round(base / 1e9, 3) if base else None,
             "backlogAsOf": backlog.as_of.isoformat() if backlog else None}
    return out, basis


def _funding_gap(owes: list[graph.Edge], fin: Financials, day: date) -> tuple[list, list[str], dict]:
    ocf, cash = fin.ttm("operating_cash_flow"), fin.instant("cash_usd")
    missing = []
    if ocf is None:
        missing.append(f"entity {fin.entity_id}: operating cash flow (SEC XBRL; private companies file none)")
    rows = []
    for year in range(day.year, day.year + GAP_YEARS):
        committed = sum(u for e in owes if (u := e.annual_usd(year)) is not None)
        if not committed or ocf is None:
            continue
        funded = ocf.usd + (cash.usd if cash is not None and year == day.year else 0.0)
        rows.append({"year": year, "committedB": round(committed / 1e9, 3), "fundedB": round(funded / 1e9, 3),
                     "gapB": _m(round((committed - funded) / 1e9, 3), day)})
    basis = {"funded": "trailing-twelve-month operating cash flow held flat (no guidance collected) + cash on "
                       "hand in the first year", "ocf": ocf.basis if ocf else None,
             "ocfB": round(ocf.usd / 1e9, 3) if ocf else None, "cashB": round(cash.usd / 1e9, 3) if cash else None}
    return rows, missing, basis


def company_doc(conn: Conn, eid: str, edges: list[graph.Edge], fin: Financials, day: date,
                ctx: dict[str, Any]) -> dict[str, Any]:
    mine = [e for e in edges if eid in (e.payer, e.payee)]
    owes = sorted([e for e in mine if e.payer == eid], key=lambda e: -(e.total.usd if e.total else 0.0))
    owed = sorted([e for e in mine if e.payee == eid], key=lambda e: -(e.total.usd if e.total else 0.0))
    by_year: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for e in owes:
        for year, usd, _ in e.schedule():
            by_year[year][e.payee] += usd
    concentration, c_basis = _concentration(eid, owed, fin, day)
    gap, g_missing, g_basis = _funding_gap(owes, fin, day)
    return {
        "node": graph.node_doc(conn, eid, mine, day, ctx["names"], ctx["kinds"]),
        "owes": [e.doc(day, ctx["sites"]) for e in owes],
        "isOwed": [e.doc(day, ctx["sites"]) for e in owed],
        "owesByYear": [{"year": y, "byCounterparty": [{"id": c, "amountB": round(v / 1e9, 3)}
                                                      for c, v in sorted(parts.items())]}
                       for y, parts in sorted(by_year.items())],
        "concentration": concentration,
        "fundingGap": gap,
        "missing": sorted({m for e in mine for m in e.missing} | set(g_missing)),
        "basis": {"methodVersion": COMPANY_METHOD, "concentration": c_basis, "fundingGap": g_basis},
    }


def compute_graph(conn: Conn, day: date) -> Batch:
    with building(day):
        edges = graph.load_edges(conn, day)
        batch = Batch()
        doc = graph_doc(conn, day, edges)
        batch.add("series", "all", doc)
        for e, edoc in zip(sorted(edges, key=lambda e: (-(e.total.usd if e.total else 0.0), e.payer, e.payee,
                                                        e.type)), doc["edges"], strict=True):
            batch.add("obligation", e.id, {**edoc, "missing": e.missing, "basis": {"methodVersion": METHOD}})
        batch.views.add(("obligations", "all"))
        batch.inputs = [[e.id for e in edges], doc["kpis"]]
        return batch


def compute_companies(conn: Conn, day: date, ids: list[str] | None = None) -> Batch:
    with building(day):
        edges = graph.load_edges(conn, day)
        targets = sorted({e.payer for e in edges} | {e.payee for e in edges})
        if ids:
            targets = [t for t in targets if t in ids]
        fins = load_financials(conn, day, targets)
        ctx = {"names": graph.entity_names(conn), "kinds": graph.entity_kinds(conn), "sites": graph.known_sites(conn)}
        batch = Batch()
        for eid in targets:
            batch.add("entity", eid, company_doc(conn, eid, edges, fins[eid], day, ctx))
        batch.inputs = [targets, [e.id for e in edges]]
        return batch


def run(as_of: date | None = None, *, trigger: str = "manual", mark: bool = True) -> dict[str, Any]:
    return run_outputs(MODEL, VERSION, as_of, compute_graph, trigger=trigger, mark=mark)


def run_companies(as_of: date | None = None, ids: list[str] | None = None, *, trigger: str = "manual",
                  mark: bool = True) -> dict[str, Any]:
    return run_outputs(COMPANY_MODEL, VERSION, as_of, lambda c, d: compute_companies(c, d, ids), trigger=trigger,
                       mark=mark)


def _run_graph_keys(keys: list[str], trigger: str) -> dict[str, Any]:
    r = run(trigger=trigger)
    return {"edges": len(r["outputs"]) - 1, "changed": len(r["changed"]), "run_id": r["run_id"]}


def _run_company_keys(keys: list[str], trigger: str) -> dict[str, Any]:
    r = run_companies(trigger=trigger)
    return {"companies": len(r["outputs"]), "changed": len(r["changed"]), "run_id": r["run_id"]}


def _install_triggers() -> None:
    for model in (MODEL, COMPANY_MODEL):
        for fld in (*EDGE_FIELDS, *EDGE_ROW_FIELDS):
            triggers.model_depends(model, "obligation", fld, triggers.everything)
        triggers.model_depends(model, "entity", "funding_fact", triggers.everything)
        triggers.model_depends(model, "site", "@model:status", triggers.everything)  # node status
    for fld in FIN_FIELDS:
        triggers.model_depends(COMPANY_MODEL, "entity", fld, triggers.everything)
    triggers.register_runner(MODEL, _run_graph_keys)
    triggers.register_runner(COMPANY_MODEL, _run_company_keys)


_install_triggers()


@app.periodic(cron=CRON_UTC, periodic_id="obligations_daily")
@app.task(name="models.obligations", queue="models", queueing_lock="models.obligations", lock="models.obligations")
def obligations_task(timestamp: int | None = None) -> dict[str, Any]:
    return {"graph": _run_graph_keys(["*"], "schedule"), "companies": _run_company_keys(["*"], "schedule")}


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as-of", type=date.fromisoformat, help="YYYY-MM-DD (default today, US Eastern)")
    p.add_argument("--no-mark", action="store_true", help="do not mark read models dirty")
    p.add_argument("--json", action="store_true", help="print the outputs as JSON")


def _company_args(p: argparse.ArgumentParser) -> None:
    _args(p)
    p.add_argument("--entity", action="append", dest="entities", help="entity id (repeatable)")


@cli.action("model", MODEL, help="Obligation graph (ObligationGraph + annual schedules per edge)", configure=_args)
def graph_cli(args: argparse.Namespace) -> int:
    r = run(args.as_of, trigger="manual", mark=not args.no_mark)
    doc = r["outputs"]["all"]
    if args.json:
        print(json.dumps(doc, indent=2, ensure_ascii=False))
        return 0
    k = doc["kpis"]
    print(f"obligations {VERSION} as of {r['as_of']} (run {r['run_id']}): {len(doc['edges'])} edges, "
          f"{len(doc['nodes'])} nodes, ${k['totalB']}B mapped, {k['filedPct']}% filed")
    for e in doc["edges"][:15]:
        sched = ", ".join(f"{a['year']}: {a['amountB']['value']}" for a in e["annual"][:4])
        print(f"  {e['id']:62} {e['totalB']['value'] or 'n/d':>8} {sched}")
    return 0


@cli.action("model", COMPANY_MODEL, help="Each company's obligations, concentration and funding gap",
            configure=_company_args)
def company_cli(args: argparse.Namespace) -> int:
    r = run_companies(args.as_of, args.entities, trigger="manual", mark=not args.no_mark)
    if args.json:
        print(json.dumps(r["outputs"], indent=2, ensure_ascii=False))
        return 0
    print(f"obligation_company {VERSION} as of {r['as_of']} (run {r['run_id']}): {len(r['outputs'])} companies")
    for eid, o in r["outputs"].items():
        top = o["concentration"][0] if o["concentration"] else None
        gap = o["fundingGap"][0]["gapB"]["value"] if o["fundingGap"] else None
        print(f"  {eid:40} owes {len(o['owes']):2} owed {len(o['isOwed']):2} owesB {o['node']['owesB']:8} "
              f"top {top['payerId'] + ' ' + str(top['share']['value']) if top else '-':30} gap {gap}")
    return 0
