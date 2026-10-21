"""Which legacy prices the instruments loader may load (docs/methods/price-sources.md sections 2-4).

- ``classify_legacy_px``: flags aidebt ``px`` values that are copies of the finra.org TRACE pull or Börse
  Frankfurt fixings by matching price and date IN MEMORY against trace/trace_rows.json and
  watch/*/bondq.json. Those files are never stored, and no matched value is returned or written.
- ``dealer_quotes``: press-reported dealer quotes (kind 'dealer', tag R, the press URL).
- ``nport_marks`` / ``bdc_marks``: fund marks recomputed from the filing line the instrument cites
  (100 x value / balance), never copied from the legacy value.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from marden.seed.common import tag_for_url
from marden.seed.instruments_ids import Identifiers
from marden.seed.instruments_nport import (
    BDC_PERIOD,
    BdcLine,
    NportRecord,
    bdc_source_name,
    fund_mark_price,
    nport_source_name,
)
from marden.seed.instruments_parse import maturity_month, normalize_name

# Issuer 10-Q "carrying value approximates fair value" is a fact, not a price (price-sources.md section 3).
ISSUER_FAIR_VALUE_IDS = frozenset({"EX-CRWV-093", "EX-CRWV-096"})
TOLERANCE = 0.0051  # legacy prices are the source value rounded to 2 decimals

TRACE_COPY = "trace_copy_blocked"
FRANKFURT_COPY = "frankfurt_fixing_needs_licence"
ISSUER_FAIR_VALUE = "issuer_fair_value_not_a_price"
DEALER_LOADED = "dealer_quote_press_loaded"
MARK_RECOMPUTED = "fund_mark_recomputed_from_filing"
MARK_NOT_RECOMPUTABLE = "fund_mark_not_recomputable"
NOT_A_FUND_FILING = "source_not_a_fund_filing"


def close_price(a: Any, b: Any) -> bool:
    try:
        return a is not None and b is not None and abs(float(a) - float(b)) <= TOLERANCE
    except (TypeError, ValueError):
        return False


def classify_legacy_px(
    rows: Mapping[str, Mapping[str, Any]], trace_rows: Mapping[str, Any], bondq_snapshots: Iterable[Mapping[str, Any]]
) -> dict[str, str]:
    """instrument id -> TRACE_COPY / FRANKFURT_COPY for legacy px that copy a blocked source."""
    quotes: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for snap in bondq_snapshots:
        for q in snap.get("q", []):
            quotes[str(q.get("id"))].append(q)
    out: dict[str, str] = {}
    for iid, row in rows.items():
        px, pxd = row.get("px"), row.get("pxd")
        if px is None:
            continue
        t = trace_rows.get(iid) or {}
        series = [(s[0], s[1]) for s in t.get("ser", []) if len(s) >= 2]
        last_print = t.get("d") == pxd and close_price(t.get("px"), px)
        if last_print or any(d == pxd and close_price(p, px) for d, p in series):
            out[iid] = TRACE_COPY
        elif any(str(q.get("ts") or "")[:10] == pxd and close_price(q.get("last"), px) for q in quotes.get(iid, [])):
            out[iid] = FRANKFURT_COPY
    return out


@dataclass(frozen=True)
class Quote:
    instrument_id: str
    obs_date: date
    price: float
    url: str
    relpath: str  # seed file the quote was read from


def dealer_quotes(sources: Iterable[tuple[str, Mapping[str, Mapping[str, Any]]]]) -> list[Quote]:
    """Press dealer quotes: kind 'dealer quote', a dated price, and a non-filed (press) URL.

    ``sources`` are (relpath, {instrument id: price record}) pairs in priority order; the first record
    for an (instrument, date, url) wins. Case-keyed records ('case:JUPITER') duplicate instrument keys.
    """
    seen: set[tuple[str, date, str]] = set()
    out: list[Quote] = []
    for relpath, records in sources:
        for iid, rec in records.items():
            if iid.startswith("case:"):
                continue
            for r in [rec, rec.get("fund")] if isinstance(rec.get("fund"), dict) else [rec]:
                if r.get("kind") != "dealer quote" or r.get("px") is None or not r.get("url") or not r.get("date"):
                    continue
                url = str(r["url"])
                if tag_for_url(url) == "F":
                    continue  # a filed document is not a press quote
                day = date.fromisoformat(str(r["date"])[:10])
                key = (iid, day, url)
                if key not in seen:
                    seen.add(key)
                    out.append(Quote(iid, day, float(r["px"]), url, relpath))
    return out


@dataclass(frozen=True)
class Mark:
    instrument_id: str
    obs_date: date
    price: Decimal
    source_name: str
    source_url: str
    relpath: str
    method: str  # 'identifier' | 'name' | 'bdc'


def is_fund_filing(url: str | None) -> bool:
    return bool(url) and "sec.gov/Archives/edgar/data/" in str(url) and str(url).endswith("primary_doc.xml")


def nport_marks(
    citations: Mapping[tuple[str, str], set[str]],
    records: Iterable[NportRecord],
    idents: Mapping[str, Identifiers],
    rows: Mapping[str, Mapping[str, Any]],
    loan_kinds: Mapping[str, bool],
) -> list[Mark]:
    """Marks from N-PORT lines in the filing each instrument cites.

    A line joins by identifier (CUSIP, ISIN, LoanX on the line), or, for loans whose lines carry a
    placeholder CUSIP, by name: the line's title appears in the instrument's issuer/instrument text and
    exactly one instrument citing that filing, and exactly one line, match.
    """
    by_url: dict[str, list[NportRecord]] = defaultdict(list)
    for r in records:
        if r.is_par:
            by_url[r.url].append(r)
    citing: dict[str, list[str]] = defaultdict(list)
    for (iid, url) in sorted(citations):
        if is_fund_filing(url) and iid in rows:
            citing[url].append(iid)
    out: list[Mark] = []
    for url, iids in citing.items():
        lines = by_url.get(url, [])
        name_hits: dict[str, list[NportRecord]] = defaultdict(list)
        for rec in lines:
            matched = [i for i in iids if rec.keys() & idents[i].join_keys()]
            for iid in matched:
                out.append(_nport_mark(iid, rec, "identifier"))
            if matched or not _loanish(rec):
                continue
            title = normalize_name(rec.title or rec.name)
            if len(title) < 12 or " " not in title:
                continue
            hits = [i for i in iids if loan_kinds.get(i) and title in _text(rows[i])]
            if len(hits) == 1:
                name_hits[hits[0]].append(rec)
        for iid, recs in name_hits.items():
            if len({normalize_name(r.title or r.name) for r in recs}) == 1 and len(recs) == 1:
                out.append(_nport_mark(iid, recs[0], "name"))
    return [m for m in out if m.price is not None]


def _loanish(rec: NportRecord) -> bool:
    return rec.asset_cat == "LON" or not rec.cusip or rec.cusip in ("000000000", "999999999", "N/A")


def _text(row: Mapping[str, Any]) -> str:
    return normalize_name(f"{row.get('issuer') or ''} {row.get('inst') or ''}")


def _nport_mark(iid: str, rec: NportRecord, method: str) -> Mark:
    price = fund_mark_price(rec.value, rec.balance)
    return Mark(iid, rec.period, price, nport_source_name(rec.fund, rec.period), rec.url, rec.relpath,
                method)  # type: ignore[arg-type]


def _cik(url: str) -> str | None:
    parts = url.split("/edgar/data/", 1)
    return parts[1].split("/", 1)[0].lstrip("0") if len(parts) == 2 else None


_STOP = frozenset({"llc", "inc", "ltd", "lp", "corp", "the", "and", "pty", "data", "centers", "center", "holdings",
                   "holdco", "intermediate", "energy", "systems", "financing", "finance", "group", "loan", "loans",
                   "parent", "company", "issuer", "revolving", "warehouse", "services", "technologies", "computer"})


def _tokens(text: str) -> set[str]:
    return {t for t in normalize_name(text).split() if len(t) >= 3 and t not in _STOP and not t.isdigit()}


def _lien_of(row: Mapping[str, Any]) -> str | None:
    text = f"{row.get('rank') or ''} {row.get('inst') or ''}".lower()
    if re.search(r"\bsub\b|subordinat", text):
        return "Sub"
    if "unsecured" in text:
        return "Unsec"
    if "1l" in text or "first lien" in text or "senior secured" in text or "secured" in text:
        return "1L"
    return None


def bdc_marks(
    citations: Mapping[tuple[str, str], set[str]],
    fundpos: Mapping[str, list[BdcLine]],
    names: Mapping[str, str],
    rows: Mapping[str, Mapping[str, Any]],
) -> tuple[list[Mark], list[tuple[str, str, str]]]:
    """Marks from the 30 Jun 2026 BDC schedule line of the BDC whose 10-Q the instrument cites.

    A line matches when its borrower key shares a distinctive word with the issuer, its maturity month
    equals the instrument's and its lien agrees; a line claimed by several instruments, or an instrument
    with several candidate lines, is ambiguous and skipped. Returns (marks, skipped (id, url, reason)).
    """
    claims: dict[tuple[str, BdcLine], list[str]] = defaultdict(list)
    skipped: list[tuple[str, str, str]] = []
    for (iid, url) in sorted(citations):
        row = rows.get(iid)
        cik = _cik(url)
        if row is None or cik is None or cik not in fundpos or "20260630" not in url or is_fund_filing(url):
            continue
        if iid.startswith("BDC-"):
            skipped.append((iid, url, "borrower-level placeholder row (no tranche)"))
            continue
        month = maturity_month(row.get("mat"))
        if month is None:
            skipped.append((iid, url, "no maturity month to match a schedule line"))
            continue
        toks, lien = _tokens(str(row.get("issuer") or "")), _lien_of(row)
        cands = {ln for ln in fundpos[cik] if ln.maturity == month and toks & _tokens(f"{ln.bkey} {ln.borrower}")
                 and (lien is None or ln.lien in (None, lien))}
        distinct = {(ln.par_k, ln.fv_k) for ln in cands}
        if len(distinct) != 1:
            skipped.append((iid, url, f"{len(distinct)} candidate schedule lines"))
            continue
        line = sorted(cands, key=lambda ln: (ln.bkey, ln.borrower))[0]
        claims[(url, line)].append(iid)
    marks: list[Mark] = []
    for (url, line), iids in claims.items():
        if len(iids) > 1:
            skipped.extend((i, url, f"schedule line claimed by {len(iids)} instruments") for i in iids)
            continue
        price = fund_mark_price(line.fv_k, line.par_k)
        bdc = names.get(line.cik, f"CIK {line.cik}")
        if price is not None:
            marks.append(Mark(iids[0], BDC_PERIOD, price, bdc_source_name(bdc, BDC_PERIOD), url, "fundpos.json", "bdc"))
    return marks, skipped
