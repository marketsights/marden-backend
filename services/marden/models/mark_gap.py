"""Model `mark_gap` (spec 22 "Mark gap and NAV hit"; spec 07 Rules): who carries an instrument above
market, and what marking it to the Marden price would cost each holder.

    python -m marden model mark_gap [--as-of YYYY-MM-DD] [--instrument ID ...] [--json]

Per instrument and holder (each fund's latest public filing that lists it; marden.models.mark_gap_inputs):
    gapPts     = Marden price - the fund's last filed mark                 (points; negative = fund above market)
    navHitMm   = par x gapPts / 100                                         ($mm; negative = NAV falls)
    shareOfNav = navHitMm / the fund's net assets in the same filing
which is spec 22's (fund mark - Marden price) x par / net assets with the sign of a NAV change. Output:
one `model_output` per instrument (model 'mark_gap', version VERSION, subject ('instrument', id)) =
{holders: Holder[], count, parMm, fundMarks: {low, high, filing, date}, nextMarksPublic, markGap,
mardenPrice, missing, basis}: the pieces of InstrumentDetail (holders rows, fundMarks, nextMarksPublic)
and InstrumentRow.markGap (par-weighted, so the KPI equals the holders table total / par). Without a
Marden price the gap fields are null and `missing` says so; an instrument no filing lists is 'no fund'.

Runs: daily 09:48 UTC (05:48 EDT; spec 22 "Daily, 5-6 am ... mark gaps") and within a minute of a new
price output, a new holding line of a holder fund, or a fund's net assets (`models.run_dirty`). A run for
today marks the instrument's read models (`@model:mark_gap`); a past --as-of is a backtest.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, timedelta
from typing import Any

from marden import cli
from marden.core.db import Conn
from marden.core.queue import app
from marden.models import triggers
from marden.models.mark_gap_inputs import BDC_LAG_DAYS, TRANCHE_PARENTS, Holder, Inputs, Price, load
from marden.models.pit import NPORT_LAG_DAYS
from marden.models.sourced import Batch, m_value, method_version, rounded, run_outputs
from marden.readmodels.deps import via

log = logging.getLogger(__name__)

MODEL = "mark_gap"
VERSION = "0.1"
METHOD = method_version(MODEL, VERSION)
CRON_UTC = "48 9 * * *"
SOURCE = "Marden price - last filed fund mark"


def _month_end(year: int, month: int) -> date:
    return date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)


def next_public(day: date, filing_types: set[str]) -> date | None:
    """When the next fund marks become public after `day`: the next N-PORT month end + 60 days, or the next
    BDC quarter end + 45 days (10-Q deadline), whichever comes first among the holders' filing kinds."""
    out: list[date] = []
    start = day - timedelta(days=NPORT_LAG_DAYS + 31)
    months = [_month_end(start.year + (start.month - 1 + k) // 12, (start.month - 1 + k) % 12 + 1) for k in range(8)]
    if "N-PORT" in filing_types:
        out.append(min(m + timedelta(days=NPORT_LAG_DAYS) for m in months if m + timedelta(days=NPORT_LAG_DAYS) > day))
    if filing_types & {"10-Q", "10-K"}:
        start = day - timedelta(days=BDC_LAG_DAYS + 92)
        quarters = [_month_end(start.year + (start.month - 1 + k) // 12, (start.month - 1 + k) % 12 + 1)
                    for k in range(9)]
        out.append(min(q + timedelta(days=BDC_LAG_DAYS) for q in quarters
                       if q.month % 3 == 0 and q + timedelta(days=BDC_LAG_DAYS) > day))
    return min(out) if out else None


def holder_row(h: Holder, price: Price | None) -> dict[str, Any]:
    """One Holder (types.ts). Gap fields are null without a Marden price or a filed mark."""
    mark = h.last_mark
    gap = price.value - mark if price is not None and mark is not None else None
    par_mm: float | None = h.marked_par_usd / 1e6 if mark is not None else h.par_usd / 1e6
    if mark is None and not h.par_usd and h.par_missing:
        par_mm = None  # filed without a principal amount or a mark: par n/d, never 0
    hit = par_mm * gap / 100.0 if gap is not None and par_mm is not None else None
    share = hit * 1e6 / h.net_assets if hit is not None and h.net_assets else None
    return {
        "name": h.name,
        "filing": "N-PORT" if h.filing_type == "N-PORT" else "10-Q",
        "filingUrl": h.filing_url,
        "filingDate": h.period_end.isoformat(),
        "parMm": rounded(par_mm, 6),  # to the dollar: a $430 line is not 0
        "lastMark": rounded(mark, 4),
        "gapPts": rounded(gap, 4),
        "navHitMm": rounded(hit, 4),
        "shareOfNav": rounded(share, 6),
    }


def fund_marks(holders: list[Holder]) -> dict[str, Any] | None:
    """Low and high of the holders' last marks on the newest report date at least two funds share (else the
    newest date): the filed range shown next to the Marden price."""
    marked = [h for h in holders if h.last_mark is not None]
    if not marked:
        return None
    funds_on: dict[date, int] = {}
    for h in marked:
        funds_on[h.period_end] = funds_on.get(h.period_end, 0) + 1
    shared = [d for d, n in funds_on.items() if n >= 2]
    when = max(shared) if shared else max(funds_on)
    marks = [h.last_mark for h in marked if h.period_end == when and h.last_mark is not None]
    kinds = sorted({"N-PORT" if h.filing_type == "N-PORT" else "10-Q" for h in marked if h.period_end == when})
    return {"low": round(min(marks), 4), "high": round(max(marks), 4), "filing": " and ".join(kinds),
            "date": when.isoformat()}


def instrument_output(iid: str, inputs: Inputs) -> dict[str, Any]:
    day, price = inputs.as_of, inputs.prices.get(iid)
    listed = inputs.holders.get(iid, [])
    # A filed line with zero or negative par (a closed or short position) does not make the fund a holder;
    # a line filed with a positive value but no principal amount does (its par is n/d).
    holders = sorted((h for h in listed if h.par_usd > 0 or (not h.par_usd and h.par_missing and h.value_usd > 0)),
                     key=lambda h: (-h.par_usd, h.name))
    rows = [holder_row(h, price) for h in holders]
    rows.sort(key=lambda r: (-(abs(r["navHitMm"]) if r["navHitMm"] is not None else -1), -(r["parMm"] or 0.0),
                             r["name"]))
    missing: list[str] = []
    if len(holders) < len(listed):
        missing.append(f"{len(listed) - len(holders)} filed lines with zero or negative par left out "
                       "(closed or short positions)")
    if no_par := sorted(r["name"] for r in rows if r["parMm"] is None):
        missing.append("par (filed without a principal amount or a mark) for " + ", ".join(no_par)
                       + ": left out of parMm")
    marked = [r for r in rows if r["lastMark"] is not None]
    if not holders:
        gap: Any = "no fund"
    else:
        if price is None:
            missing.append("Marden price (no 'price' model output or Marden daily price on or before the day)")
        if not marked:
            missing.append("a filed mark (no holder line carries a price)")
        hits = [r for r in rows if r["navHitMm"] is not None]
        par = sum(r["parMm"] for r in hits)
        value = round(sum(r["navHitMm"] for r in hits) / par * 100.0, 4) if hits and par > 0 else None
        gap = m_value(value, day, METHOD, source_name=SOURCE)
    if holders and any(h.net_assets is None for h in holders):
        missing.append("fund net assets for " + ", ".join(sorted(h.name for h in holders if h.net_assets is None)))
    filing_kinds = {h.filing_type for h in holders}
    nxt = next_public(day, filing_kinds) if holders else None
    return {
        "holders": rows,
        "count": len(holders),
        "parMm": round(sum(h.par_usd for h in holders) / 1e6, 3),
        "fundMarks": fund_marks(holders),
        "nextMarksPublic": nxt.isoformat() if nxt else None,
        "markGap": gap,
        "mardenPrice": None if price is None else {
            "value": round(price.value, 4), "tag": "M", "asOf": price.as_of.isoformat(),
            "sourceName": "Marden model price",  # rule 4: every value names its source
            **({"methodVersion": price.method} if price.method else {}),
            **({"sourceUrl": price.source_url} if price.source_url else {})},
        "missing": missing,
        "basis": {
            "methodVersion": METHOD,
            "formula": "gapPts = Marden price - last mark; navHitMm = parMm x gapPts / 100; "
                       "shareOfNav = navHitMm / fund net assets (same filing)",
            "markGap": "par-weighted: sum(navHitMm) / sum(parMm) x 100",
            "publicLag": f"N-PORT usable {NPORT_LAG_DAYS} days after period end; BDC schedules on filing "
                         f"(else {BDC_LAG_DAYS} days after quarter end)",
            "priceOrigin": price.origin if price else None,
            "linesWithoutMark": inputs.unpriced_lines.get(iid, 0),
            "bdc10K": sum(1 for h in holders if h.filing_type == "10-K"),
            "netAssets": sorted({h.net_assets_basis for h in holders if h.net_assets_basis}),
        },
    }


def _instrument_ids(conn: Conn, ids: list[str] | None) -> list[str]:
    rows = conn.execute(
        """SELECT id FROM instrument WHERE (%(ids)s::text[] IS NULL AND (in_v1 OR id IN
                  (SELECT instrument_id FROM holding WHERE instrument_id IS NOT NULL)))
               OR id = ANY(%(ids)s) ORDER BY id""",
        {"ids": ids},
    ).fetchall()
    return [r["id"] for r in rows]


def run(as_of: date | None = None, instruments: list[str] | None = None, *, trigger: str = "manual",
        mark: bool = True) -> dict[str, Any]:
    """Mark gaps for every V1 or fund-held instrument (or `instruments`) as of `as_of` (default today)."""

    def compute(conn: Conn, day: date) -> Batch:
        ids = _instrument_ids(conn, instruments)
        inputs = load(conn, day, ids)
        batch = Batch()
        for iid in ids:
            batch.add("instrument", iid, instrument_output(iid, inputs))
        batch.inputs = [[iid, sorted(ln for h in inputs.holders.get(iid, []) for ln in h.lines),
                         getattr(inputs.prices.get(iid), "value", None)] for iid in ids]
        return batch

    return run_outputs(MODEL, VERSION, as_of, compute, trigger=trigger, mark=mark)


def run_keys(keys: list[str], trigger: str) -> dict[str, Any]:
    result = run(instruments=None if "*" in keys else keys, trigger=trigger)
    return {"instruments": len(result["outputs"]), "changed": len(result["changed"]), "run_id": result["run_id"]}


# ---------------------------------------------------------------- triggers and schedule
_HOLDING_INSTRUMENTS = via(  # the fund's instruments and the packages they are tranches of
    f"""WITH o AS (SELECT DISTINCT o.instrument_id FROM holding h JOIN holding o ON o.fund_entity_id = h.fund_entity_id
                    WHERE h.id = %s AND o.instrument_id IS NOT NULL)
        SELECT instrument_id FROM o
        UNION SELECT t.parent FROM ({TRANCHE_PARENTS}) t JOIN o ON o.instrument_id = t.child""",
    int_id=True,  # holding.id as an integer: the primary key serves the lookup on every holding write
)
_TRANCHE_PARENT = via(f"SELECT parent FROM ({TRANCHE_PARENTS}) t WHERE t.child = %s")
_FUND_INSTRUMENTS = via(
    "SELECT DISTINCT instrument_id FROM holding WHERE fund_entity_id = %s AND instrument_id IS NOT NULL")


def _install_triggers() -> None:
    triggers.model_depends(MODEL, "instrument", "@model:price")
    triggers.model_depends(MODEL, "instrument", "@price")  # Marden daily prices (price_obs kind 'marden')
    triggers.model_depends(MODEL, "holding", "@holding", _HOLDING_INSTRUMENTS)
    triggers.model_depends(MODEL, "holding", "mark_price", _HOLDING_INSTRUMENTS)
    triggers.model_depends(MODEL, "entity", "net_assets_usd", _FUND_INSTRUMENTS)
    triggers.model_depends(MODEL, "instrument", "tranche_of", _TRANCHE_PARENT)
    triggers.register_runner(MODEL, run_keys)


_install_triggers()


@app.periodic(cron=CRON_UTC, periodic_id="mark_gap_daily")
@app.task(name="models.mark_gap", queue="models", queueing_lock="models.mark_gap", lock="models.mark_gap")
def mark_gap_task(timestamp: int | None = None) -> dict[str, Any]:
    result = run(trigger="schedule")
    return {"instruments": len(result["outputs"]), "changed": len(result["changed"]), "run_id": result["run_id"]}


# ---------------------------------------------------------------- CLI
def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as-of", type=date.fromisoformat, help="YYYY-MM-DD (default today, US Eastern); a past day "
                   "is a backtest on what was public that day")
    p.add_argument("--instrument", action="append", dest="instruments", help="instrument id (repeatable)")
    p.add_argument("--no-mark", action="store_true", help="do not mark read models dirty")
    p.add_argument("--json", action="store_true", help="print every output as JSON")


@cli.action("model", MODEL, help="Mark gap and NAV hit per holder (fund mark vs Marden price)", configure=_args)
def run_cli(args: argparse.Namespace) -> int:
    result = run(args.as_of, args.instruments, trigger="manual", mark=not args.no_mark)
    if args.json:
        print(json.dumps(result["outputs"], indent=2, ensure_ascii=False))
        return 0
    print(f"mark_gap {VERSION} as of {result['as_of']} (run {result['run_id']}): {len(result['outputs'])} "
          f"instruments, changed {len(result['changed'])}")
    for iid, out in result["outputs"].items():
        fm, gap = out["fundMarks"], out["markGap"]
        gap_text = gap if isinstance(gap, str) else ("n/d" if gap["value"] is None else f"{gap['value']:+.2f}")
        marks = f"{fm['low']:.2f}-{fm['high']:.2f} {fm['date']}" if fm else "n/d"
        print(f"  {iid:36} funds {out['count']:4} par ${out['parMm']:10.1f}mm marks {marks:24} gap {gap_text:>7} "
              f"next {out['nextMarksPublic'] or 'n/d'}")
    return 0
