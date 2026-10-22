"""Fact rows for one aidebt instrument (field table: marden/seed/instruments.py docstring).

Tag rules (seed/common.py): terms take the tag and name of the row's primary ``src`` URL (press -> R; a row
without any URL is a Marden research value, M). A money or term value (coupon and its parts, maturity, size)
is F only when a filed document the row cites states it (``marden.seed.cited``: the stored text is checked,
an N-PORT report only on this instrument's own lines); the first such document is its source. When none
does, the row's press URL is the source (R); with no press URL either, the value is R "unverified archive
figure" with a note for review (``FactRow.flag``). An aidebt row lists every URL its research used, so the
first one is not necessarily the one that states each field: a fund's N-CSRS showing its own $1.2mm slice of
the Jupiter loan does not state the loan's $18B size. Ratings are agency opinions as reported (R). Legacy
judgments (tenants, chain, flags, holder notes, scores) and loader inferences (amount basis, legacy price
status, price basis) are M with a method version.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from marden.seed.cited import CitedDocs, TextLookup, anchor_patterns, first_stating, is_document_url
from marden.seed.common import SEED_AS_OF, host_of, source_name_for_url, tag_for_url
from marden.seed.instruments_ids import Identifiers
from marden.seed.instruments_parse import amount_basis, core_name, parse_coupon, parse_maturity

LEGACY_AIDEBT = "legacy-aidebt@2026-10-20"
LEGACY_AIWCREDIT = "legacy-aiwcredit@2026-10-20"
LOADER_METHOD = "seed-instruments@2026-10-21"
ARCHIVE = "Marden research archive"
GENERIC_SEARCH = "sec.gov/edgar/search"
# Rating text the legacy research read off finra.org's TRACE security master ("Moody's Baa1 / S&P BBB+ (via
# FINRA)"): finra.org data is never loaded (docs/methods/price-sources.md section 3), ratings included.
FINRA_TEXT = re.compile(r"\bFINRA\b|\bTRACE\b")
PRICE_BELOW_90 = "price < 90"
UNVERIFIED = "unverified archive figure"
ROMAN = re.compile(r"i{1,3}|iv|vi{0,3}|ix|x")  # series numerals in issuer names ('Issuer II' is not 'Issuer III')
MARDEN_CALC = re.compile(r"\bcalc(?:ulated)?\b|\bimplied\b", re.I)  # '~11-12% (calc)': the research's own figure
# Money and term fields: each is checked against the cited filings (coupon_type follows the coupon text).
CHECKED_TERMS = frozenset({"coupon", "coupon_type", "coupon_rate_pct", "coupon_benchmark", "coupon_spread_bp",
                           "maturity", "maturity_text", "amount_usd"})


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
    document_id: int | None = None  # the stored filing that states the value (else the seed file is used)
    flag: str | None = None  # why the value could not be sourced: opened as a review item for the fact


def primary_source(src: Iterable[str] | None) -> str | None:
    """The row's main document: its first URL that is not the generic EDGAR search page."""
    urls = [u for u in (src or []) if u]
    real = [u for u in urls if GENERIC_SEARCH not in u]
    return (real or urls or [None])[0]


def _m(field: str, value: Any, relpath: str, method: str, *, unit: str | None = None,
       valid_from: date = SEED_AS_OF) -> FactRow:
    return FactRow(field, value, "M", ARCHIVE, None, relpath, valid_from, unit, method)


def issuer_words(issuer: str | None) -> list[str]:
    """The issuer's name words that pick its lines in a fund report ('QTS Issuer ABS II, LLC' -> qts issuer abs;
    'VCI Asset Holdings 2 LLC' -> vci asset holdings 2); a parenthetical note is not part of the name."""
    name = re.sub(r"\(.*", "", issuer or "")
    return [w for w in core_name(name).split() if len(w) >= 3 or w.isdigit() or ROMAN.fullmatch(w)]


def title_words(inst: str | None) -> list[str]:
    """The instrument's own name words, to find its line in a fund report ('HYPER 2025-1 A2' -> hyper 2025 a2)."""
    return [w for w in re.findall(r"[a-z]+\d*|\d{4}", re.sub(r"\(.*", "", inst or "").lower())
            if len(w) >= 2 and w not in ("the", "of", "and")]


def _source_terms(row: Mapping[str, Any], urls: list[str], primary: str, cited: CitedDocs | TextLookup | None,
                  terms: Mapping[str, Any], ids: Iterable[str], names: Iterable[str], also: Iterable[str]):
    """field, value -> (tag, source name, url, document id, flag) for a row whose primary URL is filed."""
    filed = list(dict.fromkeys(u for u in [primary, *urls, *also] if tag_for_url(u) == "F" and is_document_url(u)))
    press = next((u for u in urls if host_of(u) and tag_for_url(u) != "F"), None)
    ids, names = list(ids), list(names)

    def source(field: str, value: Any) -> tuple[str, str, str | None, int | None, str | None]:
        check_field, check_value = ("coupon", terms.get("coupon")) if field == "coupon_type" else (field, value)
        if cited is not None and check_value is not None:
            ev = first_stating(cited, filed, check_field, check_value, ids=ids, names=names,
                               titles=title_words(row.get("inst")),
                               anchors=anchor_patterns(terms, check_field, name=row.get("inst")))
            if ev is not None:
                return "F", source_name_for_url(ev.url), ev.url, ev.document_id, None
        if press is not None:
            return "R", source_name_for_url(press), press, None, None
        if isinstance(value, str) and MARDEN_CALC.search(value):
            return "M", ARCHIVE, None, None, None
        checked = ", ".join(filed) or "no stored filing"
        return "R", UNVERIFIED, None, None, (
            f"{field} = {value!r}: no filing the archive row cites states it ({checked}) and the row has no press "
            "source; kept as an archive figure, not as filed")

    return source


def terms_facts(iid: str, row: Mapping[str, Any], kind: str, relpath: str,
                cited: CitedDocs | TextLookup | None = None, *, ids: Iterable[str] = (),
                also: Iterable[str] = ()) -> list[FactRow]:
    """Coupon, maturity, size, seniority, status and source URLs.

    Seniority, status and source URLs take the row's primary URL. Money and term values (CHECKED_TERMS) take
    the first filed URL of the row (then of ``also``: fund filings that list the instrument, N-PORT / BDC)
    whose stored text (``cited``, marden.seed.cited.CitedDocs) states them, else the row's press URL (R), else
    M when the text marks it the research's own calculation ('(calc)'), else R "unverified archive figure"
    with a ``flag``. Without ``cited`` nothing can be checked, so no term is F. ``ids``: the instrument's
    CUSIPs / ISINs (to read only its own lines of a fund report).
    """
    urls = [u for u in row.get("src") or [] if u]
    url = primary_source(urls)
    tag = tag_for_url(url) if url else "M"
    name = source_name_for_url(url, fallback=ARCHIVE)
    method = LEGACY_AIDEBT if tag == "M" else None
    values: list[tuple[str, Any, str | None]] = []

    def add(field: str, value: Any, unit: str | None = None) -> None:
        if value is not None and value != "" and value != []:
            values.append((field, value, unit))

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
    add("source_urls", urls)

    terms = {f: v for f, v, _ in values if f in CHECKED_TERMS}
    issuer = issuer_words(row.get("issuer"))
    sourced = _source_terms(row, urls, url, cited, terms, ids, issuer, also) if tag == "F" else None
    out: list[FactRow] = []
    for field, value, unit in values:
        if sourced is not None and field in CHECKED_TERMS:
            f_tag, f_name, f_url, doc_id, flag = sourced(field, value)
            f_method = LEGACY_AIDEBT if f_tag == "M" else None
            out.append(FactRow(field, value, f_tag, f_name, f_url, relpath, SEED_AS_OF, unit, f_method, doc_id, flag))
        else:
            out.append(FactRow(field, value, tag, name, url, relpath, SEED_AS_OF, unit, method))
    basis = amount_basis(iid, row, kind)
    if basis:
        out.append(_m("amount_basis", basis, relpath, LOADER_METHOD))
    return out


def rating_text(row: Mapping[str, Any]) -> str | None:
    """The rating text without the clauses read off finra.org; None when nothing else is left."""
    text = (row.get("rating") or "").strip()
    if not text or text.lower().startswith("no rating found"):
        return None
    kept = [part.strip() for part in text.split(";") if part.strip() and not FINRA_TEXT.search(part)]
    return "; ".join(kept) or None


def rating_fact(row: Mapping[str, Any], relpath: str) -> FactRow | None:
    text = rating_text(row)
    if text is None:
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
