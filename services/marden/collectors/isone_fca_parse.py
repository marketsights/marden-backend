"""Parsers for ISO-NE Forward Capacity Auction results: the results report, the obligations workbook, press releases.

- `parse_report` (fca-results-report.pdf, one section per FCA, newest first): the Capacity Commitment Period,
  the day the auction began and each capacity zone's payment rate ($/kW-month) from sentences such as
  "The payment rate for the Rest of Pool, Northern New England, and Maine Capacity Zones and associated
  external interfaces is 3.580/kW-month." ("The system closed ... with a Payment Rate of $2.001/kW Month."
  prices every zone; a sentence naming only an external interface is not a zone price).
- `parse_obligations` (fca_obligations.xlsx, one sheet per FCA, one row per resource): per capacity zone the
  sum of the Capacity Supply Obligations in the period's first month (MW; ISO-NE's "Total Capacity Acquired"
  is the same sum over all zones) and the load zones of the resources in it.
- `press_releases` / `release_date` (key stats page links, release PDFs): when the results were announced.
- `keystats_totals`: the key stats table's "Total Capacity Acquired (MW)" per FCA, a check on the sums.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urljoin

import openpyxl
import pymupdf

from marden.collectors.capacity_common import decimal
from marden.collectors.rates_common import ParseError

KEYSTATS_URL = "https://www.iso-ne.com/about/key-stats/markets"
REPORT_URL = "https://www.iso-ne.com/static-assets/documents/2018/05/fca-results-report.pdf"
OBLIGATIONS_URL = "https://www.iso-ne.com/static-assets/documents/2018/02/fca_obligations.xlsx"
ZONE_CODES = {"rest-of-pool": "ROP", "rest of pool": "ROP", "southeast new england": "SENE",
              "northern new england": "NNE", "maine": "MAINE"}
_ZONE_NAMES = (("ROP", r"\brest[- ]of[- ]pool\b"), ("SENE", r"\bsoutheast(?:ern)? new england\b"),
               ("NNE", r"\bnorthern new england\b"), ("MAINE", r"\bmaine\b"))
_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_DATE = rf"((?:{_MONTHS}) \d{{1,2}}, \d{{4}})"
_HEADING = re.compile(r"Forward Capacity (?:Auction|Market) \((?:FCA\) (\d+)|FCA (\d+)\))\s+Results?\b")
_RATE = re.compile(r"\$?\s?(\d+\.\d+)\s*/\s*kW[- ]?month", re.I)
_SENTENCE_BREAK = re.compile(r"(?<=\.)\s+(?=[A-Z•])|\s*[•]\s*|\s+x\s+(?=[A-Z])")
MAX_KW_MONTH = Decimal(50)  # $/kW-month; the highest FCA price so far is $17.728 (FCA 9, new SEMA/RI)


def _day(text: str) -> date:
    return datetime.strptime(text, "%B %d, %Y").date()


def zone_code(name: str) -> str:
    key = re.sub(r"\s+", " ", name.strip().lower())
    if key not in ZONE_CODES:
        raise ParseError(f"unknown ISO-NE capacity zone name {name!r}")
    return ZONE_CODES[key]


# ---------------------------------------------------------------- results report
@dataclass
class Section:
    fca: int
    page: int  # 1-based page of the section's heading
    ccp_start: date
    ccp_end: date
    auction_date: date | None
    auction_date_quote: str | None
    prices: dict[str, tuple[Decimal, str]] = field(default_factory=dict)  # zone code -> (rate, sentence)
    system_price: tuple[Decimal, str] | None = None  # "The system closed ..." (every zone)


def pdf_pages(data: bytes, what: str) -> tuple[list[str], dict[str, Any]]:
    if not data.lstrip()[:5] == b"%PDF-":
        raise ParseError(f"{what}: not a PDF (starts {data[:40]!r})")
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # pymupdf.FileDataError and friends: not a PDF
        raise ParseError(f"{what}: not a PDF ({type(exc).__name__}: {exc})") from None
    with doc:
        return [re.sub(r"\s+", " ", page.get_text()) for page in doc], dict(doc.metadata or {})


def prices_in(text: str) -> tuple[dict[str, tuple[Decimal, str]], tuple[Decimal, str] | None]:
    """Zone prices from a section's text: (zone -> (rate, sentence), system-wide rate or None)."""
    zones: dict[str, tuple[Decimal, str]] = {}
    system = None
    for sentence in _SENTENCE_BREAK.split(text):
        rates = _RATE.findall(sentence)
        if not rates or "payment rate" not in sentence.lower():
            continue
        head = sentence[: _RATE.search(sentence).start()].lower()
        named = [code for code, pattern in _ZONE_NAMES if re.search(pattern, head)]
        is_system = not named and re.search(r"\bthe system\b", head) is not None
        if not named and not is_system:
            continue  # an external interface's rate
        if len(set(rates)) > 1:
            raise ParseError(f"two payment rates in one zone sentence: {sentence[:200]!r}")
        rate = decimal(rates[0], "payment rate")
        if not Decimal(0) < rate <= MAX_KW_MONTH:
            raise ParseError(f"payment rate {rate} $/kW-month is implausible: {sentence[:200]!r}")
        if is_system:
            system = (rate, sentence.strip())
            continue
        for code in named:
            if code in zones and zones[code][0] != rate:
                raise ParseError(f"zone {code} has two payment rates ({zones[code][0]}, {rate})")
            zones[code] = (rate, sentence.strip())
    return zones, system


_NEW_DESIGN = re.compile(r"\b(?:Prompt|Seasonal) (?:Capacity )?Auction\b[^.]{0,80}\bResults?\b", re.I)


def unknown_sections(data: bytes) -> list[int]:
    """Pages of the results report that look like results of an auction design this parser does not read
    (ISO-NE's planned prompt / seasonal capacity auction)."""
    pages, _ = pdf_pages(data, "FCA results report")
    return [i + 1 for i, text in enumerate(pages) if _NEW_DESIGN.search(text) and not _HEADING.search(text)]


def parse_report(data: bytes, *, first_fca: int = 1) -> dict[int, Section]:
    pages, _ = pdf_pages(data, "FCA results report")
    starts = [(i, int(m[1] or m[2])) for i, text in enumerate(pages) if (m := _HEADING.search(text))]
    if not starts:
        raise ParseError("FCA results report: no 'Forward Capacity Auction (FCA) N Results' section")
    out: dict[int, Section] = {}
    for k, (page, fca) in enumerate(starts):
        if fca < first_fca or fca in out:
            continue
        end = starts[k + 1][0] if k + 1 < len(starts) else len(pages)
        text = " ".join(pages[page:end])
        ccp = re.search(rf"begins on {_DATE} and ends on {_DATE}", text) or re.search(
            rf"Capacity Commitment Period of {_DATE} to {_DATE}", text)
        if not ccp:
            raise ParseError(f"FCA {fca}: no Capacity Commitment Period dates in the section on page {page + 1}")
        began = re.search(rf"FCA {fca} began at [^.]*? on {_DATE}", text)
        zones, system = prices_in(text)
        if not zones and system is None:
            raise ParseError(f"FCA {fca}: no capacity zone payment rate in the section on page {page + 1}")
        out[fca] = Section(fca, page + 1, _day(ccp[1]), _day(ccp[2]), _day(began[1]) if began else None,
                           began[0] if began else None, zones, system)
    return out


# ---------------------------------------------------------------- obligations workbook
@dataclass
class ZoneObligation:
    code: str
    name: str
    mw: Decimal
    resources: int
    load_zones: list[str]


@dataclass
class Obligations:
    fca: int
    sheet: str
    month: date  # the CSO column summed (the commitment period's first month)
    zones: dict[str, ZoneObligation]


def _col(header: tuple, name: str, sheet: str) -> int:
    for j, cell in enumerate(header):
        if str(cell or "").strip().lower() == name.lower():
            return j
    raise ParseError(f"fca_obligations '{sheet}': no '{name}' column")


def parse_obligations(data: bytes, *, first_fca: int = 1) -> dict[int, Obligations]:
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # zipfile.BadZipFile, KeyError, InvalidFileException: not a workbook
        raise ParseError(f"fca_obligations: not an xlsx workbook ({type(exc).__name__}: {exc})") from None
    out: dict[int, Obligations] = {}
    for ws in wb.worksheets:
        m = re.fullmatch(r"FCA\s*(\d+)", ws.title.strip())
        if not m or int(m[1]) < first_fca:
            continue
        ws.reset_dimensions()  # read-only sheets may carry a stale dimension; read every row
        rows = ws.iter_rows(values_only=True)
        header = next(rows, ())
        zone_col, load_col = _col(header, "Capacity Zone Name", ws.title), _col(header, "Load Zone", ws.title)
        month_col = next((j for j, c in enumerate(header) if isinstance(c, datetime | date)), None)
        if month_col is None:
            raise ParseError(f"fca_obligations '{ws.title}': no monthly CSO columns")
        month = header[month_col]
        sums: dict[str, list[Any]] = {}
        for row in rows:
            name = str(row[zone_col] or "").strip() if len(row) > zone_col else ""
            if not name:
                continue
            cso = row[month_col]
            if cso is None:
                continue
            mw = decimal(cso, f"{ws.title} CSO")
            if mw < 0:
                raise ParseError(f"fca_obligations '{ws.title}': negative CSO {mw}")
            entry = sums.setdefault(zone_code(name), [name, Decimal(0), 0, set()])
            entry[1] += mw
            entry[2] += 1
            if row[load_col]:
                entry[3].add(str(row[load_col]).strip().upper())
        if not sums:
            raise ParseError(f"fca_obligations '{ws.title}': no resources with a capacity zone")
        out[int(m[1])] = Obligations(int(m[1]), ws.title, month.date() if isinstance(month, datetime) else month, {
            code: ZoneObligation(code, v[0], v[1], v[2], sorted(v[3])) for code, v in sorted(sums.items())})
    return out


# ---------------------------------------------------------------- key stats page and press releases
_ANCHOR = re.compile(r'<a\b[^>]*\bhref="(?P<href>[^"]+\.pdf)"[^>]*>(?P<text>.*?)</a>', re.I | re.S)


def press_releases(page: bytes, base_url: str = KEYSTATS_URL) -> dict[int, str]:
    """FCA number -> its results press release (the initial-results release when there are two)."""
    out: dict[int, str] = {}
    for a in _ANCHOR.finditer(page.decode("utf-8", errors="replace")):
        name = a["href"].rsplit("/", 1)[-1].lower()
        m = re.search(r"fca[_-]?(\d+)_", name)
        if not m or "result" not in name:
            continue
        fca = int(m[1])
        if fca not in out or ("initial" in name and "initial" not in out[fca].lower()):
            out[fca] = urljoin(base_url, a["href"])
    return out


def keystats_totals(page: bytes) -> dict[int, Decimal]:
    """'FCA 18 in 2024 for CCP 2027/2028' | '31,556' -> {18: 31556}."""
    html = page.decode("utf-8", errors="replace")
    out: dict[int, Decimal] = {}
    for row in re.findall(r"(?s)<tr.*?</tr>", html):
        cells = [re.sub(r"<[^>]+>|\s+", " ", c).strip() for c in re.findall(r"(?s)<t[hd][^>]*>(.*?)</t[hd]>", row)]
        if len(cells) >= 2 and (m := re.match(r"FCA (\d+) in \d{4} for CCP", cells[0])):
            total = re.sub(r"[^\d.]", "", cells[1])
            if total:
                out[int(m[1])] = Decimal(total)
    return out


def release_date(data: bytes, what: str) -> tuple[date, str]:
    """The date line of an ISO-NE press release: 'Holyoke, MA—February 9, 2024—...'."""
    pages, _ = pdf_pages(data, what)
    first = pages[0] if pages else ""
    m = re.search(rf"Holyoke, MA\s*[\u2014\u2013-]+\s*{_DATE}", first) or re.search(_DATE, first[:2000])
    if not m:
        raise ParseError(f"{what}: no release date on the first page")
    return _day(m[1]), f"{what}: \"{m[0]}\""
