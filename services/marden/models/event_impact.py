"""Model `event_impact` (docs/methods/event-impact-v0.1.md): what each event did to the value of a site's debt.

    cd services && uv run python -m marden model event_impact [--as-of YYYY-MM-DD] [--site SLUG ...] [--json]

For every site with debt the price engine prices, each dated change in the site's event inputs since Jan 1
(event_impact_changes: power and permit signals, the projected completion, dealer quotes) is priced twice on
the day the engine first sees it, with the engine's own functions (event_impact_price): as the engine prices
that day, and with that change's inputs left out, everything else the same. The difference is the change's
effect on each instrument, in points and in yield (YTW where the debt can be redeemed early, else YTM, worked
by the yield model; floating-rate debt also its discount margin). A signal day is also priced with each kind
of signal left out on its own (`byKind`), so a single record (a force-majeure notice) has its own effect.

Alongside the model effect: the observed market move over the same day, the instrument's last trade before
and first trade after it (event_impact_market), for the site's debt and the V1 bonds of its tenant and end
user (which the engine prices from their own trades, not from the site's records).

One `model_output` per site (subject ('site', slug)): {label, methodVersion, sourceUrl, instruments, related,
changes: [{key, kind, date, label, records, effects, byKind, market, why}], countedDays {kind: [dates]},
missing}. Runs daily 09:52 UTC (after the 09:40 price and 09:44 yield runs) and within a minute of a new site
event, a site fact the signals read, a cushion run, or a price observation of the site's debt.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from datetime import date
from typing import Any

from marden import cli
from marden.core.db import Conn
from marden.core.queue import app
from marden.models import price_signals, triggers
from marden.models import yields as ym
from marden.models.event_impact_changes import (
    CompletionChange,
    QuoteChange,
    SignalChange,
    completion_changes,
    day_change,
    quote_changes,
    record_doc,
    record_tags,
    signal_days,
    without,
)
from marden.models.event_impact_market import market_move, trade_days
from marden.models.event_impact_price import DayModel, day_model, priceable
from marden.models.price import targets
from marden.models.sourced import Batch, method_version, run_outputs
from marden.readmodels.context import business_today

log = logging.getLogger(__name__)

MODEL = "event_impact"
VERSION = "0.1"
METHOD = method_version(MODEL, VERSION)
METHOD_URL = "docs/methods/event-impact-v0.1.md"
LABEL = "Marden event impact"
CRON_UTC = "52 9 * * *"
Change = SignalChange | CompletionChange | QuoteChange


# ---------------------------------------------------------------- who is affected
def site_keys(conn: Conn) -> list[str]:
    """Sites with debt the price engine prices."""
    rows = conn.execute("SELECT DISTINCT site_slug FROM instrument WHERE id = ANY(%s) AND site_slug IS NOT NULL "
                        "ORDER BY 1", (targets(conn),)).fetchall()
    return [r["site_slug"] for r in rows]


def site_debt(conn: Conn, slug: str) -> list[str]:
    return [r["id"] for r in conn.execute("SELECT id FROM instrument WHERE site_slug = %s ORDER BY id", (slug,))]


def related_bonds(conn: Conn, slug: str) -> list[dict[str, Any]]:
    """V1 bonds of the site's tenant and end user (or of their subsidiaries), not on the site itself."""
    rows = conn.execute(
        """SELECT DISTINCT i.id, cp.role, cp.label FROM chain_party cp
             JOIN entity e ON e.id = cp.entity_id OR e.parent_id = cp.entity_id
             JOIN instrument i ON i.issuer_entity_id = e.id
            WHERE cp.site_slug = %s AND cp.role IN ('tenant', 'end_user') AND i.in_v1 AND i.kind = 'bond'
              AND i.site_slug IS DISTINCT FROM %s ORDER BY cp.role DESC, i.id""", (slug, slug)).fetchall()
    return [{"instrumentId": r["id"], "role": r["role"], "party": r["label"]} for r in rows]


# ---------------------------------------------------------------- the changes of one site
def plan(conn: Conn, slug: str, day: date, ids: Sequence[str]) -> list[Change]:
    since = date(day.year, 1, 1)
    out: list[Change] = []
    for p in signal_days(price_signals.from_records(conn, day, slug), since):
        if p <= day and (ch := day_change(price_signals.from_records(conn, p, slug), p)) is not None:
            out.append(ch)
    out.extend(completion_changes(conn, slug, since, day))
    out.extend(quote_changes(conn, ids, since, day))
    return sorted(out, key=lambda c: (c.day, type(c).__name__))


def _plain_missing(missing: Sequence[str], day: date) -> str:
    text = " ".join(missing).lower()
    if "convertible" in text:
        return ("Not priced: a convertible note's value depends on the share price, which the price model does not "
                "model.")
    if "non-usd" in text:
        return "Not priced: the price model has no curve for non-USD debt."
    if "issued" in text and "after" in text:
        return f"Not yet issued on {day:%b} {day.day}, {day.year}."
    if "terms" in text:
        return "Not priced: its coupon or maturity is not in the sources."
    return f"The price model has no price for it on {day:%b} {day.day}, {day.year}."


def _yields(yinst: ym.Instrument | None, dm: DayModel, prices: Sequence[float]) -> tuple[list[dict[str, Any]], str]:
    """The yield model's points at `prices` on the day, and the measure shown (YTW where the debt may be redeemed
    early, else YTM)."""
    if yinst is None:
        return [], "YTM"
    pts = [ym.PricePoint(dm.day, p, "marden", "M", LABEL, None, METHOD) for p in prices]
    out = ym.instrument_output(yinst, dm.book, dm.day, pts, None)
    early = any(w.get("counted") and w.get("kind") != "maturity" for w in out.get("workouts") or [])
    return out.get("points") or [], "YTW" if early else "YTM"


def effect(dm: DayModel, yinst: ym.Instrument | None, iid: str, base: dict[str, Any],
           cf: dict[str, Any]) -> dict[str, Any]:
    """InstrumentEffect: the price with the change (`base`) and without it (`cf`), and the yields at both."""
    w, wo = base.get("price"), cf.get("price")
    out: dict[str, Any] = {"instrumentId": iid, "with": w, "without": wo, "changePts": None, "stored": dm.stored(iid),
                           "measure": "YTM", "yieldWith": None, "yieldWithout": None, "yieldChangePct": None,
                           "dmWithBp": None, "dmWithoutBp": None, "why": None}
    if not isinstance(w, int | float) or not isinstance(wo, int | float):
        out["why"] = _plain_missing(base.get("missing") or cf.get("missing") or [], dm.day)
        return out
    out["changePts"] = round(w - wo, 3)
    pts, measure = _yields(yinst, dm, [w, wo])
    out["measure"] = measure
    key = "ytw" if measure == "YTW" else "ytm"
    if len(pts) == 2 and pts[0].get(key) is not None and pts[1].get(key) is not None:
        out.update({"yieldWith": pts[0][key], "yieldWithout": pts[1][key],
                    "yieldChangePct": round(pts[0][key] - pts[1][key], 3),
                    "dmWithBp": pts[0].get("dmBp"), "dmWithoutBp": pts[1].get("dmBp")})
    return out


def _price_change(dm: DayModel, yinsts: dict[str, ym.Instrument], ch: Change, slug: str,
                  ids: Sequence[str]) -> dict[str, Any]:
    doc: dict[str, Any] = {"date": ch.day.isoformat(), "label": ch.label(), "effects": [], "why": None}
    if isinstance(ch, SignalChange):
        sigs = dm.site_signals(slug)
        doc.update({"key": f"signals:{ch.day}", "kind": "signals", "kinds": ch.kinds,
                    "counts": {"before": list(ch.before), "after": list(ch.after)}, "byKind": {}})
        for iid in ids:
            base = dm.price(iid)
            doc["effects"].append(effect(dm, yinsts.get(iid), iid, base, dm.price(iid, signals=without(sigs, ch.day))))
            for k in ch.kinds:
                cf = dm.price(iid, signals=without(sigs, ch.day, [k]))
                doc["byKind"].setdefault(k, []).append(effect(dm, yinsts.get(iid), iid, base, cf))
    elif isinstance(ch, CompletionChange):
        doc.update({"key": f"completion:{ch.day}", "kind": "completion",
                    "from": ch.before.isoformat() if ch.before else None,
                    "to": ch.after.isoformat() if ch.after else None})
        for iid in ids:
            cf = dm.price(iid, completion=ch.before)
            doc["effects"].append(effect(dm, yinsts.get(iid), iid, dm.price(iid), cf))
    else:
        doc.update({"key": f"quote:{ch.day}:{ch.instrument_id}", "kind": "quote", "instrumentId": ch.instrument_id,
                    "quote": {"price": ch.price, "tag": ch.tag, "sourceName": ch.source_name,
                              "sourceUrl": ch.source_url or ""}})
        iid = ch.instrument_id
        doc["effects"].append(effect(dm, yinsts.get(iid), iid, dm.price(iid), dm.price(iid, drop_obs=ch.matches)))
    return doc


def _records(ch: Change, tags: dict[str, str]) -> list[dict[str, Any]]:
    if isinstance(ch, SignalChange):
        return [record_doc(s, tags) for s in ch.records]
    if isinstance(ch, QuoteChange):
        return [{"date": ch.day.isoformat(), "text": ch.label(), "tag": ch.tag, "sourceName": ch.source_name,
                 "sourceUrl": ch.source_url or "", "kind": "quote", "kindWords": "dealer quote"}]
    return [{"date": ch.day.isoformat(), "text": ch.label(), "tag": "M", "sourceName": "Marden cushion model",
             "sourceUrl": "docs/methods/cushion-v0.1.md", "kind": "completion", "kindWords": "projected completion"}]


def compute(conn: Conn, day: date, slugs: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
    """Every site's output as of `day` (no writes)."""
    keys = list(slugs) if slugs else site_keys(conn)
    sites: dict[str, dict[str, Any]] = {}
    plans: dict[str, list[Change]] = {}
    for slug in keys:
        ids, related = site_debt(conn, slug), related_bonds(conn, slug)
        plans[slug] = plan(conn, slug, day, ids)
        sites[slug] = {"label": LABEL, "methodVersion": METHOD, "sourceUrl": METHOD_URL, "asOf": day.isoformat(),
                       "instruments": ids, "related": related, "changes": [], "countedDays": {}, "missing": []}
    every = sorted({i for s in sites.values() for i in s["instruments"] + [r["instrumentId"] for r in s["related"]]})
    yinsts = ym.load_terms(conn, every)
    trades = trade_days(conn, every, day)
    for d in sorted({c.day for cs in plans.values() for c in cs}):
        why = priceable(d)
        dm = None
        if why is None:
            try:
                dm = day_model(conn, d)
            except ValueError as exc:
                why = str(exc)
        for slug, cs in plans.items():
            tags = record_tags(conn, slug) if any(c.day == d for c in cs) else {}
            for ch in (c for c in cs if c.day == d):
                ids = sites[slug]["instruments"]
                if dm is None:
                    doc = {"key": f"{type(ch).__name__}:{d}", "kind": "unpriced", "date": d.isoformat(),
                           "label": ch.label(), "effects": [], "why": f"Not priced: {why}."}
                else:
                    doc = _price_change(dm, yinsts, ch, slug, ids)
                doc["records"] = _records(ch, tags)
                moved = [i for i in ids if not isinstance(ch, QuoteChange) or i == ch.instrument_id]
                doc["market"] = [market_move(i, i, trades.get(i, []), d)
                                 for i in moved + [r["instrumentId"] for r in sites[slug]["related"]]]
                sites[slug]["changes"].append(doc)
                if isinstance(ch, SignalChange):
                    for k in ch.kinds:
                        sites[slug]["countedDays"].setdefault(k, []).append(d.isoformat())
    return sites


def run(as_of: date | None = None, sites: list[str] | None = None, *, trigger: str = "manual",
        mark: bool = True) -> dict[str, Any]:
    def batch_of(conn: Conn, day: date) -> Batch:
        batch = Batch()
        outs = compute(conn, day, sites)
        for slug, out in outs.items():
            batch.add("site", slug, out)
        batch.inputs = [[s, [[c["key"], [e.get("changePts") for e in c["effects"]]] for c in o["changes"]]]
                        for s, o in sorted(outs.items())]
        return batch

    return run_outputs(MODEL, VERSION, as_of, batch_of, trigger=trigger, mark=mark)


def run_keys(keys: list[str], trigger: str) -> dict[str, Any]:
    result = run(sites=None if "*" in keys else keys, trigger=trigger)
    return {"sites": len(result["outputs"]), "changed": len(result["changed"]), "run_id": result["run_id"]}


# ---------------------------------------------------------------- triggers and schedule
def _instrument_site(conn: Conn, subject_id: str) -> list[str]:
    row = conn.execute("SELECT site_slug FROM instrument WHERE id = %s", (subject_id,)).fetchone()
    return [row["site_slug"]] if row and row["site_slug"] else []


def _install_triggers() -> None:
    triggers.model_depends(MODEL, "site", "@event")
    triggers.model_depends(MODEL, "site", "@model:cushion")
    for fld in price_signals.SITE_FIELDS:
        triggers.model_depends(MODEL, "site", fld)
    triggers.model_depends(MODEL, "instrument", "@price", _instrument_site)
    triggers.register_runner(MODEL, run_keys)


_install_triggers()


@app.periodic(cron=CRON_UTC, periodic_id="event_impact_daily")
@app.task(name="models.event_impact", queue="models", queueing_lock="models.event_impact", lock="models.event_impact")
def event_impact_task(timestamp: int | None = None) -> dict[str, Any]:
    result = run(trigger="schedule")
    return {"sites": len(result["outputs"]), "changed": len(result["changed"]), "run_id": result["run_id"]}


# ---------------------------------------------------------------- CLI
def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as-of", type=date.fromisoformat, help="YYYY-MM-DD (default today, US Eastern)")
    p.add_argument("--site", action="append", dest="sites", help="site slug (repeatable)")
    p.add_argument("--no-mark", action="store_true", help="do not mark read models dirty")
    p.add_argument("--json", action="store_true", help="print every output as JSON")


def _pts(v: Any) -> str:
    return "n/d" if v is None else f"{v:+.2f}"


@cli.action("model", MODEL, help="What each event did to the value of a site's debt (counterfactual Marden price)",
            configure=_args)
def run_cli(args: argparse.Namespace) -> int:
    result = run(args.as_of, args.sites, trigger="manual", mark=not args.no_mark)
    if args.json:
        print(json.dumps(result["outputs"], indent=2, ensure_ascii=False, default=str))
        return 0
    print(f"event_impact {METHOD} as of {result['as_of'] or business_today()} (run {result['run_id']}): "
          f"{len(result['outputs'])} sites, changed {len(result['changed'])}")
    for slug, out in result["outputs"].items():
        for c in out["changes"]:
            eff = ", ".join(f"{e['instrumentId']} {_pts(e['changePts'])}" for e in c["effects"])
            print(f"  {slug:28} {c['date']} {c['kind']:10} {eff or c.get('why') or ''}")
    return 0
