"""The DERA quarterly Form N-PORT data set zip for the `sec_nport` collector (no network, no database).

`dataset_candidates` reads four of its tab-separated tables (SUBMISSION, REGISTRANT,
FUND_REPORTED_HOLDING, IDENTIFIERS; https://www.sec.gov/files/nport_readme.pdf) and returns the
accessions whose positions carry a watched identifier. The tables are streamed line by line, so the
1.6 GB of a quarter's text never sits in memory. Every filing's non-USD exchange rates are tallied
as well (sec_nport_fx), which settles the rate direction for the quarter's report dates.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from marden.collectors.sec_index_parse import normalize_cik
from marden.collectors.sec_nport_fx import FxVotes, parse_rate
from marden.collectors.sec_nport_parse import NPORT_FORMS, NportParseError, dera_date, id_keys


@dataclass(frozen=True)
class DatasetCandidate:
    accession: str
    cik: str
    form: str
    filing_date: date
    period: date
    matched_keys: tuple[str, ...]


@dataclass
class DatasetStats:
    holdings_rows: int = 0
    identifier_rows: int = 0
    matched_positions: int = 0
    matched_accessions: int = 0
    outside_periods: int = 0
    other_forms: int = 0


def _tsv(zf: zipfile.ZipFile, name: str) -> Iterator[list[str]]:
    with zf.open(name) as fh:
        for line in io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="\n"):
            yield line.rstrip("\r\n").split("\t")


def _columns(rows: Iterator[list[str]], name: str, wanted: tuple[str, ...]) -> list[int]:
    header = next(rows, None)
    if header is None or any(w not in header for w in wanted):
        raise NportParseError(f"{name}: missing columns {[w for w in wanted if w not in (header or [])]}")
    return [header.index(w) for w in wanted]


def dataset_candidates(
    zf: zipfile.ZipFile, keys: set[str], periods: set[date] | None = None, fx: FxVotes | None = None
) -> tuple[list[DatasetCandidate], DatasetStats]:
    """Accessions of NPORT-P(/A) filings with a position whose CUSIP, ISIN or other identifier is a
    watched key (optionally only those reporting as of one of `periods`), oldest filing first. With
    `fx`, every filing's (currency, exchange rate) pairs are added to it."""
    stats = DatasetStats()
    rows = _tsv(zf, "IDENTIFIERS.tsv")
    hid, isin, other = _columns(rows, "IDENTIFIERS", ("HOLDING_ID", "IDENTIFIER_ISIN", "OTHER_IDENTIFIER"))
    by_holding: dict[str, set[str]] = {}
    for r in rows:
        stats.identifier_rows += 1
        if len(r) > max(hid, isin, other) and (hit := id_keys(r[isin], r[other]) & keys):
            by_holding.setdefault(r[hid], set()).update(hit)
    rows = _tsv(zf, "FUND_REPORTED_HOLDING.tsv")
    acc, hid, cusip, cur, rate = _columns(rows, "FUND_REPORTED_HOLDING", (
        "ACCESSION_NUMBER", "HOLDING_ID", "ISSUER_CUSIP", "CURRENCY_CODE", "EXCHANGE_RATE"))
    matched: dict[str, set[str]] = {}
    rates: dict[str, set[tuple[str, Decimal]]] = {}
    for r in rows:
        stats.holdings_rows += 1
        if len(r) <= max(acc, hid, cusip, cur, rate):
            continue
        if fx is not None and r[rate] and r[cur] not in ("USD", "") and (value := parse_rate(r[rate])):
            rates.setdefault(r[acc], set()).add((r[cur], value))
        hit = (id_keys(r[cusip]) & keys) | by_holding.get(r[hid], set())
        if hit:
            stats.matched_positions += 1
            matched.setdefault(r[acc], set()).update(hit)
    subs: dict[str, tuple[str, date | None, date | None]] = {}
    rows = _tsv(zf, "SUBMISSION.tsv")
    acc, filed, form, rep = _columns(rows, "SUBMISSION", ("ACCESSION_NUMBER", "FILING_DATE", "SUB_TYPE", "REPORT_DATE"))
    for r in rows:
        if len(r) <= max(acc, filed, form, rep):
            continue
        if r[acc] in matched:
            subs[r[acc]] = (r[form].strip().upper(), dera_date(r[filed]), dera_date(r[rep]))
        if fx is not None and r[acc] in rates:
            fx.add(r[acc], dera_date(r[rep]), rates[r[acc]])
    ciks: dict[str, str] = {}
    rows = _tsv(zf, "REGISTRANT.tsv")
    acc, cik = _columns(rows, "REGISTRANT", ("ACCESSION_NUMBER", "CIK"))
    for r in rows:
        if len(r) > max(acc, cik) and r[acc] in matched and (c := normalize_cik(r[cik])):
            ciks[r[acc]] = c
    out = []
    for accession, hit in matched.items():
        sub_form, filed_on, period = subs.get(accession, ("", None, None))
        cik_value = ciks.get(accession)
        if sub_form not in NPORT_FORMS or filed_on is None or period is None or cik_value is None:
            stats.other_forms += 1
            continue
        if periods is not None and period not in periods:
            stats.outside_periods += 1
            continue
        out.append(DatasetCandidate(accession, cik_value, sub_form, filed_on, period, tuple(sorted(hit))))
    stats.matched_accessions = len(matched)
    out.sort(key=lambda c: (c.filing_date, c.accession))
    return out, stats
