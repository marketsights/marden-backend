"""Model `leases` (spec 25): the off-balance-sheet lease map and its downgrade ranking.

    python -m marden model leases [--as-of YYYY-MM-DD] [--json]

One output, subject ('series', 'all') = LeaseMapPage (types.ts) + `missing`, `basis`:
- leases: one LeaseRecord per lease and compute contract on the obligation map (marden.models.graph):
  lessee and lessor are the edge's ends, amounts / start / term its facts (or the payer's funding fact),
  attribution Filed / Reported / Inferred from its sources, the site its single linked site, the campus
  the edge's own (an approved site link only), dependent debt the instruments behind the edge with their
  size, holders and whether their documents quote a rating-tied term;
- turnsAdded = the lease's committed total / the lessee's EBITDA (SEC XBRL, trailing twelve months; the
  total undiscounted, as payer_years: agencies add the present value, so v0 is an upper bound);
- lesseeBand: the payer_capacity model's downgradeRisk for the lessee;
- priorityScore (M v0; weights in basis) = P(cut | band) x dependent debt $B x (1.5 when that debt has a
  rating-linked term, else 1) x proximity (1 / (1 + years until commencement));
- headroomBeforeTurns and triggerLease need each agency's stated downgrade trigger, which is not collected
  yet: headroom is n/d and no lease is flagged as a trigger lease (the `missing` list says so);
- reconciliation: one row per lessee only once its latest 10-K/10-Q "leases not yet commenced" total is a
  fact (lease notes are read by the extraction agents; us-gaap has no non-dimensional element for it).
  Until then there are no rows: the residual is never estimated, and the totals cover the mapped leases
  only (disclosedB and unattributedB 0, attributedShare 0). Inferred leases (spec 25 "Backing into the
  deals") come from the researcher agent's trail; v0 builds none.

Runs: daily 09:33 UTC (after payer_capacity) and within a minute of a lease edge's facts or a lessee's
payer_capacity output. A past --as-of is a backtest.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from typing import Any

from marden import cli
from marden.core.db import Conn
from marden.core.queue import app
from marden.models import graph, pit, stress_inputs, triggers
from marden.models.credit_inputs import load_financials
from marden.models.runs import latest_output
from marden.models.sourced import Batch, fact_value, m_value, method_version, run_outputs
from marden.readmodels.context import building
from marden.readmodels.views.payers_common import evidence_of, number_of, text_of, with_value

MODEL, VERSION = "leases", "0.1"
METHOD = method_version(MODEL, VERSION)
CRON_UTC = "33 9 * * *"
LEASE_TYPES = ("Lease", "Compute purchase")
STATUS_LABELS = {
    "Started": "Commenced",
    "Signed": "Signed, not commenced",
    "Announced": "Announced",
    "Amended": "Amended",
    "Terminated": "Terminated",
    "Disputed": "Disputed",
}
NOT_OUTSTANDING = ("Commenced", "Terminated")
P_CUT = {"High": 0.6, "Watch": 0.3, "Low": 0.1}  # v0 weights, uncalibrated
RATING_LINK_WEIGHT = 1.5
_RATING_TERM = re.compile(r"rating|investment[- ]grade|\bIG\b|\bBBB|\bBaa", re.I)


def _m(value: Any, day: date) -> dict[str, Any]:
    return m_value(value, day, METHOD, source_name="Marden lease map", uncalibrated=True)


def _rating_linked(conn: Conn, day: date, ids: list[str]) -> set[str]:
    out = set()
    for r in pit.fact_rows(conn, day, "instrument", ids, ["agreement_term", "covenant_test"]):
        v = r["value"] if isinstance(r["value"], dict) else {}
        text = f"{v.get('type') or ''} {v.get('threshold') or ''} {v.get('metric') or ''}"
        if _RATING_TERM.search(text):
            out.add(r["subject_id"])
    return out


def _attribution(edge: graph.Edge) -> str:
    tags = {r["tag"].strip() for r in edge.rows["evidence"]}
    return "Filed" if "F" in tags else "Reported" if "R" in tags else "Inferred"


def _campus(conn: Conn, edge: graph.Edge, site: str | None) -> str | None:
    if edge.ob.get("campus_id"):
        return edge.ob["campus_id"]
    if site is None:
        return None
    rows = conn.execute(
        "SELECT campus_id FROM site_link WHERE site_slug = %s AND status = 'approved'", (site,)
    ).fetchall()
    return rows[0]["campus_id"] if len(rows) == 1 else None


def lease_record(conn: Conn, edge: graph.Edge, day: date, ctx: dict[str, Any]) -> dict[str, Any]:
    doc = edge.doc(day, ctx["sites"])
    sites = doc["linkedSites"]
    site = sites[0] if len(sites) == 1 else None
    insts = ctx["insts"]
    debt = [
        {
            "instrumentId": i,
            "name": insts[i].name,
            "amountB": insts[i].size_b(day),
            "ratingLinked": i in ctx["linked"],
            "holdersCount": insts[i].holder_count,
        }
        for i in doc["linkedInstruments"]
        if i in insts
    ]
    debt.sort(key=lambda d: (-(d["amountB"]["value"] or 0), d["name"]))
    band_out = latest_output(conn, "payer_capacity", "entity", edge.payer, day)
    band = band_out["output"].get("downgradeRisk") if band_out and isinstance(band_out["output"], dict) else None
    band = (
        band
        if isinstance(band, dict) and band.get("value") in ("Low", "Watch", "High", "n/d")
        else fact_value(None, as_of=day, tag="M")
    )
    fin = ctx["fins"].get(edge.payer)
    ebitda = fin.ebitda() if fin else None
    turns = round(edge.total.usd / ebitda.usd, 2) if edge.total and ebitda and ebitda.usd > 0 else None
    debt_b = sum(d["amountB"]["value"] or 0 for d in debt)
    p = P_CUT.get(band.get("value"))
    years_out = max(0, (edge.start or day.year) - day.year)
    score = None
    if p is not None:
        weight = RATING_LINK_WEIGHT if any(d["ratingLinked"] for d in debt) else 1.0
        score = round(p * debt_b * weight / (1 + years_out), 3)
    sched = edge.schedule()
    annual = (
        edge.annual.sourced_b()
        if edge.annual
        else _m(round(sched[0][1] / 1e9, 3), day)
        if sched
        else fact_value(None, as_of=day)
    )
    attribution = _attribution(edge)
    return {
        "id": edge.id,
        "lessee": ctx["names"].get(edge.payer, edge.payer),
        "lessor": with_value(edge.facts["edge_type"], ctx["names"].get(edge.payee, edge.payee)),
        "siteSlug": site,
        "campusId": _campus(conn, edge, site),
        "kind": "Compute contract" if edge.type == "Compute purchase" else "Lease",
        "status": STATUS_LABELS.get(edge.status, edge.status),
        "totalB": doc["totalB"],
        "annualB": annual,
        "commencement": text_of(edge.facts.get("start")),
        "termYears": number_of(edge.facts.get("term_years")),
        "attribution": attribution,
        "confidence": "Low" if attribution == "Inferred" else None,
        "turnsAdded": _m(turns, day),
        "headroomBeforeTurns": fact_value(None, as_of=day, tag="M"),
        "triggerLease": False,
        "lesseeBand": band,
        "priorityScore": _m(score, day),
        "dependentDebt": debt,
        "evidence": sorted((evidence_of(r) for r in edge.rows["evidence"]), key=lambda e: e["date"], reverse=True),
    }


def compute(conn: Conn, day: date) -> Batch:
    with building(day):
        edges = [e for e in graph.load_edges(conn, day) if e.type in LEASE_TYPES]
        ids = sorted({i for e in edges for i in e.instruments()})
        ctx = {
            "names": graph.entity_names(conn),
            "sites": graph.known_sites(conn),
            "insts": stress_inputs.load(conn, day, ids),
            "linked": _rating_linked(conn, day, ids),
            "fins": load_financials(conn, day, sorted({e.payer for e in edges})),
        }
        leases = [lease_record(conn, e, day, ctx) for e in edges]
        leases.sort(
            key=lambda r: (
                r["priorityScore"]["value"] is None,
                -(r["priorityScore"]["value"] or 0),
                -(r["totalB"]["value"] or 0),
                r["lessee"],
                r["id"],
            )
        )
        outstanding = [r for r in leases if r["status"] not in NOT_OUTSTANDING]
        debt: dict[str, float] = {}
        for r in leases:
            for d in r["dependentDebt"]:
                debt.setdefault(d["instrumentId"], d["amountB"]["value"] or 0.0)
        lessees = sorted({r["lessee"] for r in leases})
        batch = Batch()
        batch.add(
            "series",
            "all",
            {
                "asOf": day.isoformat(),
                "totals": {  # n/d (null) until the 10-K lease notes and agency triggers are collected, never 0
                    "disclosedB": None,
                    "attributedB": round(sum(r["totalB"]["value"] or 0 for r in outstanding), 3),
                    "unattributedB": None,
                    "attributedShare": None,
                    "triggerLeases": None,
                    "debtExposedB": round(sum(debt.values()), 3),
                },
                "reconciliation": [],
                "leases": leases,
                "missing": sorted(
                    {
                        *(m for e in edges for m in e.missing),
                        *(
                            f"{x}: latest 10-K/10-Q 'leases not yet commenced' total (lease note; extraction)"
                            for x in lessees
                        ),
                        "agency-stated downgrade triggers: headroom n/d, no trigger leases flagged",
                        *(
                            [f"{n} outstanding leases with total n/d: left out of attributedB"]
                            if (n := sum(1 for r in outstanding if r["totalB"]["value"] is None)) else []
                        ),
                        *(
                            [f"{n} dependent instruments with size n/d: left out of debtExposedB and priority scores"]
                            if (n := sum(1 for r in leases for d in r["dependentDebt"]
                                         if d["amountB"]["value"] is None))
                            else []
                        ),
                        "inferred leases (researcher trail): none built in v0",
                    }
                ),
                "basis": {
                    "methodVersion": f"{METHOD} (model v0)",
                    "pCut": P_CUT,
                    "ratingLinkWeight": RATING_LINK_WEIGHT,
                    "proximity": "1 / (1 + years until commencement)",
                    "turnsAdded": "edge total (undiscounted) / lessee EBITDA (SEC XBRL LTM)",
                },
            },
        )
        batch.views.add(("leases", "all"))
        batch.inputs = [[r["id"] for r in leases]]
        return batch


def run(as_of: date | None = None, *, trigger: str = "manual", mark: bool = True) -> dict[str, Any]:
    return run_outputs(MODEL, VERSION, as_of, compute, trigger=trigger, mark=mark)


def _run_keys(keys: list[str], trigger: str) -> dict[str, Any]:
    r = run(trigger=trigger)
    return {"leases": len(r["outputs"]["all"]["leases"]), "run_id": r["run_id"]}


def _install_triggers() -> None:
    for fld in (
        "edge_type",
        "evidence",
        "total_usd",
        "annual_usd",
        "start",
        "term_years",
        "status",
        "linked_sites",
        "linked_instruments",
    ):
        triggers.model_depends(MODEL, "obligation", fld, triggers.everything)
    triggers.model_depends(MODEL, "entity", "@model:payer_capacity", triggers.everything)
    triggers.model_depends(MODEL, "site", "site_link", triggers.everything)  # a lease's campus (_campus)
    triggers.register_runner(MODEL, _run_keys)


_install_triggers()


@app.periodic(cron=CRON_UTC, periodic_id="leases_daily")
@app.task(name="models.leases", queue="models", queueing_lock="models.leases", lock="models.leases")
def leases_task(timestamp: int | None = None) -> dict[str, Any]:
    return _run_keys(["*"], "schedule")


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as-of", type=date.fromisoformat, help="YYYY-MM-DD (default today, US Eastern)")
    p.add_argument("--no-mark", action="store_true", help="do not mark read models dirty")
    p.add_argument("--json", action="store_true")


@cli.action("model", MODEL, help="Off-balance-sheet lease map and downgrade ranking (spec 25)", configure=_args)
def leases_cli(args: argparse.Namespace) -> int:
    r = run(args.as_of, trigger="manual", mark=not args.no_mark)
    doc = r["outputs"]["all"]
    if args.json:
        print(json.dumps(doc, indent=2, ensure_ascii=False))
        return 0
    print(
        f"leases {VERSION} as of {r['as_of']} (run {r['run_id']}): {len(doc['leases'])} leases; totals {doc['totals']}"
    )
    for x in doc["leases"][:15]:
        print(
            f"  {x['id']:56} {x['lesseeBand']['value'] or 'n/d':5} total {x['totalB']['value']} turns "
            f"{x['turnsAdded']['value']} debt {sum(d['amountB']['value'] or 0 for d in x['dependentDebt']):.2f} "
            f"score {x['priorityScore']['value']}"
        )
    return 0
