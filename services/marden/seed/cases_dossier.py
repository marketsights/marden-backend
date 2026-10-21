"""Site and instrument facts from the research dossiers, aiwc/cases/<CASE>.json (field table:
marden/seed/cases.py). Site, capacity and lease blocks are loaded by the `sites` loader; this
module loads the rest: debt rows, collateral and reserves, credit support, parties and ratings,
cost, risks with their dated evidence, watched sources, covenants, facility and deal metrics.
Bond prices quoted in the dossier (`parties.tenant_bond`, `parties.sponsor_bonds`, `debt[].price`)
come from finra.org TRACE or Börse Frankfurt and are never written.
"""

from __future__ import annotations

import re
from typing import Any

from marden.seed.cases_archive import DOSSIER, Archive
from marden.seed.cases_quarantine import PRICE_KEYS, scrub_text, strip
from marden.seed.cases_sink import FactSink, first_url, is_nd, is_paraphrase, usd_m, weakest
from marden.seed.common import tag_for_url

DRIVERS = ("power", "permits", "water", "construction", "tenant", "sponsor", "other")
PARTY_FIELDS = ("sponsor", "guarantor", "lenders", "fund_holders", "sponsor_liquidity", "sponsor_note")
RATING_FIELDS = ("tenant_rating", "sponsor_rating", "deal_rating")  # ratings are reported values (spec 22)
BLOCKED_PARTY_FIELDS = ("tenant_bond", "sponsor_bonds")  # bond prices from TRACE / Frankfurt
_ID = re.compile(r"\b(?:EX|NP|FWP|MAN|BDC|XB|8K)-[A-Za-z0-9_]+-[A-Za-z0-9.]+")
_MORE = re.compile(r"^(?:\s*(?:,|/|and)\s*-?(\d{3})|\.\.(\d{3}))")


def instrument_refs(arc: Archive, refs: list[Any]) -> list[str]:
    """Instrument ids named by `local:aidebt.json row ...` pointers, with their shorthand
    ('EX-CLOUDHQ-034, -035, -036', 'EX-DATABANK-038..041', 'EX-VANTAGE_ORCL-366/367')."""
    found: list[str] = []
    for ref in refs:
        if not isinstance(ref, str) or not ref.startswith("local:"):
            continue
        for m in _ID.finditer(ref):
            iid = m.group(0).rstrip(".")
            found.append(iid)
            prefix, rest = iid.rsplit("-", 1)[0], ref[m.end():]
            last = int(iid.rsplit("-", 1)[1]) if iid.rsplit("-", 1)[1].isdigit() else None
            while last is not None and (more := _MORE.match(rest)):
                nxt = [int(more.group(1))] if more.group(1) else list(range(last + 1, int(more.group(2)) + 1))
                found += [f"{prefix}-{n:03d}" for n in nxt]
                last, rest = nxt[-1], rest[more.end():]
    return arc.known(found)


def _debt_item(arc: Archive, d: dict[str, Any]) -> tuple[dict[str, Any], str, list[str]]:
    d = strip(d, PRICE_KEYS)
    url = first_url(d.get("src"))
    ids = instrument_refs(arc, d.get("src") or [])
    item = {
        "name": d.get("inst"), "issuer": d.get("issuer"), "amount_usd": usd_m(d.get("amt_m")),
        "amount_asof": d.get("amt_asof"), "amount_note": d.get("amt_note"), "coupon_pct": d.get("coupon_pct"),
        "coupon": d.get("coupon"), "maturity": d.get("maturity"), "amortization": d.get("amort"),
        "rank": d.get("rank"), "rating": d.get("rating"), "drawn_usd": usd_m(d.get("drawn_m")),
        "drawn_asof": d.get("drawn_asof"), "commitment_usd": usd_m(d.get("commitment_m")),
        "holders": d.get("holders"), "covenants": d.get("covenants"), "note": scrub_text(d.get("note")),
        "instrument_ids": ids or None, "source_url": url,
        "quote": None if is_paraphrase(d.get("quote")) else d.get("quote"),
    }
    tag = "M" if d.get("amt_inferred") else tag_for_url(url)
    return item, tag, ids


def _debt(sink: FactSink, arc: Archive, case: str, slug: str) -> None:
    src, rows = arc.src(DOSSIER.format(case)), arc.get(DOSSIER, case).get("debt") or []
    items = [_debt_item(arc, d) for d in rows]
    if items:
        sink.add("site", slug, "debt_items", [i for i, _, _ in items], src=src, tag=weakest([t for _, t, _ in items]))
    # The row's quote covers the row as a whole, so it stays in debt_items, not on each derived field.
    for d, (item, tag, ids) in zip(rows, items, strict=True):
        url = item.get("source_url")
        for iid in ids:
            sink.add("instrument", iid, "amortization", d.get("amort"), src=src, url=url, tag=tag)
            sink.add("instrument", iid, "ranking_text", d.get("rank"), src=src, url=url, tag=tag)
            sink.add("instrument", iid, "holders_text", d.get("holders"), src=src, url=url, tag=tag)
            sink.add("instrument", iid, "covenants_text", d.get("covenants"), src=src, url=url, tag=tag)
            sink.add("instrument", iid, "drawn_usd", usd_m(d.get("drawn_m")), src=src, url=url,
                     unit="USD", valid_from=d.get("drawn_asof"), tag=tag)
            sink.add("instrument", iid, "commitment_usd", usd_m(d.get("commitment_m")), src=src, url=url,
                     unit="USD", tag=tag)


def _collateral(sink: FactSink, arc: Archive, case: str, slug: str) -> None:
    src = arc.src(DOSSIER.format(case))
    col = arc.get(DOSSIER, case).get("collateral") or {}
    url, quote = first_url(col.get("src")), col.get("quote")
    value = {"description": col.get("desc"), "excludes_gpus": col.get("excludes_gpus"),
             "excludes_gpus_note": col.get("excludes_gpus_note"), "inferred_gpus": col.get("inferred_gpus")}
    if not is_nd(col.get("desc")):
        sink.add("site", slug, "collateral", value, src=src, url=url, quote=quote)
    reserves = " ".join(x for x in (col.get("reserves"), col.get("reserves_note")) if isinstance(x, str) and x.strip())
    sink.add("site", slug, "reserves", reserves, src=src, url=url, quote=quote)
    for cs in arc.get(DOSSIER, case).get("credit_support") or []:
        url = first_url(cs.get("src"))
        sink.add("site", slug, "credit_support",
                 {"type": cs.get("type"), "provider": cs.get("provider"), "amount_usd": usd_m(cs.get("amount_m")),
                  "covers": cs.get("covers"), "inferred": True if cs.get("inferred") else None},
                 src=src, url=url, quote=cs.get("quote"), tag="M" if cs.get("inferred") else None)


def _parties(sink: FactSink, arc: Archive, case: str, slug: str, stats: dict[str, Any]) -> None:
    src = arc.src(DOSSIER.format(case))
    parties = arc.get(DOSSIER, case).get("parties") or {}
    url = first_url(parties.get("src"))
    for fld in PARTY_FIELDS:
        sink.add("site", slug, fld, scrub_text(parties.get(fld)) if isinstance(parties.get(fld), str)
                 else parties.get(fld), src=src, url=url)
    for fld in RATING_FIELDS:
        sink.add("site", slug, fld, parties.get(fld), src=src, url=url, tag="R")
    for fld in BLOCKED_PARTY_FIELDS:
        if not is_nd(parties.get(fld)):
            stats["skipped"].setdefault("bond price text (TRACE / Frankfurt) not loaded", []).append(f"{case}.{fld}")


def _cost(sink: FactSink, arc: Archive, case: str, slug: str) -> None:
    src = arc.src(DOSSIER.format(case))
    cost = arc.get(DOSSIER, case).get("cost") or {}
    url, quote, tag = first_url(cost.get("src")), cost.get("quote"), "M" if cost.get("inferred") else None
    for fld, value, unit in (
        ("total_project_cost_usd", usd_m(cost.get("total_project_cost_m")), "USD"),
        ("cost_to_complete_usd", usd_m(cost.get("cost_to_complete_m")), "USD"),
        ("cost_overrun", cost.get("overrun"), None), ("cost_note", scrub_text(cost.get("note")), None),
        ("cost_basis", cost.get("cost_basis"), None), ("advance_rates", cost.get("advance_rates"), None),
    ):
        sink.add("site", slug, fld, value, src=src, url=url, quote=quote, unit=unit, tag=tag)


def _risks(sink: FactSink, arc: Archive, case: str, slug: str) -> None:
    src = arc.src(DOSSIER.format(case))
    risks = arc.get(DOSSIER, case).get("risks") or {}
    drivers = (arc.credit_cases.get(case) or {}).get("drivers") or {}
    for driver in DRIVERS:
        r = risks.get(driver) or {}
        value = {"status": scrub_text(r.get("status")), "detail": scrub_text(r.get("detail")),
                 "level": (drivers.get(driver) or {}).get("level")}
        if not (is_nd(value["status"]) and is_nd(value["detail"])):
            sink.add("site", slug, f"risk_{driver}", value, src=src, judged=True)
        for n, e in enumerate(r.get("evidence") or []):
            url = e.get("u") if isinstance(e.get("u"), str) and e["u"].startswith("http") else None
            para = is_paraphrase(e.get("q"))  # a paraphrase is kept as the item's summary, not as a quote
            # `item` (the position in the driver's list) keeps two excerpts of one article apart
            sink.add("site", slug, "risk_evidence",
                     {"driver": driver, "item": n, "date": e.get("d"), "headline": e.get("w"), "url": url,
                      "summary": e.get("q") if para else None},
                     src=src, url=url, quote=None if para else e.get("q"), valid_from=e.get("d"))


def _watch(sink: FactSink, arc: Archive, case: str, slug: str) -> None:
    src = arc.src(DOSSIER.format(case))
    status = {(w.get("kind"), w.get("url"), w.get("id")): w.get("ok")
              for w in (arc.credit_cases.get(case) or {}).get("watch") or []}
    items = [{"kind": w.get("kind"), "agency": w.get("agency"), "id": w.get("id"), "url": w.get("url"),
              "query": w.get("query"), "why": w.get("why"),
              "page_ok": status.get((w.get("kind"), w.get("url"), w.get("id")))}
             for w in arc.get(DOSSIER, case).get("watch") or []]
    sink.add("site", slug, "watch_sources", items, src=src, judged=True)


def _terms(sink: FactSink, arc: Archive, case: str, slug: str) -> None:
    src, dossier = arc.src(DOSSIER.format(case)), arc.get(DOSSIER, case)
    sink.add("site", slug, "research_note", scrub_text(dossier.get("note")), src=src, judged=True)
    for fld, key in (("covenants", "covenants"), ("facility_terms", "facility"), ("deal_metrics", "metrics")):
        block = dossier.get(key)
        if not isinstance(block, dict):
            continue
        url, quote = first_url(block.get("src")), block.get("quote")
        value = {(k[:-2] + "_usd" if k.endswith("_m") else k): (usd_m(v) if k.endswith("_m") else v)
                 for k, v in block.items() if k not in ("src", "quote")}
        sink.add("site", slug, fld, value, src=src, url=url, quote=quote,
                 valid_from=block.get("drawn_asof"))


def write_dossier(sink: FactSink, arc: Archive, case: str, slug: str, stats: dict[str, Any]) -> None:
    if not arc.has(DOSSIER, case):
        stats["skipped"].setdefault("no dossier file", []).append(case)
        return
    _debt(sink, arc, case, slug)
    _collateral(sink, arc, case, slug)
    _parties(sink, arc, case, slug, stats)
    _cost(sink, arc, case, slug)
    _risks(sink, arc, case, slug)
    _watch(sink, arc, case, slug)
    _terms(sink, arc, case, slug)
