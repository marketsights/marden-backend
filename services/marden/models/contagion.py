"""Contagion over the obligation graph (spec 18 "Contagion stress"): "company X pays only N% of its
obligations", walked three hops. Output: ContagionResult (types.ts) plus `missing` and `basis`.

1. First order: every payee of X's payment edges (graph.PAYMENT_TYPES: leases, compute and chip purchases,
   power purchases, loans, revenue shares) loses (1 - N%) of the edge's next annual payment (the first
   scheduled year from this year on, marden.models.graph); an edge with no schedule is listed as missing,
   never sized by guesswork.
2. Each payee's cover: its trailing-twelve-month operating cash flow (SEC XBRL) against its own next-year
   payments on the map. coverageAfter = (cash flow - loss) / payments. A payee whose loss exceeds its
   spare cash flow (cash flow - payments, floored at zero) pays only the share it can still cover,
   1 - (loss - spare) / payments, on its own edges: the next hop. A payee without filed cash flow or
   without mapped payments cannot be judged (coverageAfter null) and passes nothing on.
3. Up to three hops; a company is hit once (the first hop that reaches it).
4. Instruments, two ways (the lower price wins):
   - cash cut: the debt behind each edge whose payer pays less than in full (graph.Edge.instruments) and
     the notes of every such company (X included) are worth f x their price now (f = the share still
     paid), floored at the low end of their collateral recovery (recovery model);
   - weaker credit: each hit company's lost contract value (its cut share of the remaining schedule,
     PV at the 10-year Treasury) comes off its equity value; CreditGrades turns that into an intensity
     multiplier (stress v0.1) and the price engine re-prices its notes and the site debt it is tenant of
     (stress_price.Book; only when the caller passes the day's book).
   priceNow is the price engine's price on the day; holders come from the mark-gap model.
totalAtRiskB = the annual payments lost over all hops. Severity does not change the walk (there is no
market-spread leg); it is recorded.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from marden.core.db import Conn
from marden.models import graph, stress_inputs
from marden.models.credit_inputs import Financials, load_financials

HOPS = 3
METHOD = "contagion v0.1"


def next_annual(edge: graph.Edge, day: date) -> float | None:
    sched = [(y, usd) for y, usd, _ in edge.schedule() if y >= day.year]
    return min(sched)[1] if sched else None


def _payments(edges: list[graph.Edge], eid: str, day: date) -> tuple[float, list[str]]:
    total, unknown = 0.0, []
    for e in edges:
        if e.payer == eid and e.pays:
            amt = next_annual(e, day)
            if amt is None:
                unknown.append(e.id)
            else:
                total += amt
    return total, unknown


def walk(
    edges: list[graph.Edge], fins: dict[str, Financials], company: str, pay_share: float, day: date
) -> tuple[list[dict[str, Any]], dict[str, float], dict[str, float], list[str]]:
    """Returns (hops, pay share per company that pays less than in full, cut share per edge id, missing)."""
    pay = {company: pay_share}
    cut: dict[str, float] = {}
    hit = {company}
    frontier = {company}
    hops: list[dict[str, Any]] = []
    missing: list[str] = []
    for hop in range(1, HOPS + 1):
        losses: dict[str, float] = defaultdict(float)
        for payer in sorted(frontier):
            f = pay[payer]
            for e in edges:
                if e.payer != payer or not e.pays:
                    continue
                amt = next_annual(e, day)
                if amt is None:
                    missing.append(f"hop {hop}: obligation {e.id} has no annual schedule ({', '.join(e.missing)})")
                    continue
                cut[e.id] = 1.0 - f
                if e.payee not in hit:
                    losses[e.payee] += (1.0 - f) * amt
        companies, nxt = [], set()
        for payee, loss in sorted(losses.items(), key=lambda kv: -kv[1]):
            if loss <= 0:
                continue
            fin = fins.get(payee)
            ocf = fin.ttm("operating_cash_flow") if fin else None
            payments, unknown = _payments(edges, payee, day)
            coverage = None
            if ocf is not None and payments > 0:
                coverage = round((ocf.usd - loss) / payments, 3)
                spare = max(0.0, ocf.usd - payments)
                if loss > spare:
                    pay[payee] = max(0.0, 1.0 - (loss - spare) / payments)
                    nxt.add(payee)
            elif ocf is None:
                missing.append(f"hop {hop}: {payee} has no filed operating cash flow (cover not judged)")
            elif unknown:
                missing.append(f"hop {hop}: {payee}'s own payments are not sized ({', '.join(unknown[:3])})")
            companies.append({"id": payee, "lossB": round(loss / 1e9, 3), "coverageAfter": coverage})
            hit.add(payee)
        if companies:
            hops.append({"hop": hop, "companies": companies})
        frontier = nxt
        if not frontier:
            break
    return hops, pay, cut, sorted(set(missing))


def _reprice_hits(
    conn: Conn, day: date, book: Any, edges: list[graph.Edge], cut: dict[str, float], hit: list[str], company: str
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], list[str]]:
    """Price changes from the hit companies' weaker credit: each one's lost contract value (cut share x
    the remaining schedule, PV) off its equity value, CreditGrades -> intensity multiplier (stress v0.1),
    re-priced by the price engine (its own notes and the site debt it is tenant of)."""
    from marden.models.stress_price import Shock
    from marden.models.stress_scenarios import _pv, _sites_of, equity_multiplier

    rate = book.ctx.book.curve(day).par_yield(10.0) / 100.0
    pv: dict[str, float] = defaultdict(float)
    for e in edges:
        if cut.get(e.id, 0.0) > 0:
            got, _ = _pv(e, day, rate)
            if got is not None:
                pv[e.payee] += cut[e.id] * got
    shock, basis, missing = Shock(), {}, []
    for payee in hit:
        if payee == company or payee not in pv:
            continue
        mult, info, miss = equity_multiplier(conn, book, payee, pv[payee])
        missing += miss
        if mult is not None:
            shock.hazard_mult[payee] = mult
            basis[payee] = {**info, "hazardMultiplier": round(mult, 4)}
    ids: set[str] = set()
    for payee in shock.hazard_mult:
        ids.update(
            r["id"]
            for r in conn.execute(
                "SELECT id FROM instrument WHERE site_slug = ANY(%s)", (_sites_of(conn, payee, ("tenant",)),)
            ).fetchall()
        )
    ids.update(i for v in stress_inputs.issued_by(conn, list(shock.hazard_mult)).values() for i in v)
    out = {}
    for iid in sorted(ids & set(book.insts)):
        rp = book.reprice(iid, shock)
        if rp["now"] is not None and rp["after"] is not None:
            out[iid] = rp
    return out, basis, missing


def contagion(
    conn: Conn, day: date, company: str, pay_pct: float, severity: str = "base", book: Any = None
) -> dict[str, Any]:
    edges = graph.load_edges(conn, day)
    ids = sorted({e.payer for e in edges} | {e.payee for e in edges} | {company})
    fins = load_financials(conn, day, ids)
    known = company in {e.payer for e in edges}
    hops, pay, cut, missing = walk(edges, fins, company, max(0.0, min(pay_pct, 100.0)) / 100.0, day)
    if not known:
        missing.insert(0, f"{company}: no payment edge on the obligation map on {day}")
    share: dict[str, float] = {}
    for e in edges:
        if e.id in cut and cut[e.id] > 0 and pay.get(e.payer, 1.0) < 1.0:
            for iid in e.instruments():
                share[iid] = min(share.get(iid, 1.0), 1.0 - cut[e.id])
    notes = stress_inputs.issued_by(conn, [c for c, f in pay.items() if f < 1.0])
    for c, iids in notes.items():
        for iid in iids:
            share[iid] = min(share.get(iid, 1.0), pay[c])
    hit = [c["id"] for h in hops for c in h["companies"]]
    repriced, credit, miss = _reprice_hits(conn, day, book, edges, cut, hit, company) if book else ({}, {}, [])
    missing += miss
    insts = stress_inputs.load(conn, day, [*share, *repriced])
    rows = []
    for iid in sorted(set(share) | set(repriced)):
        inst = insts.get(iid)
        if inst is None:
            continue
        now = repriced[iid]["now"] if iid in repriced else inst.price
        if now is None:  # listed as missing, not as a row (a row carries both prices)
            missing.append(f"instrument {iid}: price model output on or before {day}")
            continue
        afters = [repriced[iid]["after"]] if iid in repriced else []
        if iid in share:
            cut_after = now * share[iid]
            if inst.recovery_low is not None:
                cut_after = max(cut_after, min(inst.recovery_low, now))
            afters.append(cut_after)
        rows.append(
            {
                "instrumentId": iid,
                "name": inst.name,
                "priceNow": round(now, 3),
                "priceAfter": round(min(afters), 3),
                "holders": inst.holders_text(),
            }
        )
    rows = [r for r in rows if abs(r["priceAfter"] - r["priceNow"]) >= 0.001]
    rows.sort(key=lambda r: (r["priceAfter"] - r["priceNow"], r["instrumentId"]))
    total = sum(c["lossB"] for h in hops for c in h["companies"])
    unscheduled = any("has no annual schedule" in m for m in missing)
    if unscheduled:  # payments on unscheduled edges are unknown: none known is n/d, some known is a floor
        missing.append("totalAtRiskB: " + ("n/d (no edge on the walk has a payment schedule)" if not total else
                                           "scheduled payments only (edges with no schedule add to it)"))
    return {
        "hops": hops,
        "instruments": rows,
        "totalAtRiskB": None if unscheduled and not total else round(total, 3),
        "missing": sorted(set(missing)),
        "basis": {
            "methodVersion": f"{METHOD} (model v0)",
            "company": company,
            "payPct": pay_pct,
            "severity": severity,
            "paysLess": {c: round(f, 4) for c, f in sorted(pay.items())},
            "credit": credit,
            "loss": "next annual payment on each cut edge (graph schedule)",
            "priceAfter": "cash stream cut to share f: price now x f, floored at the recovery low; a hit "
            "company's weaker credit: the price engine at its shocked intensity (lower wins)",
        },
    }
