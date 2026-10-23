"""What the Mispriced screen compares for one instrument (readmodels/views/mispriced.py; method
docs/methods/mispriced-v0.1.md): the latest market evidence, the funds' filed marks, and the events dated after
that evidence.

- Market evidence: the newest TRACE trade, exchange quote (Börse Frankfurt, BondTerminal) or reported dealer
  quote observed by the build day; without any, the funds' filed marks (par-weighted over the holders of the
  `mark_gap` output, dated by the newest filing). The tag stays the record's own (TRACE F, quotes R, filings F).
- Events since: each priced change on the instrument's site after the evidence date (the `event_impact` model's
  effect on this instrument, M), and the site's, the instrument's and its issuer's records after that date
  (filings, rating actions, permits, opposition, notices, and news the event pipeline marked red, amber or
  green). For a tenant's or end user's own bond, the records of the sites it leases (tenant news): the price model
  prices that bond from its own trades, so those have no model effect.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.views.debt_effects import change_title
from marden.readmodels.views.instrument_inputs import _known_by_clause, day, iso, num, price_rows
from marden.readmodels.views.sites_common import latest_output

Doc = dict[str, Any]
MARKET_KINDS = ("trade", "exchange", "dealer")
KIND_RANK = {"trade": 0, "exchange": 1, "dealer": 2}
RECORD_KINDS = ("filing", "rating", "permit", "opposition", "notice", "news", "interconnection", "queue")
NEWS_SEVERITIES = ("red", "amber", "green")
RELATED_SITES = """SELECT DISTINCT cp.site_slug FROM chain_party cp JOIN entity e ON e.id = cp.entity_id
                    WHERE cp.role IN ('tenant', 'end_user') AND (e.id = %(e)s OR e.id = %(p)s)"""


def evidence_doc(row: Row) -> Doc:
    kind = "quote" if row["kind"] == "exchange" else row["kind"]
    out: Doc = {
        "kind": kind,
        "price": round(float(row["price"]), 3),
        "date": iso(row["obs_date"]),
        "tag": str(row["tag"]).strip(),
        "sourceName": row["source_name"],
    }
    if row.get("source_url"):
        out["sourceUrl"] = row["source_url"]
    return out


def market_rows(conn: Conn, iid: str) -> list[Row]:
    return [r for r in price_rows(conn, iid, MARKET_KINDS) if num(r["price"]) is not None]


def latest_market(rows: list[Row]) -> Doc | None:
    """The newest observation; on one day a trade before a quote before a dealer quote."""
    if not rows:
        return None
    best = max(rows, key=lambda r: (r["obs_date"], -KIND_RANK.get(r["kind"], 9)))
    same_day = [r for r in rows if r["obs_date"] == best["obs_date"] and r["kind"] == best["kind"]]
    out = evidence_doc(same_day[-1])
    if best["kind"] == "trade":
        out["prints"] = len(same_day)
    return out


def latest_trade(rows: list[Row]) -> Doc | None:
    trades = [r for r in rows if r["kind"] in ("trade", "exchange")]
    return evidence_doc(trades[-1]) if trades else None


def trade_days_after(rows: list[Row], after: str) -> int:
    return len({r["obs_date"] for r in rows if r["kind"] == "trade" and iso(r["obs_date"]) > after})


def fund_marks(conn: Conn, iid: str) -> Doc | None:
    """Par-weighted last filed mark of the holders (the `mark_gap` output), with the newest filing behind it."""
    out = latest_output(conn, "mark_gap", "instrument", iid)
    holders = [
        h
        for h in ((out["output"] or {}).get("holders") if out is not None else None) or []
        if isinstance(h, dict) and num(h.get("lastMark")) is not None and (num(h.get("parMm")) or 0) > 0
    ]
    if not holders:
        return None
    par = sum(num(h["parMm"]) or 0 for h in holders)
    mark = sum((num(h["lastMark"]) or 0) * (num(h["parMm"]) or 0) for h in holders) / par
    newest = max(holders, key=lambda h: str(h.get("filingDate") or ""))
    doc: Doc = {
        "kind": "fund_mark",
        "price": round(mark, 3),
        "date": str(newest.get("filingDate") or ""),
        "tag": "F",
        "sourceName": f"{len(holders)} funds' filed marks (par-weighted; newest {newest.get('filing') or 'filing'}, "
        f"{newest.get('name')})",
        "holders": len(holders),
        "parMm": round(par, 1),
    }
    if newest.get("filingUrl"):
        doc["sourceUrl"] = newest["filingUrl"]
    return doc


# ---------------------------------------------------------------- events since
def _event(
    when: str,
    kind: str,
    text: str,
    src: Doc,
    href: str | None,
    effect: float | None,
    note: str | None,
    severity: str | None = None,
) -> Doc:
    out: Doc = {
        "date": when,
        "kind": kind,
        "text": text,
        "tag": src.get("tag") or "M",
        "sourceName": src.get("sourceName") or "Marden",
        "href": href,
        "effectPts": effect,
        "effectNote": note,
        "severity": severity,
    }
    if src.get("sourceUrl"):
        out["sourceUrl"] = src["sourceUrl"]
    return out


def impact_events(conn: Conn, iid: str, slug: str, since: str) -> list[Doc]:
    """Priced changes on the site after `since`, with this instrument's model effect."""
    out = latest_output(conn, "event_impact", "site", slug)
    found: list[Doc] = []
    for ch in ((out["output"] or {}).get("changes") if out is not None else None) or []:
        if not isinstance(ch, dict) or str(ch.get("date") or "") <= since:
            continue
        eff = next((e for e in ch.get("effects") or [] if e.get("instrumentId") == iid), None)
        pts = num((eff or {}).get("changePts"))
        rec = (ch.get("records") or [{}])[0]
        kind = {"completion": "slip", "signals": "signal", "quote": "quote"}.get(str(ch.get("kind")), "signal")
        if kind == "slip" and not ch.get("from"):
            continue  # the first projection is not news
        note = None if pts is not None else ((eff or {}).get("why") or "The price model gives this change no effect")
        src = {
            "tag": rec.get("tag") or "M",
            "sourceName": rec.get("sourceName") or "Marden event impact model",
            "sourceUrl": rec.get("sourceUrl"),
        }
        found.append(_event(ch["date"], kind, change_title(ch), src, f"/sites/{slug}?tab=events", pts, note))
    return found


def record_events(conn: Conn, subjects: list[tuple[str, str]], since: str, kind_label: str | None = None) -> list[Doc]:
    """Published records of these subjects dated after `since` and by the build day."""
    if not subjects:
        return []
    sql = (
        "SELECT subject_type, subject_id, event_date, kind, title, severity, material, tag, source_name, source_url"
        " FROM event WHERE status = 'published'"
        " AND (subject_type, subject_id) IN (SELECT * FROM unnest(%(t)s::text[], %(i)s::text[]))"
        " AND event_date > %(since)s AND event_date <= %(day)s AND kind = ANY(%(kinds)s)"
        + _known_by_clause()
        + " ORDER BY event_date DESC, id DESC"
    )
    rows = conn.execute(
        sql,
        {
            "t": [s[0] for s in subjects],
            "i": [s[1] for s in subjects],
            "since": since,
            "day": day(),
            "kinds": list(RECORD_KINDS),
        },
    ).fetchall()
    out: list[Doc] = []
    for r in rows:
        if r["kind"] == "news" and r["severity"] not in NEWS_SEVERITIES and not r["material"]:
            continue
        kind = kind_label or ("material" if r["material"] else r["kind"])
        href = f"/sites/{r['subject_id']}?tab=events" if r["subject_type"] == "site" else None
        src = {"tag": str(r["tag"]).strip(), "sourceName": r["source_name"], "sourceUrl": r["source_url"]}
        out.append(
            _event(
                iso(r["event_date"]),
                kind,
                r["title"],
                src,
                href,
                None,
                "Not an input the price model reads",
                r["severity"],
            )
        )
    return out


SEVERITY_RANK = {"red": 0, "amber": 1, "green": 2}
KIND_ORDER = {"slip": 0, "signal": 0, "quote": 0, "rating": 1, "material": 1, "permit": 2, "opposition": 2, "notice": 2}


def importance(e: Doc) -> tuple:
    """Priced effects first (largest first), then red, amber and green records, ratings and permits before news
    and filings; newest first within each."""
    pts = e["effectPts"]
    return (
        pts is None,
        -abs(pts or 0),
        SEVERITY_RANK.get(e.get("severity") or "", 3),
        KIND_ORDER.get(e["kind"], 3),
        "".join(chr(255 - ord(c)) for c in e["date"]),
    )


def events_since(conn: Conn, inst: Row, since: str) -> list[Doc]:
    """Every event after the market evidence date, most important first (`importance`); one line per title and day."""
    iid, slug = inst["id"], inst["site_slug"]
    found = impact_events(conn, iid, slug, since) if slug else []
    subjects = [("instrument", iid)] + ([("site", slug)] if slug else [])
    if inst.get("issuer_entity_id"):
        subjects.append(("entity", inst["issuer_entity_id"]))
    found += record_events(conn, subjects, since)
    if not slug and inst.get("issuer_entity_id"):
        rel = conn.execute(
            RELATED_SITES.replace("SELECT DISTINCT cp.site_slug", "SELECT DISTINCT cp.site_slug, s.name").replace(
                "FROM chain_party cp", "FROM chain_party cp JOIN site s ON s.slug = cp.site_slug"
            ),
            {"e": inst["issuer_entity_id"], "p": inst.get("parent_entity_id")},
        ).fetchall()
        names = {r["site_slug"]: r["name"] for r in rel}
        own = "The price model prices this debt from its issuer's own credit, not from the site"
        grouped: dict[tuple[str, str], list[Doc]] = {}
        for ev in record_events(conn, [("site", s) for s in names], since, "tenant_news"):
            grouped.setdefault((ev["date"], ev["text"]), []).append(ev)
        for evs in grouped.values():  # one line per story, however many of the tenant's sites carry it
            slug = (evs[0]["href"] or "").split("/sites/")[-1].split("?")[0]
            where = str(names.get(slug) or slug) + (f" and {len(evs) - 1} more sites" if len(evs) > 1 else "")
            found.append({**evs[0], "text": f"{where}: {evs[0]['text']}"})
        for s, name in names.items():
            for ev in impact_events(conn, iid, s, since):
                found.append(
                    {**ev, "kind": "tenant_news", "text": f"{name}: {ev['text']}", "effectPts": None, "effectNote": own}
                )
    seen: set[tuple[str, str]] = set()
    out: list[Doc] = []
    for ev in sorted(found, key=importance):
        k = (ev["date"], ev["text"][:80].lower())
        if k not in seen:
            seen.add(k)
            out.append(ev)
    return out


def days_since(when: str) -> int | None:
    try:
        return (day() - date.fromisoformat(when[:10])).days
    except ValueError:
        return None
