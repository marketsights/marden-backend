"""Site, instrument and party facts, and coming-up events, from the case memos aiwc/memo/<CASE>.json
(field table: marden/seed/cases.py). The memo headline and the chain-party lists are loaded by the
`sites` loader; this module adds the status summary, tranches, security / structure terms (with
their verbatim quotes), open questions, scored issues and their next dated step, contractors, and
each party's roles across sites. Memo prices (`price`, `px_*`, price notes) are never written.
"""

from __future__ import annotations

from typing import Any

from marden.seed.cases_archive import MEMO, Archive, Parties
from marden.seed.cases_quarantine import PRICE_KEYS, scrub_all, scrub_text, strip
from marden.seed.cases_sink import EventSink, FactSink, basis_tag, first_url, is_nd, urls_in, usd_m, weakest
from marden.seed.common import parse_date

SEVERITY = {3: "red", 2: "amber"}
PRICE_TERMS = ("Fund mark",)  # a fund's mark is a price: loaded by the holdings loader, not here
_ISSUE_TEXT = ("what", "why", "impact", "title", "clears_if", "clears", "level_note")


def _tranches(sink: FactSink, arc: Archive, case: str, slug: str) -> None:
    memo, src = arc.get(MEMO, case), arc.src(MEMO.format(case))
    rows = []
    for inst in memo.get("instruments") or []:
        inst = strip(inst, PRICE_KEYS)
        ids = arc.known(inst.get("debt_row"))
        rows.append({
            "name": inst.get("name"), "borrower": inst.get("borrower"), "amount_usd": usd_m(inst.get("amt_m")),
            "terms": scrub_text(inst.get("terms")), "ranking": inst.get("ranking"), "holders": inst.get("holders"),
            "third_party": inst.get("third_party"), "modeled": inst.get("modeled"),
            "note": scrub_text(inst.get("note")), "instrument_id": ids[0] if ids else None,
        })
        for iid in ids:
            sink.add("instrument", iid, "research_terms", {k: v for k, v in rows[-1].items() if k != "instrument_id"},
                     src=src)
    sink.add("site", slug, "tranches", rows, src=src)
    summary = [{"tranche": c.get("tranche"), "issuer": c.get("issuer"), "seniority": c.get("rank"),
                "coupon": c.get("coupon"), "maturity": c.get("maturity"), "amount_usd": usd_m(c.get("amt_m")),
                "holders": c.get("holders"), "modeled": c.get("modeled"), "note": scrub_text(c.get("note"))}
               for c in (strip(c, PRICE_KEYS) for c in memo.get("cap") or [])]
    sink.add("site", slug, "tranche_summary", summary, src=src)


def _security(sink: FactSink, arc: Archive, case: str, slug: str, stats: dict[str, Any]) -> list[dict[str, Any]]:
    """structure_term facts; returns the terms the memo marks 'not disclosed' (open questions)."""
    memo, src = arc.get(MEMO, case), arc.src(MEMO.format(case))
    open_items: list[dict[str, Any]] = []
    for s in memo.get("security") or []:
        key, basis = s.get("k"), str(s.get("basis") or "")
        if key in PRICE_TERMS:
            stats["skipped"].setdefault("memo security term that is a price (holdings loader owns it)", []).append(
                f"{case}: {key}")
            continue
        if basis.strip().lower() == "not disclosed":
            open_items.append({"area": key, "item": s.get("v"), "short": s.get("s")})
            continue
        url = first_url(s.get("src"))
        sink.add("site", slug, "structure_term", {"key": key, "text": s.get("v"), "short": s.get("s"),
                                                  "basis": basis or None},
                 src=src, url=url, basis=basis or None, quote=s.get("q"))
    return open_items


def _issues(sink: FactSink, events: EventSink, arc: Archive, case: str, slug: str) -> None:
    memo, src = arc.get(MEMO, case), arc.src(MEMO.format(case))
    for i in memo.get("issues") or []:
        nxt = i.get("next") or {}
        value = scrub_all({k: i.get(k) for k in _ISSUE_TEXT})
        value.update({"driver": i.get("driver"), "category": i.get("cat"), "level": i.get("level"),
                      "level_suggest": i.get("level_suggest"), "next_date": nxt.get("date"),
                      "next_event": scrub_text(nxt.get("event")), "next_short": i.get("next_short"),
                      "sources": urls_in(i.get("src")) or None})
        url = first_url(i.get("src"))
        sink.add("site", slug, "issue", value, src=src, url=url, judged=True)
        day = parse_date(nxt.get("date"))
        title = scrub_text(nxt.get("event")) or scrub_text(i.get("title"))
        if day is not None and title:
            events.add("site", slug, day, "catalyst", f"{i.get('title')}: {title}" if i.get("title") else title,
                       src=src, url=url, severity=SEVERITY.get(i.get("level"), "none"))


def _open_questions(sink: FactSink, arc: Archive, case: str, slug: str, extra: list[dict[str, Any]]) -> None:
    memo, src = arc.get(MEMO, case), arc.src(MEMO.format(case))
    long, short = memo.get("unknowns") or [], memo.get("unknowns_s") or []
    items = [{"item": text, "short": short[n] if n < len(short) else None} for n, text in enumerate(long)]
    sink.add("site", slug, "open_questions", items + extra, src=src, judged=True)


def _parties(sink: FactSink, arc: Archive, parties: Parties, case: str, slug: str, stats: dict[str, Any]) -> None:
    memo, src = arc.get(MEMO, case), arc.src(MEMO.format(case))
    contractors = []
    for row in memo.get("chain") or []:
        name, basis = row.get("name"), row.get("basis")
        if is_nd(name):
            continue
        tag = basis_tag(basis)
        if row.get("role") == "Contractor":
            contractors.append(({"name": name, "what": row.get("what"), "basis": basis}, tag))
        eid = parties.find(name, ticker=row.get("ticker"))
        if eid is None:
            stats["unresolved_parties"] = stats.get("unresolved_parties", 0) + 1
            continue
        sink.add("entity", eid, "site_role", {"site_slug": slug, "case": case, "role": row.get("role"),
                                              "name": name, "what": row.get("what"), "basis": basis},
                 src=src, basis=basis)
    if contractors:
        sink.add("site", slug, "contractors", [c for c, _ in contractors], src=src,
                 tag=weakest([t for _, t in contractors]))


def write_memo(sink: FactSink, events: EventSink, arc: Archive, parties: Parties, case: str, slug: str,
               stats: dict[str, Any]) -> None:
    if not arc.has(MEMO, case):
        stats["skipped"].setdefault("no memo file", []).append(case)
        return
    src = arc.src(MEMO.format(case))
    sink.add("site", slug, "status_summary", scrub_text(arc.get(MEMO, case).get("status")), src=src, judged=True)
    _tranches(sink, arc, case, slug)
    open_items = _security(sink, arc, case, slug, stats)
    _issues(sink, events, arc, case, slug)
    _open_questions(sink, arc, case, slug, open_items)
    _parties(sink, arc, parties, case, slug, stats)
