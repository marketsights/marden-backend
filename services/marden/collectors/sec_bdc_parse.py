"""Parsers for the SEC BDC Data Sets: the index page, and `soi.tsv` + `datasets/tag.tsv` in each zip.

The SOI report is a pivot of each BDC's XBRL schedule of investments: metadata columns, then
axis/member columns, then one column per tag, headed by the tag's label (BDC readme 5.08). The
labels move between taxonomy years (us-gaap/2026 labels InvestmentOwnedAtCost "Adjusted cost basis"
and InvestmentOwnedAtFairValue "Initial fair value of Investment"), so the value columns are found
through the zip's own tag table, by tag name. Rows are kept when they are point-in-time values
(qtrs 0) for the filing's own period (each 10-Q repeats the prior year-end as a comparative, which
the earlier filing already reported), the form is a 10-Q or 10-K, and the line names a followed
borrower (sec_bdc_borrowers). Numbers are as filed: money in USD, rates as decimal fractions.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from marden.collectors import sec_bdc_borrowers as bb

PAGE_URL = "https://www.sec.gov/data-research/sec-markets-data/bdc-data-sets"
FILES_BASE = "https://www.sec.gov/files/datastandardsinnovation/data/business-development-company-bdc-data-sets"
_ZIP_LINK = re.compile(r'href="[^"]*/business-development-company-bdc-data-sets/((\d{4})(?:_(\d{2})|q([1-4]))_bdc\.zip)"')
METADATA = ("adsh", "cik", "name", "ddate", "qtrs", "form", "filed", "period", "inlineurl", "cstm")
IDENTIFIER = "Investment, Identifier Axis"
ISSUER_AXIS = "Investment, Issuer Name Axis"
TYPE_AXIS = "Investment Type Axis"
NONCONSOLIDATED_AXIS = "Investment Company, Nonconsolidated Subsidiary Axis"

# concept -> (us-gaap tag, labels the SOI presets use)
CONCEPTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "par": ("InvestmentOwnedBalancePrincipalAmount", ("Investment Owned, Balance, Principal Amount",)),
    "cost": ("InvestmentOwnedAtCost", ("Investment Owned, Cost",)),
    "fv": ("InvestmentOwnedAtFairValue", ("Investment Owned, Fair Value",)),
    "pct_nav": ("InvestmentOwnedPercentOfNetAssets", ("Investment Owned, Net Assets, Percentage",)),
    "rate": ("InvestmentInterestRate", ("Investment Interest Rate",)),
    "spread": ("InvestmentBasisSpreadVariableRate", ("Investment, Basis Spread, Variable Rate",)),
    "maturity": ("InvestmentMaturityDate", ("Investment Maturity Date",)),
    "unfunded": ("InvestmentCompanyFinancialCommitmentToInvesteeFutureAmount",
                 ("Investment Company, Financial Support to Investee Contractually Required, Not Provided, Amount",)),
    "issuer_enum": ("InvestmentIssuerNameExtensibleEnumeration", ("Investment, Issuer Name [Extensible Enumeration]",)),
}


class ParseError(ValueError):
    """The file is not in the shape this parser knows; nothing from it is written."""


@dataclass(frozen=True, order=True)
class DataSetFile:
    start: date  # first month the file covers (monthly files: that month; 2025 and earlier: quarters)
    name: str
    url: str


def parse_index_page(html: str) -> list[DataSetFile]:
    """Every BDC data set zip linked from the index page, oldest first."""
    files: dict[str, DataSetFile] = {}
    for m in _ZIP_LINK.finditer(html):
        name, year, month, quarter = m.group(1), int(m.group(2)), m.group(3), m.group(4)
        start = date(year, int(month), 1) if month else date(year, 3 * int(quarter) - 2, 1)
        files[name] = DataSetFile(start, name, f"{FILES_BASE}/{name}")
    if not files:
        raise ParseError("BDC data sets page: no zip links found")
    return sorted(files.values())


@dataclass(frozen=True)
class SoiLine:
    adsh: str
    cik: str
    bdc_name: str
    form: str  # '10-Q' or '10-K' (amendments folded in; the accession tells them apart)
    filed: date
    period: date
    filing_url: str
    identifier: str
    issuer_axis: str
    borrower: str  # sec_bdc_borrowers key
    line_key: str
    par: Decimal | None = None
    cost: Decimal | None = None
    fv: Decimal | None = None
    pct_nav: Decimal | None = None  # fraction of net assets as filed (0.0068 = 0.68%)
    rate: Decimal | None = None  # fraction
    spread: Decimal | None = None  # fraction
    maturity: date | None = None
    unfunded: Decimal | None = None

    @property
    def text(self) -> str:
        return f"{self.identifier} {self.issuer_axis}".strip()


@dataclass
class ParsedSoi:
    lines: list[SoiLine] = field(default_factory=list)
    rows: int = 0
    rows_other_period: int = 0  # comparative periods and duration facts
    rows_other_form: int = 0
    rows_jv: int = 0
    rows_duplicate: int = 0
    excluded: Counter = field(default_factory=Counter)  # collision label -> rows
    bad_values: Counter = field(default_factory=Counter)  # concept -> unparseable cells


def _labels(tag_tsv: bytes) -> tuple[dict[str, set[str]], set[str]]:
    """concept -> its labels; plus every value (non-abstract standard tag) label."""
    wanted = {tag: concept for concept, (tag, _) in CONCEPTS.items()}
    by_concept: dict[str, set[str]] = {c: set(presets) for c, (_, presets) in CONCEPTS.items()}
    values: set[str] = {lbl for _, presets in CONCEPTS.values() for lbl in presets}
    reader = csv.DictReader(io.StringIO(tag_tsv.decode("utf-8", errors="replace")), delimiter="\t")
    for row in reader:
        if row.get("custom") != "0" or not row.get("tlabel"):
            continue
        if row.get("abstract") == "0":
            values.add(row["tlabel"])
        concept = wanted.get(row.get("tag") or "")
        if concept:
            by_concept[concept].add(row["tlabel"])
    return by_concept, values


def _iso(text: str) -> date | None:
    try:
        return date.fromisoformat(text.strip()[:10]) if text and text.strip() else None
    except ValueError:
        return None


def archives_url(inline_url: str, cik: str, adsh: str) -> str:
    """The filing's primary document under /Archives (the ix viewer URL unwrapped), else its index page."""
    if "/Archives/" in inline_url:
        return "https://www.sec.gov" + inline_url[inline_url.index("/Archives/"):]
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh.replace('-', '')}/{adsh}-index.htm"


def _rows(soi: io.TextIOBase) -> Iterator[list[str]]:
    yield from csv.reader(soi, delimiter="\t")  # quoted fields may hold tabs: standard quoting, not QUOTE_NONE


def parse_zip(data: bytes) -> ParsedSoi:
    """The followed borrowers' schedule lines in one BDC data set zip."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        names = set(zf.namelist())
    except zipfile.BadZipFile as exc:
        raise ParseError(f"BDC data set: not a zip ({exc})") from None
    if "soi.tsv" not in names or "datasets/tag.tsv" not in names:
        raise ParseError(f"BDC data set: soi.tsv or datasets/tag.tsv missing ({sorted(names)[:5]})")
    by_concept, value_labels = _labels(zf.read("datasets/tag.tsv"))
    out = ParsedSoi()
    with zf.open("soi.tsv") as raw:
        rows = _rows(io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline=""))
        header = next(rows, None)
        if header is None:  # e.g. 2026_06: "no filings with financial statement data"
            return out
        if tuple(header[: len(METADATA)]) != METADATA or IDENTIFIER not in header:
            raise ParseError(f"soi.tsv: unexpected header {header[:12]!r}")
        _parse_rows(rows, header, by_concept, value_labels, out)
    return out


def _parse_rows(rows: Iterator[list[str]], header: list[str], by_concept: dict[str, set[str]],
                value_labels: set[str], out: ParsedSoi) -> None:
    idx = {name: i for i, name in enumerate(header)}
    cols = {c: [i for i, name in enumerate(header) if name in labels] for c, labels in by_concept.items()}
    axes = [i for i, name in enumerate(header) if i >= len(METADATA) and name not in value_labels]
    ident_i, issuer_i = idx[IDENTIFIER], idx.get(ISSUER_AXIS)
    enum_i = cols["issuer_enum"]
    nonconsolidated_i = idx.get(NONCONSOLIDATED_AXIS)
    seen: set[tuple[str, str]] = set()
    for row in rows:
        out.rows += 1
        if len(row) != len(header):
            raise ParseError(f"soi.tsv row {out.rows}: {len(row)} fields, header has {len(header)}")
        identifier = " ".join(row[ident_i].split())
        issuer = " ".join(row[issuer_i].split()) if issuer_i is not None else ""
        haystack = " ".join([identifier, issuer, *(row[i] for i in enum_i)])
        label = bb.excluded_by(haystack)
        if label is not None:
            out.excluded[label] += 1
            continue
        borrower = bb.match_borrower(haystack)
        if borrower is None:
            continue
        if row[idx["qtrs"]] != "0" or row[idx["ddate"]] != row[idx["period"]]:
            out.rows_other_period += 1
            continue
        form = row[idx["form"]].removesuffix("/A")
        if form not in ("10-Q", "10-K"):
            out.rows_other_form += 1
            continue
        if bb.held_by_jv(identifier) or (nonconsolidated_i is not None and row[nonconsolidated_i]):
            out.rows_jv += 1
            continue
        line = _line(row, idx, cols, axes, header, identifier, issuer, form, borrower.key, out)
        if line is None:
            continue
        if (line.adsh, line.line_key) in seen:
            out.rows_duplicate += 1
            continue
        seen.add((line.adsh, line.line_key))
        out.lines.append(line)


def _line(row: list[str], idx: dict[str, int], cols: dict[str, list[int]], axes: list[int], header: list[str],
          identifier: str, issuer: str, form: str, borrower: str, out: ParsedSoi) -> SoiLine | None:
    def first(concept: str) -> str | None:
        return next((row[i] for i in cols[concept] if row[i].strip()), None)

    def number(concept: str) -> Decimal | None:
        text = first(concept)
        value = bb.decimal_or_none(text)
        if text is not None and value is None:
            out.bad_values[concept] += 1
        return value

    period, filed = _iso(row[idx["period"]]), _iso(row[idx["filed"]])
    if period is None or filed is None:
        out.bad_values["period"] += 1
        return None
    key_parts = [row[idx["ddate"]], sorted((header[i], " ".join(row[i].split())) for i in axes if row[i].strip())]
    line_key = hashlib.sha256(json.dumps(key_parts, ensure_ascii=False).encode()).hexdigest()[:32]
    maturity_text = first("maturity")
    maturity = _iso(maturity_text) if maturity_text else None
    if maturity_text and maturity is None:
        out.bad_values["maturity"] += 1
    cik, adsh = row[idx["cik"]].strip(), row[idx["adsh"]].strip()
    return SoiLine(
        adsh=adsh, cik=cik, bdc_name=" ".join(row[idx["name"]].split()), form=form, filed=filed, period=period,
        filing_url=archives_url(row[idx["inlineurl"]].strip(), cik, adsh), identifier=identifier,
        issuer_axis=issuer, borrower=borrower, line_key=line_key, par=number("par"), cost=number("cost"),
        fv=number("fv"), pct_nav=number("pct_nav"), rate=number("rate"), spread=number("spread"),
        maturity=maturity, unfunded=number("unfunded"),
    )
