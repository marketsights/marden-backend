"""Fact rows for one aidebt instrument (field table: marden/seed/instruments.py docstring).

Tag rules (seed/common.py): terms take the tag and name of the row's primary ``src`` URL (filed -> F,
press -> R; a row without any URL is a Marden research value, M). Ratings are agency opinions as
reported (R). Legacy judgments (tenants, chain, flags, holder notes, scores) and loader inferences
(amount basis, legacy price status, price basis) are M with a method version.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from marden.seed.common import SEED_AS_OF, source_name_for_url, tag_for_url
from marden.seed.instruments_ids import Identifiers
from marden.seed.instruments_parse import amount_basis, parse_coupon, parse_maturity

LEGACY_AIDEBT = "legacy-aidebt@2026-10-20"
LEGACY_AIWCREDIT = "legacy-aiwcredit@2026-10-20"
LOADER_METHOD = "seed-instruments@2026-10-21"
ARCHIVE = "Marden research archive"
GENERIC_SEARCH = "sec.gov/edgar/search"
PRICE_BELOW_90 = "price < 90"


@dataclass(frozen=True)
class FactRow:
    field: str
    value: Any
    tag: str
    source_name: str
    source_url: str | None
    relpath: str
    valid_from: date = SEED_AS_OF
    unit: str | None = None
    method_version: str | None = None


def primary_source(src: Iterable[str] | None) -> str | None:
    """The row's main document: its first URL that is not the generic EDGAR search page."""
    urls = [u for u in (src or []) if u]
    real = [u for u in urls if GENERIC_SEARCH not in u]
    return (real or urls or [None])[0]


def _m(field: str, value: Any, relpath: str, method: str, *, unit: str | None = None,
       valid_from: date = SEED_AS_OF) -> FactRow:
    return FactRow(field, value, "M", ARCHIVE, None, relpath, valid_from, unit, method)


def terms_facts(iid: str, row: Mapping[str, Any], kind: str, relpath: str) -> list[FactRow]:
    """Coupon, maturity, size, seniority, status and source URLs, sourced to the row's primary URL."""
    url = primary_source(row.get("src"))
    tag = tag_for_url(url) if url else "M"
    name = source_name_for_url(url, fallback=ARCHIVE)
    method = LEGACY_AIDEBT if tag == "M" else None
    out: list[FactRow] = []

    def add(field: str, value: Any, unit: str | None = None) -> None:
        if value is not None and value != "" and value != []:
            out.append(FactRow(field, value, tag, name, url, relpath, SEED_AS_OF, unit, method))

    cpn = (row.get("cpn") or "").strip()
    add("coupon", cpn or None)
    parsed = parse_coupon(cpn)
    add("coupon_type", parsed.get("coupon_type"))
    add("coupon_rate_pct", parsed.get("coupon_rate_pct"), "pct")
    add("coupon_benchmark", parsed.get("coupon_benchmark"))
    add("coupon_spread_bp", parsed.get("coupon_spread_bp"), "bp")
    maturity, leftover = parse_maturity(row.get("mat"))
    add("maturity", maturity, "date")
    add("maturity_text", leftover)
    amt = row.get("amt_m")
    add("amount_usd", round(float(amt) * 1_000_000) if amt is not None else None, "USD")
    add("seniority", (row.get("rank") or "").strip() or None)
    add("status", row.get("status"))
    add("source_urls", [u for u in row.get("src") or [] if u])
    basis = amount_basis(iid, row, kind)
    if basis:
        out.append(_m("amount_basis", basis, relpath, LOADER_METHOD))
    return out


def rating_fact(row: Mapping[str, Any], relpath: str) -> FactRow | None:
    text = (row.get("rating") or "").strip()
    if not text or text.lower().startswith("no rating found"):
        return None
    return FactRow("rating", text, tag_for_url(None), source_name_for_url(None, fallback=ARCHIVE), None, relpath)


def judgment_facts(row: Mapping[str, Any], relpath: str) -> tuple[list[FactRow], list[str]]:
    """Legacy research text and scores (M). Returns (facts, skip reasons).

    The 'price < 90' flag (and the legacy score built on it) came from legacy prices that are mostly
    blocked copies, so it is never carried over: a Marden model re-derives it from Marden prices. (Keeping it
    when some loaded price is below 90 made these facts depend on what other writers had stored.)
    """
    out: list[FactRow] = []
    skips: list[str] = []
    for field, key in (("tenants", "tenants"), ("backstop", "backstop"), ("chain", "chain")):
        text = (row.get(key) or "").strip()
        if text and text.lower() not in ("n/d", "none"):
            out.append(_m(field, text, relpath, LEGACY_AIDEBT))
    flags = list(row.get("flags") or [])
    tainted = PRICE_BELOW_90 in flags
    if tainted:
        flags.remove(PRICE_BELOW_90)
        skips.append("flag 'price < 90' dropped: derived from a legacy price that was not loaded")
    if flags:
        out.append(_m("flags", flags, relpath, LEGACY_AIDEBT))
    holders = [h for h in row.get("holders") or [] if h]
    if holders:
        out.append(_m("holder_notes", holders, relpath, LEGACY_AIDEBT))
    score = row.get("score")
    if score is not None and not tainted:
        out.append(_m("legacy_score", score, relpath, LEGACY_AIDEBT, unit="score_0_8"))
    elif score is not None:
        skips.append("legacy_score skipped: it scores the dropped 'price < 90' flag")
    if row.get("cat"):
        out.append(_m("legacy_category", row["cat"], relpath, LEGACY_AIDEBT))
    return out, skips


def exposure_fact(info: Mapping[str, Any], relpath: str, as_of_day: date) -> FactRow | None:
    links = [{"case": c, "role": r} for c, r in info.get("x") or [] if c and r]
    if not links:
        return None
    return _m("exposure_links", links, relpath, LEGACY_AIWCREDIT, valid_from=as_of_day)


def identifier_fact(ident: Identifiers, relpath: str) -> FactRow | None:
    """F, sourced to the earliest N-PORT filing whose line shows one of the identifiers; else M."""
    items = ident.as_fact()
    if not items:
        return None
    if ident.nport_records:
        rec = sorted(ident.nport_records, key=lambda r: (r.period, r.url))[0]
        return FactRow("identifiers", items, "F", source_name_for_url(rec.url), rec.url, rec.relpath, rec.period)
    return _m("identifiers", items, relpath, LOADER_METHOD)


def price_basis_value(kinds: set[str]) -> str:
    """Docs only (v1-instruments.md): which kinds of seeded price an instrument has."""
    parts = []
    if "fund_mark" in kinds:
        parts.append("fund marks")
    if "dealer" in kinds:
        parts.append("dealer quote (press)")
    return "; ".join(parts) or "none"
