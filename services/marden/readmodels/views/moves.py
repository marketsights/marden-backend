"""Read models `moves` (all) and `instrument_moves` (instrument.id): which V1 instruments' prices and yields moved
over 1D, 1W, 1M, QTD and YTD, from a trade or the Marden model, and what moved them (GET /api/moves,
GET /api/instruments/:id/moves; contract apps/api/src/contracts/moves.ts).

From the `moves` model's output per instrument (the two prices each window compares, their yields, the split
of a model move into priced events, rates and spreads) and the `event_impact` model's output of the sites
(the "What moved it" list of an instrument: each priced change on its site with its effect, and for a
tenant's or end user's bond the trades around the site's changes). Every number keeps its tag: a traded price
its trade's (TRACE prints are stored as filed, F), a Marden price, yield or split M.
"""

from __future__ import annotations

from typing import Any

from marden.core.db import Conn
from marden.models.moves import LABEL, METHOD, METHOD_URL
from marden.models.moves_windows import LABELS, WINDOWS, nominal_start
from marden.readmodels.context import known_facts
from marden.readmodels.registry import builder
from marden.readmodels.views.debt_effects import change_title, instrument_effect, market_doc, names_of
from marden.readmodels.views.debt_effects_text import Doc, signed, when
from marden.readmodels.views.instrument_inputs import all_instrument_ids, instrument_identity
from marden.readmodels.views.instrument_text import short_name, type_label
from marden.readmodels.views.instruments import size_b
from marden.readmodels.views.sites_common import day, latest_output

SIZE_FACTS = ("amount_usd", "amount_basis", "commitment_usd", "drawn_usd", "seniority")


def m_sourced(value: Any, as_of: str | None) -> Doc:
    return {"value": value, "tag": "M", "asOf": as_of or day().isoformat(), "sourceName": LABEL,
            "sourceUrl": METHOD_URL, "methodVersion": METHOD}


def price_change(w: Doc) -> Doc:
    """The price change with the tag of the prices compared: a trade's own, else M."""
    to = w.get("to") or {}
    if w.get("basis") == "trade" and to:
        out: Doc = {"value": w.get("changePts"), "tag": to.get("tag") or "F",
                    "asOf": to.get("date") or day().isoformat(),
                    "sourceName": to.get("sourceName") or "TRACE"}
        if to.get("sourceUrl"):
            out["sourceUrl"] = to["sourceUrl"]
        return out
    return m_sourced(w.get("changePts"), w.get("end"))


def site_href(slug: str | None) -> str | None:
    return f"/sites/{slug}?tab=events" if slug else None


def driver_doc(w: Doc) -> Doc:
    """MoveDriver: the main driver in words, its points and the split on hover."""
    d = w.get("driver") or {"kind": "none"}
    kind, pts = d.get("kind"), d.get("pts")
    out: Doc = {"kind": kind, "pts": pts, "text": "", "href": None, "detail": ""}
    events = w.get("events") or []
    rates = w.get("rates")
    total = w.get("changePts")
    if total is None:
        out.update({"kind": "none", "text": "n/d", "detail": w.get("why") or ""})
        return out
    parts = [f"{e['label']} ({when(e['date'])}): {signed(e['pts'])} pts" for e in events]
    if rates:
        parts.append(f"rates (Treasury {rates['years']:g}-year {signed(rates['treasuryBp'], 0)}bp): "
                     f"{signed(rates['pts'])} pts")
    elif w.get("basis") == "model":
        parts.append("rates: not split out (floating rate: the coupon resets with SOFR)")
    if kind == "event":
        e = next((x for x in events if x["key"] == d.get("key")), None)
        if e is not None:
            out.update({"text": f"{e['label']} ({when(e['date'])})", "href": site_href(e.get("siteSlug"))})
    elif kind == "rates" and rates:
        out["text"] = f"Rates: Treasury {rates['years']:g}-year yield {signed(rates['treasuryBp'], 0)}bp"
    elif kind == "spreads":
        out["text"] = "Spreads: the price model's credit inputs (tenant and issuer bond prices) and residual"
    elif kind == "trade":
        f, t = w["from"], w["to"]
        out["text"] = f"Trades: {f['price']:.1f} ({when(f['date'])}) → {t['price']:.1f} ({when(t['date'])})"
        if events:
            out["text"] += f"; priced events in the window: {signed(sum(e['pts'] for e in events))} pts"
    else:
        out["text"] = "No move"
    explained = sum(e["pts"] for e in events) + (rates["pts"] if rates else 0.0)
    if w.get("basis") == "model":
        parts.append(f"spreads and residual: {signed(total - explained)} pts")
    out["detail"] = f"Move {signed(total)} pts. " + "; ".join(parts) + "." if parts else f"Move {signed(total)} pts."
    return out


def window_doc(w: Doc, measure: str) -> Doc:
    """MoveWindow (moves.ts)."""
    end = w.get("end")
    out: Doc = {k: w.get(k) for k in ("key", "nominalStart", "start", "end", "partial", "basis", "from", "to", "why",
                                      "note")}
    out.update({"change": price_change(w), "yieldMeasure": measure,
                "yieldChangeBp": m_sourced(w.get("yieldChangeBp"), end),
                "dmChangeBp": m_sourced(w["dmChangeBp"], end) if w.get("dmChangeBp") is not None else None,
                "yieldWhy": w.get("yieldWhy"), "driver": driver_doc(w)})
    return out


def _empty_window(key: str, why: str) -> Doc:
    start = nominal_start(key, day()).isoformat()
    return {"key": key, "nominalStart": start, "start": None, "end": None, "partial": False, "basis": None,
            "from": None, "to": None, "why": why, "note": None, "change": m_sourced(None, None), "yieldMeasure": "YTM",
            "yieldChangeBp": m_sourced(None, None), "dmChangeBp": None, "yieldWhy": None,
            "driver": {"kind": "none", "pts": None, "text": "n/d", "href": None, "detail": why}}


def windows_of(out: Doc | None) -> dict[str, Doc]:
    if not out:
        return {k: _empty_window(k, "Not computed yet: the moves model has not run for it.") for k in WINDOWS}
    measure = out.get("measure") or "YTM"
    return {k: window_doc(out["windows"][k], measure) if k in (out.get("windows") or {}) else
            _empty_window(k, "Not computed for this window.") for k in WINDOWS}


def parties(conn: Conn, slug: str | None) -> dict[str, str | None]:
    if not slug:
        return {"tenant": None, "end_user": None}
    rows = conn.execute("SELECT role, label FROM chain_party WHERE site_slug = %s AND role IN ('tenant', 'end_user')",
                        (slug,)).fetchall()
    got = {r["role"]: r["label"] for r in rows if r["label"] not in (None, "", "—", "n/d")}
    return {"tenant": got.get("tenant"), "end_user": got.get("end_user")}


def move_row(conn: Conn, inst: Doc) -> Doc:
    """MoveRow (moves.ts)."""
    row = latest_output(conn, "moves", "instrument", inst["id"])
    out = row["output"] if row is not None and isinstance(row["output"], dict) else None
    facts = known_facts(conn, "instrument", inst["id"], SIZE_FACTS)
    sen = facts.get("seniority")
    who = parties(conn, inst["site_slug"])
    return {
        "instrumentId": inst["id"], "name": names_of(conn, [inst["id"]]).get(inst["id"], inst["name"]),
        "type": type_label(inst, sen["value"] if sen else None), "sizeB": size_b(facts),
        "issuer": short_name(inst.get("parent_name") or inst.get("issuer_entity") or inst["issuer_name"]) or "",
        "site": {"slug": inst["site_slug"], "name": short_name(inst["site_name"]) or inst["site_name"]}
        if inst["site_slug"] else None,
        "tenant": who["tenant"], "endUser": who["end_user"],
        "couponType": (out or {}).get("couponType"), "lastTrade": (out or {}).get("lastTrade"),
        "windows": windows_of(out),
    }


def history_note(rows: list[Doc], outs: list[Doc]) -> Doc:
    model_from = min((o["modelFrom"] for o in outs if o.get("modelFrom")), default=None)
    trades_from = min((o["tradesFrom"] for o in outs if o.get("tradesFrom")), default=None)
    note = ("Marden daily prices are on record from " + (when(model_from) if model_from else "no date yet") +
            "; TRACE trades from " + (when(trades_from) if trades_from else "no date yet") +
            ". A window that starts earlier compares from the first price on record and shows its real start date.")
    return {"modelFrom": model_from, "tradesFrom": trades_from, "note": note}


@builder("moves", many=True)
def build_moves(conn: Conn):
    """GET /api/moves: one row per V1 instrument."""
    insts = instrument_identity(conn, v1_only=True)
    rows = [move_row(conn, dict(i)) for i in insts]
    outs = [r["output"] for i in insts if (r := latest_output(conn, "moves", "instrument", i["id"])) is not None]
    windows = [{"key": k, "label": LABELS[k], "nominalStart": nominal_start(k, day()).isoformat()} for k in WINDOWS]
    yield "all", {"asOf": day().isoformat(), "windows": windows, "history": history_note(rows, outs), "rows": rows,
                  "method": {"label": LABEL, "methodVersion": METHOD, "sourceUrl": METHOD_URL}}


# ---------------------------------------------------------------- instrument: windows and what moved it
def what_moved(conn: Conn, iid: str, slug: str | None) -> list[Doc]:
    """WhatMovedRow[]: the priced changes on its site with their effect on it, newest first; and for a bond of a
    site's tenant or end user, its trades around each priced change on that site."""
    rows: list[Doc] = []
    outs = conn.execute(
        """SELECT DISTINCT ON (subject_id) subject_id, output FROM model_output
            WHERE model = 'event_impact' AND subject_type = 'site' AND as_of <= %s
            ORDER BY subject_id, as_of DESC, created_at DESC, id DESC""", (day(),)).fetchall()
    for r in outs:
        doc = r["output"] if isinstance(r["output"], dict) else {}
        mine = r["subject_id"] == slug or iid in (doc.get("instruments") or [])
        related = next((x for x in doc.get("related") or [] if x["instrumentId"] == iid), None)
        if not mine and related is None:
            continue
        names = names_of(conn, [iid])
        for ch in doc.get("changes") or []:
            eff = next((e for e in ch.get("effects") or [] if e["instrumentId"] == iid), None)
            mk = next((m for m in ch.get("market") or [] if m["instrumentId"] == iid
                       and isinstance(m.get("changePts"), int | float)), None)
            if mine and eff is not None:
                rows.append({"date": ch["date"], "kind": "event", "title": change_title(ch), "label": ch["label"],
                             "href": site_href(r["subject_id"]), "effect": instrument_effect(eff, ch, names),
                             "market": market_doc(mk, names) if mk else None, "inputs": (ch.get("records") or [])[:6],
                             "note": None})
            elif related is not None and mk is not None:
                site = conn.execute("SELECT name FROM site WHERE slug = %s", (r["subject_id"],)).fetchone()
                rows.append({"date": ch["date"], "kind": "market",
                             "title": f"{short_name(site['name']) if site else r['subject_id']}: {change_title(ch)}",
                             "label": ch["label"], "href": site_href(r["subject_id"]), "effect": None,
                             "market": market_doc(mk, names), "inputs": (ch.get("records") or [])[:6],
                             "note": f"The price model prices this {related['party']} bond from its own trades; the "
                                     "site's records are not an input to it, so only the market move is shown."})
    rows.sort(key=lambda x: (x["date"], x["kind"] == "event"), reverse=True)
    return rows


@builder("instrument_moves", keys=all_instrument_ids)
def build_instrument_moves(conn: Conn, iid: str) -> Doc | None:
    insts = instrument_identity(conn, [iid])
    if not insts:
        return None
    inst = dict(insts[0])
    row = latest_output(conn, "moves", "instrument", iid)
    out = row["output"] if row is not None and isinstance(row["output"], dict) else None
    return {"instrumentId": iid, "asOf": day().isoformat(), "windows": windows_of(out),
            "lastTrade": (out or {}).get("lastTrade"), "measure": (out or {}).get("measure") or "YTM",
            "couponType": (out or {}).get("couponType"), "whatMoved": what_moved(conn, iid, inst["site_slug"]),
            "method": {"label": LABEL, "methodVersion": METHOD, "sourceUrl": METHOD_URL}}
