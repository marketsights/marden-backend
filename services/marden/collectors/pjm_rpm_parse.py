"""Parsers for PJM's RPM auction results: the RPM page's document list, the results workbooks and report dates.

- `find_auctions`: every "Report PDF | Results XLS" pair on https://www.pjm.com/markets-and-operations/rpm whose
  results file is a Base Residual or Incremental Auction workbook for delivery year FIRST_DELIVERY_YEAR or later
  (2020/2021 is the first delivery year procured entirely as Capacity Performance; older years are legacy .xls
  files with separate Base and CP products and are listed as skipped).
- `parse_results`: from the auction's own sheets (a 3rd IA workbook repeats the BRA sheets, which are ignored):
  "<auction> Resource Clearing Results" -> per LDA the Resource Clearing Price ($/MW-day) and cleared MW (BRA:
  total annual equivalent resources cleared; IA: sell offers cleared, and buy bids cleared);
  "<auction> Load Pricing Results" -> per transmission zone the adjusted zonal capacity price ($/MW-day, the
  "**" column, which includes PRD credit funding: the price load pays), the zonal UCAP obligation (MW) and
  the zone's most specific LDA.
- `report_dates`: when the results were posted ("... and the results were posted on March 9, 2026", else the
  report's cover date, else the PDF's creation date) and when the auction opened, where the report says.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urljoin

import pymupdf
import openpyxl
from openpyxl.utils import get_column_letter

from marden.collectors.capacity_common import MW, USD_MW_DAY, Value, decimal, period_key
from marden.collectors.rates_common import ParseError

PAGE_URL = "https://www.pjm.com/markets-and-operations/rpm"
FIRST_DELIVERY_YEAR = 2020
AUCTIONS: dict[str, tuple[str, str, str]] = {  # file name token -> (key, name, sheet prefix without spaces)
    "base-residual-auction": ("bra", "Base Residual Auction", "bra"),
    "bra": ("bra", "Base Residual Auction", "bra"),
    "first-incremental-auction": ("1ia", "First Incremental Auction", "1stia"),
    "1ia": ("1ia", "First Incremental Auction", "1stia"),
    "second-incremental-auction": ("2ia", "Second Incremental Auction", "2ndia"),
    "2ia": ("2ia", "Second Incremental Auction", "2ndia"),
    "third-incremental-auction": ("3ia", "Third Incremental Auction", "3rdia"),
    "3ia": ("3ia", "Third Incremental Auction", "3rdia"),
}
_RESULTS = re.compile(r"/(\d{4})-(\d{4})-(" + "|".join(AUCTIONS) + r")-results\.(xlsx?)$", re.I)
_ANCHOR = re.compile(r'<a\b[^>]*\bhref="(?P<href>[^"]+)"[^>]*>(?P<text>.*?)</a>', re.I | re.S)
_GAP = re.compile(r"&nbsp;?|\s|\|")
ALIASES = {"PSEG": "PS", "PS-NORTH": "PSNORTH", "PS NORTH": "PSNORTH", "DPL-SOUTH": "DPLSOUTH",
           "DPL SOUTH": "DPLSOUTH", "PPL": "PL", "DAY": "DAYTON", "DUQ": "DLCO", "AECO": "AE", "PENELEC": "PENLC",
           "ATSI CLEVELAND": "ATSI-CLEVELAND"}
MAX_PRICE = Decimal(2000)  # $/MW-day; every RPM clearing price so far is below 400
_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_DATE = rf"((?:{_MONTHS}) \d{{1,2}}, \d{{4}})"


@dataclass(frozen=True)
class Listed:
    start_year: int
    auction: str  # 'bra', '1ia', '2ia', '3ia'
    results_url: str
    report_url: str | None

    @property
    def period(self) -> str:
        return period_key(self.start_year, None if self.auction == "bra" else self.auction)

    @property
    def name(self) -> str:
        label = next(v[1] for v in AUCTIONS.values() if v[0] == self.auction)
        return f"{self.start_year}/{self.start_year + 1} {label}"

    @property
    def sheet_prefix(self) -> str:
        return next(v[2] for v in AUCTIONS.values() if v[0] == self.auction)


def find_auctions(page: bytes, *, base_url: str = PAGE_URL,
                  first_year: int = FIRST_DELIVERY_YEAR) -> tuple[list[Listed], list[str]]:
    """(auctions to load, sorted oldest first; results files skipped as pre-Capacity-Performance or legacy)."""
    html = page.decode("utf-8", errors="replace")
    anchors = list(_ANCHOR.finditer(html))
    found: dict[tuple[int, str], Listed] = {}
    skipped: list[str] = []
    for i, a in enumerate(anchors):
        m = _RESULTS.search(a["href"])
        if not m:
            continue
        start, end, token, ext = int(m[1]), int(m[2]), m[3].lower(), m[4].lower()
        name = a["href"].rsplit("/", 1)[-1]
        if end != start + 1 or start < first_year or ext != "xlsx":
            skipped.append(name)
            continue
        report = None
        if i > 0 and anchors[i - 1]["href"].lower().endswith(".pdf"):
            between = html[anchors[i - 1].end():a.start()]
            if not _GAP.sub("", between):  # "Report PDF | Results XLS" in one cell
                report = urljoin(base_url, anchors[i - 1]["href"])
        key = (start, AUCTIONS[token][0])
        if key not in found:
            found[key] = Listed(start, key[1], urljoin(base_url, a["href"]), report)
    if not found:
        raise ParseError(f"no RPM results workbooks found on {base_url}")
    order = {"bra": 0, "1ia": 1, "2ia": 2, "3ia": 3}
    return sorted(found.values(), key=lambda x: (x.start_year, order[x.auction])), sorted(set(skipped))


# ---------------------------------------------------------------- workbook
def _sheet(wb: Any, prefix: str, suffix: str) -> Any:
    want = prefix + suffix
    for ws in wb.worksheets:
        if re.sub(r"\s+", "", ws.title).lower() == want:
            return ws
    raise ParseError(f"no sheet '{prefix} {suffix}' in {[ws.title for ws in wb.worksheets]}")


def _name(cell: Any) -> str:
    text = re.sub(r"[*#\s]+$", "", str(cell or "").strip()).strip()  # 'AEP ***', 'EKPC#', 'RTO*': footnote marks
    return ALIASES.get(text.upper(), text.upper())


def _num(cell: Any) -> bool:
    return isinstance(cell, int | float) and not isinstance(cell, bool)


def _header(rows: list[tuple], start: int, first: str, needles: tuple[str, ...]) -> int | None:
    for i in range(start, len(rows)):
        row = rows[i]
        head = str(row[0] or "").strip()
        if head.startswith(first) and all(any(n in str(c or "") for c in row) for n in needles):
            return i
    return None


def _col(row: tuple, needle: str, *, last: bool = False, also: str | None = None) -> int | None:
    hits = [j for j, c in enumerate(row) if needle in str(c or "") and (also is None or also in str(c or ""))]
    return (hits[-1] if last else hits[0]) if hits else None


def _block(rows: list[tuple], head: int, cols: list[int]) -> list[tuple[int, str]]:
    """(row index, name) of the data rows under a header, until the first blank, note or non-number row."""
    out = []
    for i in range(head + 1, len(rows)):
        name = str(rows[i][0] or "").strip()
        if not name or name.startswith(("*", "Note", "Total")) or not all(_num(rows[i][c]) for c in cols):
            break
        out.append((i, _name(name)))
    return out


def _loc(fname: str, ws: Any, i: int, j: int, what: str) -> str:
    return f"{fname} '{ws.title}'!{get_column_letter(j + 1)}{i + 1} ({what})"


@dataclass
class Results:
    ldas: dict[str, dict[str, Value]] = field(default_factory=dict)
    zones: dict[str, dict[str, Value]] = field(default_factory=dict)


def parse_results(data: bytes, listed: Listed, fname: str) -> Results:
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    except Exception as exc:  # zipfile.BadZipFile, KeyError, InvalidFileException: not a workbook
        raise ParseError(f"{fname}: not an xlsx workbook ({type(exc).__name__}: {exc})") from None
    out = Results()
    _resource(out, _sheet(wb, listed.sheet_prefix, "resourceclearingresults"), listed, fname)
    _load(out, _sheet(wb, listed.sheet_prefix, "loadpricingresults"), fname)
    if "RTO" not in out.ldas or len(out.zones) < 15:
        raise ParseError(f"{fname}: expected RTO and the transmission zones, got {sorted(out.ldas)} / {sorted(out.zones)}")
    return out


def _resource(out: Results, ws: Any, listed: Listed, fname: str) -> None:
    rows = list(ws.iter_rows(min_row=1, values_only=True))
    head = _header(rows, 0, "LDA", ("Resource Clearing Price",))
    if head is None:
        raise ParseError(f"{fname} '{ws.title}': no Resource Clearing Price table")
    pc = _col(rows[head], "Resource Clearing Price")
    assert pc is not None
    for i, lda in _block(rows, head, [pc]):
        price = decimal(rows[i][pc], f"{fname} {lda} clearing price")
        if not Decimal(0) <= price <= MAX_PRICE:
            raise ParseError(f"{fname} {lda}: clearing price {price} is outside 0..{MAX_PRICE} $/MW-day")
        out.ldas.setdefault(lda, {})["clearing_price"] = Value(
            price, USD_MW_DAY, _loc(fname, ws, i, pc, f"Resource Clearing Price, {lda}"))
    if listed.auction == "bra":
        cleared_head = _header(rows, head + 1, "LDA", ("Total Annual Equivalent Resources Cleared",))
        needles = [("cleared_mw", "Total Annual Equivalent Resources Cleared", "Total Annual Equivalent")]
    else:
        cleared_head = _header(rows, head + 1, "LDA", ("Sell Offers Cleared", "Buy Bids Cleared"))
        needles = [("cleared_mw", "Sell Offers Cleared", "Sell Offers Cleared"),
                   ("buy_bids_cleared_mw", "Buy Bids Cleared", "Buy Bids Cleared")]
    if cleared_head is None:
        raise ParseError(f"{fname} '{ws.title}': no cleared MW table")
    cols = {f: (_col(rows[cleared_head], n), label) for f, n, label in needles}
    for i, lda in _block(rows, cleared_head, [c for c, _ in cols.values() if c is not None]):
        for fld, (c, label) in cols.items():
            if c is None:
                continue
            mw = decimal(rows[i][c], f"{fname} {lda} {fld}")
            if mw < 0:
                raise ParseError(f"{fname} {lda}: negative {fld} {mw}")
            out.ldas.setdefault(lda, {})[fld] = Value(mw, MW, _loc(fname, ws, i, c, f"{label} [MW], {lda}"))


def _load(out: Results, ws: Any, fname: str) -> None:
    rows = list(ws.iter_rows(min_row=1, values_only=True))
    head = next((i for i, r in enumerate(rows) if str(r[0] or "").strip() == "Zone"
                 and str(r[1] or "").strip() == "LDA1"), None)
    if head is None:
        raise ParseError(f"{fname} '{ws.title}': no zonal results table (Zone, LDA1, LDA2, LDA3 ...)")
    hdr = rows[head]
    price = _col(hdr, "Zonal Capacity Price", last=True, also="**") or _col(hdr, "Zonal Capacity Price", last=True)
    oblig = _col(hdr, "UCAP Obligation", last=True)
    if price is None or oblig is None:
        raise ParseError(f"{fname} '{ws.title}': zonal table lacks a capacity price or UCAP obligation column")
    for i, zone in _block(rows, head, [price, oblig]):
        value = decimal(rows[i][price], f"{fname} {zone} zonal price")
        if not Decimal(0) <= value <= MAX_PRICE:
            raise ParseError(f"{fname} {zone}: zonal capacity price {value} is outside 0..{MAX_PRICE} $/MW-day")
        path = [_name(c) for c in rows[i][1:4] if str(c or "").strip()]
        vals = out.zones.setdefault(zone, {})
        vals["zonal_capacity_price"] = Value(value, USD_MW_DAY, _loc(fname, ws, i, price, f"{hdr[price]}, {zone}"))
        vals["zonal_ucap_obligation_mw"] = Value(decimal(rows[i][oblig], f"{fname} {zone} obligation"), MW,
                                                 _loc(fname, ws, i, oblig, f"{hdr[oblig]}, {zone}"))
        if zone in out.ldas:  # PJM leaves the LDA columns blank for some modeled LDAs (DOM in 2025/2026)
            vals["lda"] = Value(zone, None, f"{fname}: {zone} is a modeled LDA in this auction's Resource Clearing Prices")
        else:
            vals["lda"] = Value(path[-1] if path else "RTO", None,
                                f"{fname} '{ws.title}'!B{i + 1}:D{i + 1} (LDA1-LDA3 of {zone}; none listed = RTO)")


def zone_results(res: Results) -> dict[str, dict[str, Value]]:
    """LDA and zone values merged by code (DOM is both an LDA and a transmission zone)."""
    merged: dict[str, dict[str, Value]] = {}
    for table in (res.ldas, res.zones):
        for code, vals in table.items():
            merged.setdefault(code, {}).update(vals)
    return merged


# ---------------------------------------------------------------- report dates
@dataclass(frozen=True)
class ReportDates:
    published: date
    published_loc: str
    opened: date | None = None
    opened_loc: str | None = None


def _day(text: str) -> date:
    return datetime.strptime(text, "%B %d, %Y").date()


def report_dates(data: bytes, fname: str) -> ReportDates:
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # pymupdf.FileDataError and friends: not a PDF
        raise ParseError(f"{fname}: not a PDF ({type(exc).__name__}: {exc})") from None
    with doc:
        pages = [re.sub(r"\s+", " ", doc[i].get_text()) for i in range(min(doc.page_count, 6))]
        created = (doc.metadata or {}).get("creationDate") or ""
    text = " ".join(pages)
    opened = re.search(rf"\bopened on {_DATE}", text)
    opened_day = _day(opened[1]) if opened else None
    opened_loc = f"{fname}: \"{opened[0]}\"" if opened else None
    posted = re.search(rf"results were posted on {_DATE}", text)
    if posted:
        return ReportDates(_day(posted[1]), f"{fname}: \"{posted[0]}\"", opened_day, opened_loc)
    cover = re.search(rf"^.{{0,300}}?\b(?:Report|Results)\s+{_DATE}", pages[0] if pages else "")
    if cover:
        return ReportDates(_day(cover[1]), f"{fname}: cover date \"{cover[1]}\"", opened_day, opened_loc)
    m = re.match(r"D:(\d{4})(\d{2})(\d{2})", created)
    if m:
        day = date(int(m[1]), int(m[2]), int(m[3]))
        return ReportDates(day, f"{fname}: PDF CreationDate {created} (the report states no posting date)",
                           opened_day, opened_loc)
    raise ParseError(f"{fname}: no posting date, cover date or creation date")
