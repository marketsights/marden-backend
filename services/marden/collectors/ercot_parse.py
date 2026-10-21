"""Parsers for the ERCOT large-load collector (marden.collectors.ercot).

- `meetings` / `report_links`: the Large Load Working Group page lists its meeting pages
  (/calendar/MMDDYYYY-LLWG-Meeting...); a meeting page links its documents under /files/docs/YYYY/MM/DD/.
  The Large Load Integration Team's monthly "Large Load Interconnection Status Update" is posted there under
  titles such as "February TAC Report", "March TAC Report Updated_03262026" or "June 19 LLWG Report".
- `deck_units`: the text of a deck, one unit per slide (pptx, in presentation order) or page (pdf).
- `parse_status_update`: whether a deck is a status update (its first slide says so), its date, the data
  as-of date it states, and the headline figures ERCOT states in words. Every chart in these decks (queue MW
  by status, zone, TSP, size, submittal and in-service year) is a picture, so those numbers are left to the
  extraction workers; nothing is read off an image here.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import pymupdf

from marden.collectors.rates_common import ParseError

HOST = "www.ercot.com"
MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"), start=1)}
_MONTH = r"(January|February|March|April|May|June|July|August|September|October|November|December)"
_MEETING = re.compile(r"^/calendar/(\d{2})(\d{2})(\d{4})-LLWG-Meeting[^/]*$", re.I)
_DOC_PATH = re.compile(r"^/files/docs/(\d{4})/(\d{2})/(\d{2})/[^/]+\.(pdf|pptx)$", re.I)
# "Report" may run into a suffix ("April 24 LLWG Report_042426"), so no \b after it
_REPORT_TITLE = re.compile(r"\b(?:TAC|LLWG)[\s_-]+Report(?![a-z])|\bInterconnection[\s_-]+Status\b", re.I)
_STATUS_TITLE = re.compile(r"large\s+load\s+interconnection\s+status", re.I)
_DECK_DATE = re.compile(_MONTH + r"\s+(\d{1,2}),\s+(\d{4})")
_AS_OF = re.compile(r"current\s+as\s+of\s+" + _MONTH + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", re.I)
_MW = r"([\d,]+(?:\.\d+)?)\s*MW"
FIGURES = (  # series id, pattern (group 1 = MW) as ERCOT words it on the "Observations" slides
    ("ercot_large_load_approved_to_energize_mw",
     re.compile(r"Of the " + _MW + r" that have received Approval to Energize")),
    ("ercot_large_load_peak_nonsimultaneous_mw",
     re.compile(r"non-\s?simultaneous monthly peak consumption of " + _MW, re.I)),
    ("ercot_large_load_peak_simultaneous_mw",
     re.compile(r"(?<!non-)(?<!non- )\bsimultaneous monthly peak consumption of " + _MW, re.I)),
)
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


# ---------------------------------------------------------------- pages
@dataclass(frozen=True)
class Link:
    url: str
    text: str


class _Anchors(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.out.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def links(html: bytes, base: str) -> list[Link]:
    """Every <a href> on a page, made absolute (fragment and query dropped), with its text."""
    parser = _Anchors()
    parser.feed(html.decode("utf-8", errors="replace"))
    out = []
    for href, label in parser.out:
        u = urlparse(urljoin(base, href.strip()))
        if u.scheme in ("http", "https") and u.hostname:
            out.append(Link(f"https://{u.hostname}{u.path}", label))
    return out


def meetings(html: bytes, base: str) -> list[tuple[date, str]]:
    """The LLWG meeting pages a page links to, (meeting date, url), oldest first."""
    found: dict[str, date] = {}
    for link in links(html, base):
        u = urlparse(link.url)
        m = _MEETING.match(u.path)
        if u.hostname == HOST and m:
            try:
                found[link.url] = date(int(m[3]), int(m[1]), int(m[2]))
            except ValueError:
                continue
    return sorted((d, u) for u, d in found.items())


def posted_date(url: str) -> date | None:
    """The posting date in an ERCOT document path (/files/docs/YYYY/MM/DD/...)."""
    m = _DOC_PATH.match(urlparse(url).path)
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def report_links(html: bytes, base: str) -> list[Link]:
    """Documents on a meeting page titled like the status update ('... TAC Report', '... LLWG Report')."""
    out: dict[str, Link] = {}
    for link in links(html, base):
        if urlparse(link.url).hostname != HOST or posted_date(link.url) is None:
            continue
        name = urlparse(link.url).path.rsplit("/", 1)[1]
        if _REPORT_TITLE.search(link.text) or _REPORT_TITLE.search(name):
            out.setdefault(link.url, link)
    return sorted(out.values(), key=lambda lk: (posted_date(lk.url) or date.min, lk.url))


def is_challenge(body: bytes) -> bool:
    """An Imperva (Incapsula) bot-protection page instead of the content asked for."""
    head = body[:4096]
    return b"Incapsula incident ID" in body or (b"_Incapsula_Resource" in head and b"main-iframe" in head)


# ---------------------------------------------------------------- decks
@dataclass(frozen=True)
class Unit:
    loc: str  # 'slide 8' / 'page 6'
    text: str  # whitespace collapsed; paragraphs joined with '\n'


def _paragraphs(xml: bytes) -> list[str]:
    root = ET.fromstring(xml)
    out = []
    for p in root.iter(f"{_A}p"):
        parts = []
        for node in p.iter():
            if node.tag in (f"{_A}t",) and node.text:
                parts.append(node.text)
            elif node.tag == f"{_A}br":
                parts.append(" ")
        text = " ".join("".join(parts).split())
        if text:
            out.append(text)
    return out


def _slide_order(z: zipfile.ZipFile) -> list[str]:
    names = set(z.namelist())
    numbered = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                      key=lambda n: int(re.findall(r"\d+", n)[0]))
    if "ppt/presentation.xml" not in names or "ppt/_rels/presentation.xml.rels" not in names:
        return numbered
    rels = {r.get("Id"): r.get("Target", "") for r in ET.fromstring(z.read("ppt/_rels/presentation.xml.rels")).iter(
        f"{_REL}Relationship")}
    ordered = []
    for sld in ET.fromstring(z.read("ppt/presentation.xml")).iter(f"{_P}sldId"):
        target = rels.get(sld.get(f"{_R}id"), "").lstrip("/")
        name = target if target.startswith("ppt/") else f"ppt/{target}"
        if name in names:
            ordered.append(name)
    return ordered or numbered


def deck_units(data: bytes) -> list[Unit]:
    """The text of a pptx (per slide) or pdf (per page). ParseError for anything else."""
    if data[:5] == b"%PDF-":
        try:
            with pymupdf.open(stream=data, filetype="pdf") as pdf:
                return [Unit(f"page {i + 1}", " ".join(page.get_text().split())) for i, page in enumerate(pdf)]
        except (RuntimeError, ValueError) as exc:
            raise ParseError(f"unreadable PDF: {exc}") from None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            slides = _slide_order(z)
            if not slides:
                raise ParseError("zip file without PowerPoint slides")
            return [Unit(f"slide {i + 1}", "\n".join(_paragraphs(z.read(n)))) for i, n in enumerate(slides)]
    except zipfile.BadZipFile:
        raise ParseError("not a PowerPoint (pptx) or PDF file") from None
    except ET.ParseError as exc:
        raise ParseError(f"unreadable slide XML: {exc}") from None


# ---------------------------------------------------------------- the status update
@dataclass(frozen=True)
class Figure:
    series_id: str
    value: int | float  # MW
    quote: str  # the sentence it is stated in
    loc: str


@dataclass(frozen=True)
class StatusReport:
    report_date: date  # the date on the title slide
    as_of: date  # "current as of ..." when stated, else the report date
    as_of_stated: bool
    figures: list[Figure]


def _mw(text: str) -> int | float:
    value = Decimal(text.replace(",", ""))
    return int(value) if value == value.to_integral_value() else float(value)


def _sentence(text: str, start: int, end: int) -> str:
    left = max(text.rfind(". ", 0, start) + 2, text.rfind("\n", 0, start) + 1, text.rfind("•", 0, start) + 1, 0)
    stops = [i for i in (text.find(". ", end), text.find("\n", end), text.find(" \u2013", end)) if i >= 0]
    right = min(stops) if stops else len(text)
    if right < len(text) and text[right] == ".":
        right += 1
    return " ".join(text[left:right].split()).rstrip(" .") + "."


def _as_of(units: list[Unit], report_date: date) -> date | None:
    for unit in units:
        m = _AS_OF.search(unit.text)
        if not m:
            continue
        month, day = MONTHS[m[1].lower()], int(m[2])
        year = int(m[3]) if m[3] else report_date.year
        try:
            day_ = date(year, month, day)
        except ValueError:
            continue
        if not m[3] and day_ > report_date:  # "as of December 30" on a January deck
            day_ = date(year - 1, month, day)
        return day_
    return None


def parse_status_update(data: bytes) -> StatusReport | None:
    """The status update's date, as-of and stated figures; None when the deck is not a status update."""
    units = deck_units(data)
    if not units or not _STATUS_TITLE.search(units[0].text):
        return None
    m = _DECK_DATE.search(units[0].text)
    if not m:
        raise ParseError("status update without a date on its title slide")
    report_date = date(int(m[3]), MONTHS[m[1].lower()], int(m[2]))
    as_of = _as_of(units, report_date)
    figures: list[Figure] = []
    for series_id, pattern in FIGURES:
        for unit in units:
            hit = pattern.search(unit.text)
            if hit:
                figures.append(Figure(series_id, _mw(hit[1]), _sentence(unit.text, hit.start(), hit.end()), unit.loc))
                break
    return StatusReport(report_date, as_of or report_date, as_of is not None, figures)
