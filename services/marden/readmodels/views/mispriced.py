"""Read model `mispriced` (all): GET /api/mispriced, MispricedPage (contract apps/api/src/contracts/mispriced.ts;
method docs/methods/mispriced-v0.1.md). What is about to drop or bounce.

Per V1 instrument with a Marden price: the gap between the Marden price and the latest market evidence
(mispriced_inputs: a trade, a quote, else the funds' filed marks), in points and in yield (the yield model's YTW,
YTM where it cannot be redeemed early, at both prices on the build day), the events dated after that evidence with
the event-impact model's effect on the instrument, and three groups:

- `downside` ("Market above our value"): the Marden price is 3+ points below the market, or a negative event
  (a model effect of -0.25 points or worse, or a red record) came after the market evidence;
- `bounce` ("Market below our value"): the reverse;
- `stale` ("Stale marks"): the funds' filed marks sit 3+ points above the latest trade or quote dated after them.

Each row says how far to trust it (`confidence`: no trade since the event, trades since and whether the market
agrees, fund marks only) and, from the price output, which inputs are model assumptions rather than records (an
assumed lease deadline, an estimated share built), so a gap that rests on those says so. Sorted by |gap| x size.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from marden.core.db import Conn, Row
from marden.models import yields as ym
from marden.models.price_curve import load_curves
from marden.readmodels.context import known_facts
from marden.readmodels.registry import builder
from marden.readmodels.views import catalysts as cat
from marden.readmodels.views import mispriced_inputs as mi
from marden.readmodels.views.instrument_inputs import day, instrument_identity, num
from marden.readmodels.views.instrument_text import display_name, type_label
from marden.readmodels.views.instruments import size_b
from marden.readmodels.views.sites_common import latest_output

Doc = dict[str, Any]
LABEL = "Marden mispricing screen"
METHOD = "mispriced v0.1"
METHOD_URL = "docs/methods/mispriced-v0.1.md"
GAP_PTS = 3.0  # a gap this wide (points) puts the row in a group on its own
EVENT_PTS = 0.25  # a model effect this large (points) makes an event negative or positive
RECENT_DAYS = 30  # events this recent count for the confidence note, priced or not
SIZE_FACTS = ("amount_usd", "commitment_usd", "drawn_usd", "amount_basis", "seniority")


def m(value: Any) -> Doc:
    return {
        "value": value,
        "tag": "M",
        "asOf": day().isoformat(),
        "sourceName": LABEL,
        "sourceUrl": METHOD_URL,
        "methodVersion": METHOD,
    }


def short(d: str) -> str:
    from marden.readmodels.views.debt_effects_text import when

    return when(d)


# ---------------------------------------------------------------- yields
class Yields:
    """The yield model's YTW (else YTM) at any price of an instrument on the build day."""

    def __init__(self, conn: Conn, ids: list[str]) -> None:
        self.insts = ym.load_terms(conn, ids)
        self.book = load_curves(conn, day(), since=ym.HISTORY_START)

    def at(self, iid: str, prices: list[float]) -> tuple[list[float | None], str, str | None]:
        """Yields at `prices`: YTW where the debt can be redeemed early and both prices are at or under par (above
        par, a loan's prepay-at-par worst case is days away and its yield is not a measure of value), else YTM."""
        inst = self.insts.get(iid)
        if inst is None:
            return [None] * len(prices), "YTM", "No coupon or maturity terms in the sources"
        pts = [ym.PricePoint(day(), p, "marden", "M", LABEL, None, METHOD) for p in prices]
        out = ym.instrument_output(inst, self.book, day(), pts, None)
        early = any(w.get("counted") and w.get("kind") != "maturity" for w in out.get("workouts") or [])
        key = "ytw" if early and max(prices) <= 100 else "ytm"
        got = [num(p.get(key)) for p in out.get("points") or []]
        return (got if len(got) == len(prices) else [None] * len(prices)), key.upper(), out.get("why")


# ---------------------------------------------------------------- assumptions in the model price
def assumptions(conn: Conn, price_out: Row | None, slug: str | None) -> list[str]:
    """Inputs of the Marden price that are model assumptions, not records, in plain words."""
    body = (price_out["output"] or {}) if price_out is not None else {}
    basis = body.get("basis") or {}
    out: list[str] = []
    if slug:
        comp = conn.execute(
            "SELECT doc FROM read_model WHERE view = 'completion' AND key = 'all' AND as_of <= %s"
            " ORDER BY as_of DESC LIMIT 1",
            (day(),),
        ).fetchone()
        row = next((r for r in ((comp or {}).get("doc") or {}).get("rows") or [] if r.get("slug") == slug), None)
        proj = (row or {}).get("projection") or {}
        if proj.get("outsideDateAssumed"):
            when = (proj.get("outsideDate") or {}).get("value")
            out.append(f"the lease deadline ({when}) is assumed, not filed")
    rec = body.get("recoveryInputs") or {}
    if (rec.get("inputs") or {}).get("completionEstimated"):
        out.append("the share of the site built is estimated, not filed")
    if body.get("recoveryBasis") == "default_assumption":
        out.append("the recovery if it defaults is a default assumption, not the site's collateral")
    for a in (basis.get("terms") or {}).get("assumptions") or []:
        if " taken as " not in str(a):  # a day picked inside a stated month is not a pricing assumption
            out.append(str(a).replace("_", " "))
    return out


CREDIT_INPUTS = ("tenantCdsBp", "issuerCdsBp", "obligorCdsBp")


def model_basis(price_out: Row | None) -> list[str]:
    """What the Marden price leans on, in words: the credit curve it is priced off, the last observation it used."""
    body = (price_out["output"] or {}) if price_out is not None else {}
    out = [
        f"priced off the {i.get('label')} ({num(i.get('value')):,.0f} bp)"
        for i in body.get("inputs") or []
        if isinstance(i, dict) and i.get("key") in CREDIT_INPUTS and num(i.get("value")) is not None
    ]
    last = (body.get("basis") or {}).get("lastObservation")
    if last:
        out.append(f"last observation it used: {last}")
    return out


# ---------------------------------------------------------------- one row
def groups_of(gap: float | None, events: list[Doc]) -> tuple[list[str], bool, bool]:
    neg = any(
        (e["effectPts"] is not None and e["effectPts"] <= -EVENT_PTS) or e.get("severity") == "red" for e in events
    )
    pos = any(
        (e["effectPts"] is not None and e["effectPts"] >= EVENT_PTS) or e.get("severity") == "green" for e in events
    )
    g = gap if gap is not None else 0.0
    if g <= -GAP_PTS or (neg and g < 0):
        return ["downside"], neg, pos
    if g >= GAP_PTS or (pos and g > 0):
        return ["bounce"], neg, pos
    return [], neg, pos


def driver(events: list[Doc]) -> Doc | None:
    """The event that moves the price most (model effect), else the newest red record, else the newest event."""
    priced = [e for e in events if e["effectPts"] is not None and abs(e["effectPts"]) >= 0.05]
    if priced:
        return max(priced, key=lambda e: abs(e["effectPts"]))
    red = [e for e in events if e.get("severity") == "red"]
    pick = red or events
    return pick[0] if pick else None


def confidence(market: Doc | None, rows: list[Row], gap: float | None, recent: list[Doc], since: list[Doc]) -> str:
    if market is None:
        return "Model only: no trade, quote or fund mark on file"
    if market["kind"] == "fund_mark":
        return f"Fund marks only (as of {short(market['date'])}); no trade or quote on file"
    if since:
        first = min(e["date"] for e in since)
        return f"Model only: no trade or quote since {short(first)}"
    if recent:
        ev = recent[0]
        n = mi.trade_days_after(rows, ev["date"])
        if n:
            agree = gap is not None and abs(gap) < GAP_PTS
            verdict = "agrees" if agree else "still differs"
            return f"{n} trading day{'s' if n != 1 else ''} since {short(ev['date'])}; the market {verdict}"
    kind = {"trade": "trade", "quote": "quote", "dealer": "dealer quote"}[market["kind"]]
    agree = gap is not None and abs(gap) < 1
    return f"Last {kind} {short(market['date'])}; no event since{'; the market agrees' if agree else ''}"


def stale_of(fund: Doc | None, trade: Doc | None) -> Doc | None:
    if fund is None or trade is None or trade["date"] <= fund["date"]:
        return None
    gap = round(fund["price"] - trade["price"], 2)
    return {"fundMark": fund, "trade": trade, "gapPts": m(gap)} if gap >= GAP_PTS else None


def row_of(conn: Conn, inst: Row, ylds: Yields) -> Doc | None:
    iid = inst["id"]
    price_out = latest_output(conn, "price", "instrument", iid)
    mp = num(((price_out["output"] or {}).get("mardenPrice") or {}).get("value")) if price_out is not None else None
    if mp is None:
        return None
    rows = mi.market_rows(conn, iid)
    fund = mi.fund_marks(conn, iid)
    market = mi.latest_market(rows) or fund
    gap = round(mp - market["price"], 2) if market else None
    since = mi.events_since(conn, inst, market["date"]) if market else []
    recent = (
        mi.events_since(conn, inst, (day() - timedelta(days=RECENT_DAYS)).isoformat()) if market and not since else []
    )
    groups, neg, pos = groups_of(gap, since)
    stale = stale_of(fund, mi.latest_trade(rows))
    if stale:
        groups.append("stale")
    (y_m, y_x), measure, why_y = ylds.at(iid, [mp, market["price"]]) if market else ([None, None], "YTM", None)
    gap_bp = round((y_m - y_x) * 100) if y_m is not None and y_x is not None else None
    drv = driver(since)
    facts = known_facts(conn, "instrument", iid, list(SIZE_FACTS))
    size = size_b(facts)
    sen = str(facts["seniority"]["value"]) if facts.get("seniority") and facts["seniority"]["value"] else None
    assumed = assumptions(conn, price_out, inst["site_slug"])
    priced = sum(e["effectPts"] for e in since if e["effectPts"] is not None)
    basis = None
    if gap is not None and abs(gap) >= GAP_PTS and abs(priced) < abs(gap) / 2 and assumed:
        basis = (
            "Most of the gap is not explained by an event since the market price; it rests on model inputs: "
            f"{'; '.join(assumed)}."
        )
    why: Doc = {}
    if market is None:
        why["market"] = "No trade, quote or fund mark on file"
    if gap_bp is None and market is not None:
        why["gapBp"] = why_y or "The yield model cannot work a yield at these prices"
    if size["value"] is None:
        why["size"] = "Size not disclosed in any source"
    score = abs(gap or 0) * (num(size["value"]) or 0)
    return {
        "instrumentId": iid,
        "name": display_name(inst),
        "href": f"/instruments/{iid}",
        "type": type_label(inst, sen),
        "issuer": inst["issuer_name"],
        "site": {"slug": inst["site_slug"], "name": inst["site_name"]} if inst["site_slug"] else None,
        "sizeB": size,
        "mardenPrice": ((price_out["output"] if price_out is not None else None) or {}).get("mardenPrice"),
        "market": market,
        "gapPts": m(gap),
        "gapBp": m(gap_bp),
        "measure": measure,
        "yieldMarden": y_m,
        "yieldMarket": y_x,
        "groups": groups,
        "negativeEvent": neg,
        "positiveEvent": pos,
        "events": since[:8],
        "eventCount": len(since),
        "driver": drv,
        "daysSinceEvent": mi.days_since(drv["date"]) if drv else None,
        "confidence": confidence(market, rows, gap, recent, since),
        "assumptions": assumed,
        "basisNote": basis,
        "modelBasis": model_basis(price_out),
        "stale": stale,
        "score": round(score, 3),
        "why": why,
    }


@builder("mispriced", many=True)
def build_mispriced(conn: Conn):
    insts = instrument_identity(conn, v1_only=True)
    ylds = Yields(conn, [i["id"] for i in insts])
    rows = [r for i in insts if (r := row_of(conn, i, ylds)) is not None]
    rows.sort(key=lambda r: (-r["score"], -abs(r["gapPts"]["value"] or 0), r["name"]))
    catalysts = cat.mispriced_catalysts(conn, rows, {i["id"]: i for i in insts})
    counts = {g: sum(1 for r in rows if g in r["groups"]) for g in ("downside", "bounce", "stale")}
    yield (
        "all",
        {
            "asOf": day().isoformat(),
            "catalysts": catalysts,
            "rows": rows,
            "counts": counts,
            "thresholds": {"gapPts": GAP_PTS, "eventPts": EVENT_PTS},
            "method": {"label": LABEL, "methodVersion": METHOD, "sourceUrl": METHOD_URL},
        },
    )
