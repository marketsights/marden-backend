"""Read model `site_debt_effects` (site.slug): did each record on the site's Events and Evidence tabs change the
value of the site's debt? (GET /api/sites/:slug/debt-effects; contract apps/api/src/contracts/moves.ts).

From the `event_impact` model's output for the site (what each priced change in the site's event inputs did to
each instrument: the Marden price that day with the change and without it, and the yields at both) and the
records the two tabs list (views/sites_tabs). Each record is matched to the changes it stands for
(debt_effects_text.match): its effect is that change's, with the instrument's name, in points and yield, and
the observed trades of the debt and of the tenant's and end user's bonds over the same days. A record that
stands for no change says why in plain words (procedural, status only, already priced in, priced later, not
an input, no priced debt). Records with the site's common reason are left out of `effects` and take
`default`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from marden.core.db import Conn, Row
from marden.models.event_impact import LABEL, METHOD, METHOD_URL
from marden.models.event_impact_market import market_move, trade_days
from marden.readmodels.registry import builder
from marden.readmodels.views.debt_effects_text import (
    NO_CHANGE_PTS,
    NOT_READ,
    Doc,
    effect_of,
    effect_phrase,
    market_phrase,
    match,
    parse_day,
    signed,
    when,
)
from marden.readmodels.views.instrument_inputs import instrument_identity
from marden.readmodels.views.instrument_text import display_name
from marden.readmodels.views.sites_common import day, latest_output, site_slugs
from marden.readmodels.views.sites_tabs import evidence_records, site_event_log

NO_DEBT = "No priced debt is linked to this site."
NOT_YET = "Not computed yet: the event impact model has not run for this site."


def m_sourced(value: Any, as_of: str) -> Doc:
    return {"value": value, "tag": "M", "asOf": as_of, "sourceName": LABEL, "sourceUrl": METHOD_URL,
            "methodVersion": METHOD}


def names_of(conn: Conn, ids: Sequence[str]) -> dict[str, str]:
    return {r["id"]: display_name(r) for r in instrument_identity(conn, list(ids))} if ids else {}


def instrument_effect(e: Doc, ch: Doc, names: dict[str, str]) -> Doc:
    """InstrumentEffect (moves.ts): Sourced M values of one instrument's counterfactual."""
    at = ch["date"]
    out: Doc = {"instrumentId": e["instrumentId"], "name": names.get(e["instrumentId"], e["instrumentId"]),
                "date": at, "label": ch["label"], "without": m_sourced(e.get("without"), at),
                "with": m_sourced(e.get("with"), at), "change": m_sourced(e.get("changePts"), at),
                "yieldMeasure": e.get("measure") or "YTM", "yieldChange": None, "dmChangeBp": None,
                "note": None, "why": e.get("why")}
    if isinstance(e.get("yieldChangePct"), int | float):
        out["yieldChange"] = m_sourced(e["yieldChangePct"], at)
    if isinstance(e.get("dmWithBp"), int | float) and isinstance(e.get("dmWithoutBp"), int | float):
        out["dmChangeBp"] = m_sourced(round(e["dmWithBp"] - e["dmWithoutBp"], 1), at)
    stored, w = e.get("stored"), e.get("with")
    if isinstance(stored, int | float) and isinstance(w, int | float) and abs(stored - w) >= 0.01:
        out["note"] = (f"Recomputed with today's price model; the Marden price stored on {when(at)} was {stored:.1f} "
                       "(computed before later inputs or method changes).")
    return out


def market_doc(m: Doc, names: dict[str, str]) -> Doc:
    """MarketMove (moves.ts): the trades' own tag on the change."""
    tag = (m.get("after") or m.get("before") or {}).get("tag") or "F"
    as_of = (m.get("after") or {}).get("date") or day().isoformat()
    src = (m.get("after") or m.get("before") or {}).get("sourceName")
    change: Doc = {"value": m.get("changePts"), "tag": tag, "asOf": as_of}
    if src:
        change["sourceName"] = src
    return {"instrumentId": m["instrumentId"], "name": names.get(m["instrumentId"], m["instrumentId"]),
            "before": m.get("before"), "after": m.get("after"), "change": change, "why": m.get("why")}


def _collapse(hits: list[tuple[Doc, str | None]]) -> list[tuple[Doc, str | None]]:
    """One hit per change: a record that stands for several kinds of one change takes the whole change."""
    by: dict[str, list[str | None]] = {}
    order: dict[str, Doc] = {}
    for ch, k in hits:
        by.setdefault(ch["key"], []).append(k)
        order.setdefault(ch["key"], ch)
    return [(order[key], ks[0] if len(set(ks)) == 1 else None) for key, ks in by.items()]


def no_change(reason: str, market: list[Doc] | None = None, effects: list[Doc] | None = None) -> Doc:
    return {"changed": False, "line": "No change to debt value", "reason": reason, "effectiveDate": None,
            "label": None, "effects": effects or [], "market": market or [], "inputs": []}


def debt_effect(row: Doc, out: Doc, names: dict[str, str], market: list[Doc] | None = None) -> Doc:
    """DebtEffect (moves.ts) of one record."""
    ids = out.get("instruments") or []
    priced = {e["instrumentId"] for ch in out.get("changes") or [] for e in ch.get("effects") or []}
    if not ids:
        return no_change(NO_DEBT, market)
    hits, reason = match(row, out.get("changes") or [])
    if not hits:
        return no_change(reason or NOT_READ, market)
    effects: list[Doc] = []
    for ch, kind in _collapse(hits):
        for iid in ids:
            e = effect_of(ch, kind, iid)
            if e is not None and iid in priced:
                effects.append(instrument_effect(e, ch, names))
    moved = [e for e in effects if isinstance(e["change"]["value"], int | float)
             and abs(e["change"]["value"]) >= NO_CHANGE_PTS]
    main = hits[0][0]
    mk = [market_doc(m, names) for ch, _ in _collapse(hits) for m in ch.get("market") or []
          if isinstance(m.get("changePts"), int | float)]
    inputs = [r for ch, k in _collapse(hits) for r in ch.get("records") or [] if k is None or r.get("kind") == k]
    doc: Doc = {"changed": bool(moved), "effectiveDate": main["date"], "label": main["label"], "effects": effects,
                "market": mk[:6], "inputs": inputs[:8]}
    if moved:
        doc["line"] = "Effect on debt: " + "; ".join(
            effect_phrase(e["name"], _raw(e)) for e in sorted(moved, key=lambda e: e["change"]["value"])[:2])
        doc["reason"] = None
    else:
        doc["line"] = "No change to debt value"
        unpriced = [e["why"] for e in effects if e.get("why")]
        doc["reason"] = unpriced[0] if unpriced and len(unpriced) == len(effects) else (
            f"The price model read it ({main['label'][:1].lower() + main['label'][1:]}, {when(main['date'])}) and the "
            f"price moved less than {NO_CHANGE_PTS:g} points.")
    if mk and not moved:
        doc["reason"] += " Trades over the same days: " + "; ".join(
            market_phrase(m["name"], _mraw(m)) for m in mk[:2]) + "."
    return doc


def _raw(e: Doc) -> Doc:
    return {"changePts": e["change"]["value"], "without": e["without"]["value"], "with": e["with"]["value"],
            "yieldChangePct": (e.get("yieldChange") or {}).get("value"), "measure": e["yieldMeasure"]}


def _mraw(m: Doc) -> Doc:
    return {"changePts": m["change"]["value"], "before": m["before"], "after": m["after"]}


def row_key(row: Doc) -> str:
    return f"{row['date'][:10]}|{row.get('text') or ''}"


def related_ids(out: Doc) -> list[str]:
    return [r["instrumentId"] for r in out.get("related") or []]


def site_effects(conn: Conn, slug: str) -> Doc:
    """SiteDebtEffects: every Events-tab and Evidence-tab record of the site, keyed `date|text`."""
    row = latest_output(conn, "event_impact", "site", slug)
    out: Doc = row["output"] if row is not None and isinstance(row["output"], dict) else {}
    ids = list(out.get("instruments") or [])
    names = names_of(conn, ids + related_ids(out))
    default_reason = NOT_YET if row is None else (NOT_READ if ids else NO_DEBT)
    events = site_event_log(conn, slug)
    trades = trade_days(conn, ids + related_ids(out), day()) if row is not None else {}
    effects: dict[str, Doc] = {}
    for r in events:  # the material events: with the trades over the same days
        mk = [market_doc(m, names) for iid in ids + related_ids(out)
              if isinstance((m := market_move(iid, iid, trades.get(iid, []), parse_day(r["date"])))
                            .get("changePts"), int | float)]
        effects[row_key(r)] = no_change(NOT_YET, mk[:4]) if row is None else debt_effect(r, out, names, mk[:4])
    if row is not None:
        for r in evidence_records(conn, slug):
            key = row_key(r)
            if key not in effects:
                eff = debt_effect(r, out, names)
                if eff["changed"] or eff["reason"] != default_reason or eff["effects"]:
                    effects[key] = eff
    return {"asOf": day().isoformat(), "slug": slug, "default": no_change(default_reason), "effects": effects,
            "method": {"label": LABEL, "methodVersion": METHOD, "sourceUrl": METHOD_URL}}


def change_title(ch: Doc, kind: str | None = None) -> str:
    """A priced change as a headline: its first record (of the kind), else its label."""
    recs = [r for r in ch.get("records") or [] if kind is None or r.get("kind") == kind]
    if ch.get("kind") == "signals" and recs:
        return recs[0]["text"]
    return ch["label"]


def summary_pts(e: Doc) -> str:
    v = e["change"]["value"]
    return "n/d" if not isinstance(v, int | float) else f"{signed(v)} pts"


def output_row(conn: Conn, slug: str) -> Row | None:
    return latest_output(conn, "event_impact", "site", slug)


@builder("site_debt_effects", keys=site_slugs)
def build_site_debt_effects(conn: Conn, slug: str) -> Doc | None:
    if conn.execute("SELECT 1 FROM site WHERE slug = %s", (slug,)).fetchone() is None:
        return None
    return site_effects(conn, slug)
