"""Model `moves` (docs/methods/moves-v0.1.md): how much each V1 instrument's price and yield moved over 1D, 1W,
1M, QTD and YTD, from a trade or from the Marden model, and what drove it.

    cd services && uv run python -m marden model moves [--as-of YYYY-MM-DD] [--instrument ID ...] [--json]

Per window (moves_windows): the traded price when the instrument traded in the window (TRACE, tag as stored),
else the Marden daily price (M); the yield at both ends worked by the yield model (YTW where the debt may be
redeemed early, else YTM; floating-rate debt also its discount margin), and the change in points and bp.
A Marden-priced move is split into the priced events dated inside the window (model `event_impact`, each
change's counterfactual effect), rates (fixed coupon only: the price change from the Treasury par yield at
the instrument's maturity, at an unchanged spread) and spreads (the rest: the credit inputs, such as the
tenant's bond-implied CDS, and the residual); the main driver is the largest. A traded move is driven by
the trades.

One `model_output` per instrument (subject ('instrument', id)): {label, methodVersion, sourceUrl, asOf,
measure, couponType, lastTrade, modelFrom, windows: {1D: MoveWindow, ...}, missing}. Runs daily 09:56 UTC and
within a minute of a new price, a price output, a yield output or an event impact output on its site.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import date
from typing import Any

from marden import cli
from marden.core.db import Conn
from marden.core.queue import app
from marden.models import triggers
from marden.models import yields as ym
from marden.models.event_impact_market import TradeDay, trade_days
from marden.models.moves_windows import WINDOWS, Point, Window, main_driver, window
from marden.models.price import HISTORY_START
from marden.models.price_bonds import clean_at_yield
from marden.models.price_curve import CurveBook, load_curves, year_frac
from marden.models.sourced import Batch, method_version, run_outputs, stored_outputs

MODEL = "moves"
VERSION = "0.1"
METHOD = method_version(MODEL, VERSION)
METHOD_URL = "docs/methods/moves-v0.1.md"
LABEL = "Marden price and yield moves"
CRON_UTC = "56 9 * * *"
NEAR_DAYS = 365  # a yield to worst this close to its workout date is not shown as a move


def universe(conn: Conn) -> list[str]:
    return [r["id"] for r in conn.execute("SELECT id FROM instrument WHERE in_v1 ORDER BY id").fetchall()]


def _model_points(series: Sequence[ym.PricePoint]) -> list[Point]:
    return [Point(p.day, p.price, "model", p.tag or "M", p.source_name, p.source_url, 1, p.method_version)
            for p in series]


def _trade_points(days: Sequence[TradeDay]) -> list[Point]:
    return [Point(d.day, d.price, "trade", d.tag, d.source_name, d.source_url, d.prints) for d in days]


def instrument_events(outs: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """instrument id -> each priced event change on its site: {key, date, label, pts, siteSlug}."""
    out: dict[str, list[dict[str, Any]]] = {}
    for slug, row in outs.items():
        doc = row["output"] if isinstance(row["output"], dict) else {}
        for ch in doc.get("changes") or []:
            for e in ch.get("effects") or []:
                if isinstance(e.get("changePts"), int | float):
                    out.setdefault(e["instrumentId"], []).append(
                        {"key": ch["key"], "date": ch["date"], "label": ch["label"], "pts": e["changePts"],
                         "siteSlug": slug})
    return out


def rates_effect(yinst: ym.Instrument | None, book: CurveBook, w: Window, y_to: float | None) -> dict[str, Any] | None:
    """Fixed coupon: the end price less the price at the end yield without the window's Treasury move."""
    t = yinst.terms if yinst else None
    if t is None or t.floating or w.frm is None or w.to is None or y_to is None or t.maturity <= w.to.day:
        return None
    c0, c1 = book.curve(w.frm.day), book.curve(w.to.day)
    if c0 is None or c1 is None:
        return None
    years = year_frac(w.to.day, t.maturity)
    move = c1.par_yield(years) - c0.par_yield(years)  # percent
    try:
        f = ym.cash_flows(t, w.to.day, None)
    except ValueError:
        return None
    pts = clean_at_yield(f, y_to) - clean_at_yield(f, y_to - move)
    return {"pts": round(pts, 3), "treasuryBp": round(move * 100, 1), "years": round(years, 1)}


def _yield_of(pt: dict[str, Any] | None, measure: str) -> float | None:
    return None if pt is None else pt.get("ytw" if measure == "YTW" else "ytm")


def _near_workout(pt: dict[str, Any] | None) -> bool:
    """The yield to worst is worked to a date under NEAR_DAYS after the price date (unstable)."""
    if not pt or not pt.get("ytwTo") or not pt.get("date"):
        return False
    return (date.fromisoformat(pt["ytwTo"]) - date.fromisoformat(pt["date"])).days < NEAR_DAYS


def window_doc(w: Window, ys: dict[tuple[str, date, float], dict[str, Any]], measure: str, events: list[dict[str, Any]],
               rates: dict[str, Any] | None) -> dict[str, Any]:
    yf = ys.get((w.frm.basis, w.frm.day, w.frm.price)) if w.frm else None
    yt = ys.get((w.to.basis, w.to.day, w.to.price)) if w.to else None
    change = w.change
    y0, y1 = _yield_of(yf, measure), _yield_of(yt, measure)
    inside = [e for e in events if w.frm and w.to and w.frm.day.isoformat() < e["date"] <= w.to.day.isoformat()]
    doc: dict[str, Any] = {
        "key": w.key, "nominalStart": w.nominal.isoformat(), "start": w.frm.day.isoformat() if w.frm else None,
        "end": w.to.day.isoformat() if w.to else None, "partial": w.partial, "basis": w.basis,
        "from": w.frm.doc(yf) if w.frm else None, "to": w.to.doc(yt) if w.to else None,
        "changePts": change, "yieldFrom": y0, "yieldTo": y1,
        "yieldChangeBp": None if change is None or y0 is None or y1 is None else round((y1 - y0) * 100, 1),
        "dmChangeBp": None, "why": w.why, "note": w.note, "events": inside, "rates": rates,
    }
    if change is not None and yf and yt and yf.get("dmBp") is not None and yt.get("dmBp") is not None:
        doc["dmChangeBp"] = round(yt["dmBp"] - yf["dmBp"], 1)
    if change is not None and (y0 is None or y1 is None):
        doc["yieldWhy"] = "No yield worked at one of the two prices (see the instrument page)."
    elif measure == "YTW" and any(_near_workout(pt) for pt in (yf, yt)):
        doc.update({"yieldChangeBp": None, "dmChangeBp": None, "yieldWhy": (
            "The yield to worst is to a prepayment date under a year away (the debt is priced above par), so a "
            "small price move swings it; no yield change is shown.")})
    driver = main_driver(change, inside, rates["pts"] if rates else None, w.basis)
    if driver["kind"] == "spreads" and change is not None:
        driver["explained"] = round(sum(e["pts"] for e in inside) + (rates["pts"] if rates else 0.0), 3)
    doc["driver"] = driver
    return doc


def instrument_output(iid: str, day: date, prev_bd: date | None, yinst: ym.Instrument | None, book: CurveBook,
                      series: Sequence[ym.PricePoint], trades: Sequence[TradeDay],
                      events: list[dict[str, Any]]) -> dict[str, Any]:
    model, traded = _model_points(series), _trade_points(trades)
    wins = [window(k, day, prev_bd, model, traded) for k in WINDOWS]
    anchors = sorted({(p.basis, p.day, p.price) for w in wins for p in (w.frm, w.to) if p is not None})
    ys: dict[tuple[str, date, float], dict[str, Any]] = {}
    measure, coupon, missing = "YTM", None, []
    if yinst is not None and anchors:
        pts = [ym.PricePoint(d, px, "marden", "M", LABEL, None, METHOD) for _, d, px in anchors]
        out = ym.instrument_output(yinst, book, day, pts, None)
        coupon = out.get("couponType")
        if any(w.get("counted") and w.get("kind") != "maturity" for w in out.get("workouts") or []):
            measure = "YTW"
        for a, pt in zip(anchors, out.get("points") or [], strict=False):
            ys[a] = pt
        if out.get("why"):
            missing.append(out["why"])
    docs = {}
    for w in wins:
        y_to = _yield_of(ys.get((w.to.basis, w.to.day, w.to.price)) if w.to else None, "YTM")
        rates = rates_effect(yinst, book, w, y_to) if w.basis == "model" and w.change is not None else None
        docs[w.key] = window_doc(w, ys, measure, events, rates)
    last = traded[-1] if traded else None
    return {"label": LABEL, "methodVersion": METHOD, "sourceUrl": METHOD_URL, "asOf": day.isoformat(),
            "measure": measure, "couponType": coupon,
            "lastTrade": _trade_doc(last), "modelFrom": model[0].day.isoformat() if model else None,
            "tradesFrom": traded[0].day.isoformat() if traded else None, "windows": docs, "missing": missing}


def _trade_doc(p: Point | None) -> dict[str, Any] | None:
    """MoveLastTrade: the latest trade day (no yields: the instrument header works those)."""
    return None if p is None else {k: v for k, v in p.doc().items() if k not in ("ytm", "ytw", "dmBp", "basis")}


def compute(conn: Conn, day: date, ids: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
    wanted = list(ids) if ids else universe(conn)
    book = load_curves(conn, day, since=HISTORY_START)
    prev_bd = None  # the weekday before: the Treasury curve of the day before may not be public yet
    yinsts = ym.load_terms(conn, wanted)
    series, _ = ym.load_prices(conn, wanted, day)
    trades = trade_days(conn, wanted, day)
    events = instrument_events(stored_outputs(conn, "event_impact", "site", None, day))
    return {iid: instrument_output(iid, day, prev_bd, yinsts.get(iid), book, series.get(iid, []), trades.get(iid, []),
                                   events.get(iid, [])) for iid in wanted}


def run(as_of: date | None = None, instruments: list[str] | None = None, *, trigger: str = "manual",
        mark: bool = True) -> dict[str, Any]:
    def batch_of(conn: Conn, day: date) -> Batch:
        batch = Batch()
        outs = compute(conn, day, instruments)
        for iid, out in outs.items():
            batch.add("instrument", iid, out)
        batch.inputs = [[i, {k: w["changePts"] for k, w in o["windows"].items()}] for i, o in sorted(outs.items())]
        return batch

    return run_outputs(MODEL, VERSION, as_of, batch_of, trigger=trigger, mark=mark)


def run_keys(keys: list[str], trigger: str) -> dict[str, Any]:
    result = run(instruments=None if "*" in keys else keys, trigger=trigger)
    return {"instruments": len(result["outputs"]), "changed": len(result["changed"]), "run_id": result["run_id"]}


def _site_instruments(conn: Conn, slug: str) -> list[str]:
    return [r["id"] for r in conn.execute("SELECT id FROM instrument WHERE site_slug = %s AND in_v1", (slug,))]


def _install_triggers() -> None:
    triggers.model_depends(MODEL, "instrument", "@price")
    triggers.model_depends(MODEL, "instrument", "@model:price")
    triggers.model_depends(MODEL, "instrument", "@model:yield")
    triggers.model_depends(MODEL, "site", "@model:event_impact", _site_instruments)
    triggers.register_runner(MODEL, run_keys)


_install_triggers()


@app.periodic(cron=CRON_UTC, periodic_id="moves_daily")
@app.task(name="models.moves", queue="models", queueing_lock="models.moves", lock="models.moves")
def moves_task(timestamp: int | None = None) -> dict[str, Any]:
    result = run(trigger="schedule")
    return {"instruments": len(result["outputs"]), "changed": len(result["changed"]), "run_id": result["run_id"]}


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as-of", type=date.fromisoformat, help="YYYY-MM-DD (default today, US Eastern)")
    p.add_argument("--instrument", action="append", dest="instruments", help="instrument id (repeatable)")
    p.add_argument("--no-mark", action="store_true", help="do not mark read models dirty")
    p.add_argument("--json", action="store_true", help="print every output as JSON")


def _cell(w: dict[str, Any]) -> str:
    if w["changePts"] is None:
        return f"{'n/d':>14}"
    bp = "" if w["yieldChangeBp"] is None else f"{w['yieldChangeBp']:+.0f}bp"
    return f"{w['changePts']:+6.2f}{w['basis'][0]}{'*' if w['partial'] else ' '}{bp:>7}"


@cli.action("model", MODEL, help="Price and yield moves over 1D/1W/1M/QTD/YTD, with their main driver",
            configure=_args)
def run_cli(args: argparse.Namespace) -> int:
    result = run(args.as_of, args.instruments, trigger="manual", mark=not args.no_mark)
    if args.json:
        print(json.dumps(result["outputs"], indent=2, ensure_ascii=False, default=str))
        return 0
    print(f"moves {METHOD} as of {result['as_of']} (run {result['run_id']}): {len(result['outputs'])} instruments "
          "(t = traded, m = model, * = window starts later than its nominal start)")
    for iid, o in result["outputs"].items():
        print(f"  {iid:36} {o['measure']} " + " ".join(_cell(o["windows"][k]) for k in WINDOWS))
    return 0
