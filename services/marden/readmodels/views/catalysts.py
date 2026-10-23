"""The catalyst table both monitor screens lead with (Mispriced, Delivery record): per credit, the newest catalyst,
its expected effect on the price, its timing, and how far the evidence goes, with links to the documents.

- Mispriced (`mispriced_catalysts`): per instrument, the event since its last market price that the event-impact
  model says moves it most (else the newest red record, else the newest record); expected impact = that model
  effect (M), with the instrument's whole gap to the market alongside; timing = the event day, the days the market
  has had and whether it has traded since, and the next dated catalyst on file (a delivery window, a maturity).
- Delivery record (`delivery_catalysts`): per site with a schedule push in the window, the newest push; expected
  impact = the event-impact model's effect of the projected-completion change that day on each of the site's
  instruments (M); timing = the push day and the current target; evidence = the push's record and the records of
  the dates it compares.

Evidence levels: a filed document (F) is the strongest, a press or third-party report (R) next, a Marden model
output (M) is worked from the records it links. Every row lists up to four documents (source, date, link).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from marden.core.db import Conn
from marden.readmodels.views.instrument_inputs import day, num
from marden.readmodels.views.sites_common import latest_output

Doc = dict[str, Any]
LEVEL = {"F": "Filed document", "R": "Reported (press or third party)", "M": "Marden model, from the linked records"}
MAX_DOCS = 4


def documents(*sources: Doc | None) -> list[Doc]:
    """Distinct linked records, in the order given: {label, url, tag, date}."""
    out: list[Doc] = []
    seen: set[str] = set()
    for s in sources:
        url = (s or {}).get("sourceUrl")
        if not s or not url or url in seen or not str(url).startswith("http"):
            continue
        seen.add(url)
        out.append(
            {
                "label": s.get("sourceName") or "Source",
                "url": url,
                "tag": s.get("tag"),
                "date": s.get("date") or s.get("asOf"),
            }
        )
    return out[:MAX_DOCS]


def evidence(src: Doc, docs: list[Doc], note: str) -> Doc:
    tag = src.get("tag") if src.get("tag") in LEVEL else "M"
    out: Doc = {
        "tag": tag,
        "level": LEVEL[tag],
        "sourceName": src.get("sourceName") or "Marden",
        "documents": docs,
        "note": note,
    }
    if src.get("sourceUrl"):
        out["sourceUrl"] = src["sourceUrl"]
    return out


def impact(pts: float | None, as_of: str, method: str, note: str | None) -> Doc:
    value = None if pts is None else round(pts, 2)
    text = (
        f"{'+' if value > 0 else ''}{value:.1f} pts"
        if value is not None and abs(value) >= 0.05
        else "Under 0.1 pts"
        if value is not None
        else None
    )
    return {
        "pts": {
            "value": value,
            "tag": "M",
            "asOf": as_of,
            "sourceName": "Marden event impact model",
            "sourceUrl": "docs/methods/event-impact-v0.1.md",
            "methodVersion": method,
        },
        "text": text,
        "note": note,
    }


def next_catalyst(conn: Conn, subjects: list[tuple[str, str]]) -> Doc | None:
    """The earliest dated catalyst after the build day on these subjects (a delivery window, a maturity)."""
    if not subjects:
        return None
    row = conn.execute(
        """SELECT event_date, title, tag, source_name, source_url FROM event
            WHERE status = 'published' AND kind = 'catalyst' AND event_date > %(day)s
              AND (subject_type, subject_id) IN (SELECT * FROM unnest(%(t)s::text[], %(i)s::text[]))
            ORDER BY event_date, id LIMIT 1""",
        {"day": day(), "t": [s[0] for s in subjects], "i": [s[1] for s in subjects]},
    ).fetchone()
    if row is None:
        return None
    out: Doc = {
        "date": row["event_date"].isoformat(),
        "text": row["title"],
        "tag": str(row["tag"]).strip(),
        "sourceName": row["source_name"],
    }
    if row["source_url"]:
        out["sourceUrl"] = row["source_url"]
    return out


# ---------------------------------------------------------------- Mispriced
def mispriced_catalysts(conn: Conn, rows: list[Doc], insts: dict[str, Any]) -> list[Doc]:
    out: list[Doc] = []
    for r in rows:
        inst = insts.get(r["instrumentId"])
        subjects = [("instrument", r["instrumentId"])] + ([("site", r["site"]["slug"])] if r.get("site") else [])
        nxt = next_catalyst(conn, subjects)
        ev = r.get("driver")
        if ev is None and nxt is None:
            continue
        gap = r["gapPts"]["value"]
        cat = (
            {"date": ev["date"], "text": ev["text"], "kind": ev["kind"], "href": ev.get("href")}
            if ev
            else {"date": nxt["date"], "text": nxt["text"], "kind": "upcoming", "href": None}
        )
        eff = impact(
            ev["effectPts"] if ev else None,
            day().isoformat(),
            "event_impact v0.1",
            (ev or {}).get("effectNote")
            if ev and ev["effectPts"] is None
            else None
            if ev
            else "A dated catalyst still ahead: no effect is priced before it happens",
        )
        src = ev or nxt or {}
        docs = documents(*(r.get("events") or []), r.get("market"), nxt)
        out.append(
            {
                "credit": {"instrumentId": r["instrumentId"], "name": r["name"], "href": r["href"]},
                "site": r.get("site"),
                "companies": [inst["issuer_name"]] if inst else [],
                "catalyst": cat,
                "impact": eff,
                "gapPts": r["gapPts"],
                "groups": r["groups"],
                "timing": {
                    "date": cat["date"],
                    "daysSince": r.get("daysSinceEvent") if ev else None,
                    "marketSince": r["confidence"],
                    "next": nxt if ev else None,
                },
                "evidence": evidence(src, docs, r["confidence"]),
                "sort": abs((ev or {}).get("effectPts") or 0) * 10 + abs(gap or 0) * (num(r["sizeB"]["value"]) or 0),
            }
        )
    out.sort(key=lambda c: -c["sort"])
    return out


# ---------------------------------------------------------------- Delivery record
def _site_instruments(conn: Conn, slug: str) -> list[Doc]:
    rows = conn.execute(
        "SELECT id, name, issuer_name FROM instrument WHERE site_slug = %s AND in_v1 ORDER BY id", (slug,)
    ).fetchall()
    return [dict(r) for r in rows]


def _effects_on(conn: Conn, slug: str, when: str) -> tuple[list[Doc], str | None]:
    """This day's priced changes on the site: each instrument's model effect, and the change's label."""
    out = latest_output(conn, "event_impact", "site", slug)
    for ch in ((out["output"] or {}).get("changes") if out is not None else None) or []:
        if ch.get("date") == when:
            return [e for e in ch.get("effects") or [] if isinstance(e, dict)], ch.get("label")
    return [], None


def delivery_catalysts(conn: Conn, sites: list[Doc], companies: dict[str, list[str]], within: Any) -> list[Doc]:
    out: list[Doc] = []
    for s in sites:
        recent = [p for p in s["pushes"] if within(p)]
        if not recent:
            continue
        push = recent[0]
        effects, label = _effects_on(conn, s["slug"], push["date"])
        insts = _site_instruments(conn, s["slug"])
        names = {i["id"]: i["name"] for i in insts}
        slipped = [i for i in s["items"] if i["outcome"] == "open_late" and i["slipMonths"]]
        worst = max(slipped, key=lambda i: i["slipMonths"], default=None)
        priced = [e for e in effects if num(e.get("changePts")) is not None]
        total = sum(num(e["changePts"]) or 0 for e in priced) / len(priced) if priced else None
        note = None
        if not insts:
            note = "No V1 instrument on this site to price"
        elif not priced:
            note = (
                "The price model gives this push no effect that day"
                if effects
                else "Not a change the price model reads (a record, not a projected completion move)"
            )
        target = (worst or {}).get("current")
        docs = documents(push, *(x for i in slipped for x in (i.get("first"), i.get("current"))))
        detail = [f"{names.get(e['instrumentId'], e['instrumentId'])} {num(e['changePts']):+.2f} pts" for e in priced]
        credit = insts[0] if insts else None
        out.append(
            {
                "credit": {
                    "instrumentId": credit["id"] if credit else None,
                    "name": (f"{s['name']} debt" if len(insts) != 1 else f"{s['name']}: {credit['name']}")
                    if insts
                    else f"{s['name']} (no V1 instrument)",
                    "href": f"/instruments/{credit['id']}" if credit and len(insts) == 1 else s["href"],
                },
                "site": {"slug": s["slug"], "name": s["name"]},
                "companies": companies.get(s["slug"], []),
                "catalyst": {"date": push["date"], "text": push["text"], "kind": "push", "href": push["href"]},
                "impact": {
                    **impact(total, day().isoformat(), "event_impact v0.1", note),
                    "detail": detail,
                    "label": label,
                },
                "timing": {
                    "date": push["date"],
                    "daysSince": (day() - date.fromisoformat(push["date"][:10])).days,
                    "newTarget": target,
                    "slipMonths": (worst or {}).get("slipMonths"),
                    "pushes": len(recent),
                },
                "evidence": evidence(push, docs, f"{len(recent)} push{'es' if len(recent) != 1 else ''} in the window"),
                "sort": abs(total or 0) * 10 + (worst or {}).get("slipMonths", 0) + len(recent) / 10,
            }
        )
    out.sort(key=lambda c: -c["sort"])
    return out
