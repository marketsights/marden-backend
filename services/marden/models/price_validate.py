"""Validation of the price engine against docs/methods/pricing-v0.1.md "Results to reproduce" (grading only).

    cd services && uv run python -m marden model price_validate [--json PATH]

1. CoreWeave 5y CDS on the seven reported dates: the unfiltered bond-implied CDS of the bond cross-section
   nearest each date, as the methodology does ("matched to the nearest month-end bond prices, 2-15 days
   apart"): N-PORT month-end medians with at least 3 holders, or, where the methodology used market
   prints (2026-10-14/15), the TRACE prints of that day. The vendor levels below are the methodology's
   table; they are compared, never used as inputs.
2. CoreWeave leave-one-bond-out (Apr-Jul 2026): each N-PORT median of one CoreWeave note priced from the
   other notes' intensities of that day (interpolated to its maturity), against its own median.
3. Jupiter: the locked model path (SOFR+250 at par on 2025-11-07, 4-year term, Oracle tenant intensity
   point in time, lease protection 0.5, completion 2028-06-30, 1.5%/yr base, recovery 55%, liquidity
   premium solved at issue and held, the methodology's dated signal list) on the table's dates, and the
   same path with the signals Marden's records give (price_signals.from_records).
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import date, timedelta
from typing import Any

from marden import cli
from marden.core import db
from marden.core.db import Conn
from marden.models import price_marks, price_signals
from marden.models.price_bonds import Terms, clean_price, flows, solve_spread
from marden.models.price_cds import interpolate, isda
from marden.models.price_chain import Chain, Legs, hazard_fn
from marden.models.price_curve import load_curves, year_frac
from marden.models.price_issuer import build, raw_level, sections, straight_bonds
from marden.models.price_terms import attach, load_comps, load_instruments
from marden.readmodels.context import business_today

VENDOR_CRWV = (  # (reported date, vendor 5y CDS bp, methodology's Marden bp)
    (date(2026, 10, 15), 1194, 1019), (date(2026, 10, 14), 1050, 1176), (date(2026, 7, 29), 855, 736),
    (date(2026, 6, 15), 452, 436), (date(2025, 12, 12), 774, 765), (date(2025, 11, 20), 675, 766),
    (date(2025, 9, 26), 371, 432),
)
MARKET_PRINT_DATES = {date(2026, 10, 14), date(2026, 10, 15)}
MAX_MATCH_DAYS = 31
JUPITER_ID = "EX-JUPITER-190"
JUPITER_ISSUE = date(2025, 11, 7)
JUPITER_TERMS = Terms(JUPITER_ID, date(2029, 11, 7), "floating", None, 250.0, 4, ("locked: SOFR+250, 4-year term",))
JUPITER_COMPLETION = date(2028, 6, 30)
# Locked with the backtest (pricing-v0.1.md "Jupiter backtest"): the methodology's flat 55%. Price v0.2 takes a
# facility's recovery from its site waterfall when there is one; the locked path keeps 55% so it stays
# reproducible (Jupiter's waterfall is n/d anyway: nothing delivered and no stated completion).
JUPITER_RECOVERY = 0.55
JUPITER_TABLE = ((date(2025, 11, 7), 100.0), (date(2026, 3, 31), 98.2), (date(2026, 7, 16), 96.7),
                 (date(2026, 8, 17), 94.3), (date(2026, 9, 18), 94.1), (date(2026, 9, 24), 89.0))


def _universe(conn: Conn, entity: str) -> tuple[list[Any], dict[str, list[price_marks.Obs]]]:
    insts = load_instruments(conn, issuers=[entity], kinds=["bond"])
    attach(insts, load_comps(conn, business_today()))
    bonds = straight_bonds(insts).get(entity, [])
    return bonds, price_marks.load(conn, business_today(), [b.id for b in bonds])


def crwv_cds(conn: Conn) -> dict[str, Any]:
    book = load_curves(conn, business_today())
    bonds, obs = _universe(conn, "coreweave")
    by_source = {s: sections(bonds, obs, book, 3, sources=(s,)) for s in ("trace", "nport")}
    rows = []
    for reported, vendor, doc_marden in VENDOR_CRWV:
        want = ("trace",) if reported in MARKET_PRINT_DATES else ("nport",)
        cands = {d: p for d, p in by_source[want[0]].items() if p and abs((d - reported).days) <= MAX_MATCH_DAYS
                 and (reported not in MARKET_PRINT_DATES or d <= reported)}
        if not cands:
            rows.append({"date": reported.isoformat(), "vendorBp": vendor, "methodologyBp": doc_marden,
                         "mardenBp": None, "why": f"no {want[0]} cross-section within {MAX_MATCH_DAYS} days"})
            continue
        day = min(cands, key=lambda d: (abs((d - reported).days), d))  # equidistant: the earlier (no look-ahead)
        lam, how, _ = raw_level(day, cands[day])
        curve = book.curve(day)
        assert curve is not None
        bp = isda(day, curve, lam).spread_bp
        rows.append({"date": reported.isoformat(), "vendorBp": vendor, "methodologyBp": doc_marden,
                     "mardenBp": round(bp), "errorPct": round(100 * (bp - vendor) / vendor, 1),
                     "bondDate": day.isoformat(), "bonds": len(cands[day]), "how": how,
                     "holders": [p.holders for p in cands[day]]})
    graded = [r for r in rows if r.get("mardenBp") is not None]
    errs = [r["errorPct"] for r in graded]
    return {"rows": rows, "graded": len(graded),
            "meanAbsErrorPct": round(sum(abs(e) for e in errs) / len(errs), 1) if errs else None,
            "biasPct": round(sum(errs) / len(errs), 1) if errs else None, "targetMeanAbsErrorPct": 12.0}


def crwv_lobo(conn: Conn) -> dict[str, Any]:
    book = load_curves(conn, business_today())
    bonds, obs = _universe(conn, "coreweave")
    secs = sections(bonds, obs, book, 3, sources=("nport",))
    by_id = {b.id: b for b in bonds}
    # One month-end cross-section per month: funds whose fiscal month ends on the last business day (05-29)
    # and on the calendar month end (05-31) report the same month; grading both would count May twice.
    month_end: dict[tuple[int, int], date] = {}
    for day, pts in secs.items():
        key = (day.year, day.month)
        if key not in month_end or sum(p.holders for p in pts) > sum(p.holders for p in secs[month_end[key]]):
            month_end[key] = day
    rows = []
    for day, pts in sorted(secs.items()):
        if not date(2026, 4, 1) <= day <= date(2026, 7, 31) or month_end[(day.year, day.month)] != day:
            continue
        nport = [p for p in pts if p.source == "nport"]
        for p in nport:
            others = [q for q in nport if q.instrument_id != p.instrument_id]
            if not others:
                continue
            terms = by_id[p.instrument_id].terms
            curve = book.curve(day)
            assert terms is not None and curve is not None
            lam, _ = interpolate([(q.years, q.hazard) for q in others], year_frac(day, terms.maturity))
            model = clean_price(flows(terms, curve, day, None), curve, lam)
            rows.append({"date": day.isoformat(), "instrumentId": p.instrument_id, "actual": round(p.price, 3),
                         "model": round(model, 3), "error": round(model - p.price, 3), "holders": p.holders})
    errs = [r["error"] for r in rows]
    n = len(errs)
    return {"rows": rows, "n": n,
            "maePts": round(sum(abs(e) for e in errs) / n, 3) if n else None,
            "rmsePts": round(math.sqrt(sum(e * e for e in errs) / n), 3) if n else None,
            "within1PtPct": round(100 * sum(abs(e) <= 1 for e in errs) / n, 1) if n else None,
            "target": {"n": 17, "maePts": 0.76, "rmsePts": 0.87, "within1PtPct": 65}}


_ORACLE: dict[date, tuple[Any, Any]] = {}


def _oracle_on(conn: Conn, day: date) -> tuple[Any, Any]:
    """Oracle's curve as public on `day` (point in time) and that day's curve book, cached per day."""
    if day not in _ORACLE:
        book = load_curves(conn, day)
        bonds, _ = _universe(conn, "oracle")
        obs = price_marks.load(conn, day, [b.id for b in bonds])
        _ORACLE[day] = (build(conn, day, "oracle", bonds, obs, book, "ORCL"), book)
    return _ORACLE[day]


def _jupiter_price(conn: Conn, day: date, signals: list[price_signals.Signal]) -> dict[str, Any]:
    cur, book = _oracle_on(conn, day)
    ch = Chain(JUPITER_ID, tenant="oracle", completion=JUPITER_COMPLETION, signals_site="jupiter")
    issue_curve, issue_sofr = book.curve(JUPITER_ISSUE), book.sofr_on(JUPITER_ISSUE)
    out: dict[str, Any] = {"date": day.isoformat()}
    if issue_curve is None or issue_sofr is None or not cur.days:
        out["why"] = "no Treasury/SOFR on the issue date or no Oracle intensity public yet"
        return out
    at_issue = cur.hazard(JUPITER_ISSUE)
    if at_issue is None:
        at_issue = cur.hazard(cur.days[0])
        out["oracleAtIssueBackfilledFrom"] = cur.days[0].isoformat()
    assert at_issue is not None
    f0 = flows(JUPITER_TERMS, issue_curve, JUPITER_ISSUE, issue_sofr)
    hz0 = hazard_fn(ch, Legs(at_issue[0], None, None, 0, 0), JUPITER_ISSUE)
    lp = solve_spread(f0, issue_curve, 100.0, hz0, JUPITER_RECOVERY).value
    got = cur.hazard(day) if day >= cur.days[0] else None
    curve, sofr = book.curve(day), book.sofr_on(day)
    if got is None or curve is None or sofr is None:
        out.update({"lpBp": round(lp * 1e4, 1), "why": "Oracle intensity, curve or SOFR missing on the day"})
        return out
    denials, delays = price_signals.counts(signals, day)
    hz = hazard_fn(ch, Legs(got[0], None, None, denials, delays), day)
    px = clean_price(flows(JUPITER_TERMS, curve, day, sofr), curve, hz, JUPITER_RECOVERY, lp)
    cds = isda(day, curve, got[0]).spread_bp
    out.update({"price": round(px, 2), "lpBp": round(lp * 1e4, 1), "oracleCdsBp": round(cds),
                "oracleHazard": round(got[0], 5), "denials": denials, "delays": delays})
    return out


def jupiter(conn: Conn, scan_step_days: int = 7) -> dict[str, Any]:
    locked = list(price_signals.JUPITER_LOCKED)
    records = price_signals.from_records(conn, business_today(), "jupiter")
    table = []
    for day, target in JUPITER_TABLE:
        a = _jupiter_price(conn, day, locked)
        b = _jupiter_price(conn, day, records)
        table.append({"date": day.isoformat(), "methodology": target, "locked": a.get("price"),
                      "records": b.get("price"), "lockedDetail": a, "recordsDetail": b})
    first_below: dict[str, str | None] = {"locked": None, "records": None}
    day = date(2026, 1, 5)
    while day <= business_today() and None in first_below.values():
        for name, sigs in (("locked", locked), ("records", records)):
            if first_below[name] is None:
                px = _jupiter_price(conn, day, sigs).get("price")
                if px is not None and px < 95.0:
                    first_below[name] = day.isoformat()
        day += timedelta(days=scan_step_days)
    return {"table": table, "firstBelow95": first_below, "scanStepDays": scan_step_days,
            "target": {"below95By": "2026-08-17", "on2026-09-18": 94.1, "dealerQuote2026-09-18": "89-91"}}


def validate(conn: Conn) -> dict[str, Any]:
    return {"crwvCds": crwv_cds(conn), "crwvLobo": crwv_lobo(conn), "jupiter": jupiter(conn)}


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", default=None, help="write the full report to this path")


@cli.action("model", "price_validate", help="Grade the price engine against the methodology's Results table",
            configure=_args)
def validate_command(args: argparse.Namespace) -> int:
    with db.transaction() as conn:
        rep = validate(conn)
    c = rep["crwvCds"]
    print(f"CoreWeave 5y CDS: mean abs error {c['meanAbsErrorPct']}% (target 12%), bias {c['biasPct']}%, "
          f"{c['graded']}/7 graded")
    for r in c["rows"]:
        print(f"  {r['date']}  vendor {r['vendorBp']:>5}  methodology {r['methodologyBp']:>5}  "
              f"Marden {r.get('mardenBp') or 'n/d':>5}  {r.get('errorPct', '')}%  {r.get('bondDate') or r.get('why')}")
    lo = rep["crwvLobo"]
    print(f"CoreWeave leave-one-bond-out: n={lo['n']} MAE {lo['maePts']} RMSE {lo['rmsePts']} "
          f"within 1pt {lo['within1PtPct']}% (target 17 / 0.76 / 0.87 / 65%)")
    j = rep["jupiter"]
    for r in j["table"]:
        print(f"  Jupiter {r['date']}  methodology {r['methodology']:>5}  locked {r['locked']}  records {r['records']}")
    print(f"  first below 95: {j['firstBelow95']} (target: by 2026-08-17)")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(rep, fh, indent=1, default=str)
    return 0

