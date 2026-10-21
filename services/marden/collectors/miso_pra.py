"""MISO Planning Resource Auction (PRA) results -> capacity series facts (tag F, source MISO). See capacity_common.

Since planning year 2023/24 the PRA clears four seasons (summer Jun-Aug, fall Sep-Nov, winter Dec-Feb, spring
Mar-May) and prices each Local Resource Zone (LRZ 1-10) in $/MW-day. MISO's "PRA Results Posting" deck has one
"<Season> PRA Results by Zone" table per season; per zone this collector writes `clearing_price` (the
"ACP ($/MW-Day)" row, USD/MW-day) and `cleared_mw` (the "Committed (Offer Cleared + FRAP)" row, MW of seasonal
accredited capacity) on 'capacity:miso:lrz<N>:<planning year>-<season>' (e.g. capacity:miso:lrz9:2025-2026-summer),
with delivery dates of the season. valid_from = the deck's publication date (a corrected repost carries the
repost date). The decks state no separate auction date, so `auction_date` is not written.

Documents: www.misoenergy.org answers automated clients with a Cloudflare challenge (see TERMS_NOTE), so the
deck cannot be discovered from the Resource Adequacy page; POSTINGS lists the decks' published URLs on MISO's
document CDN (cdn.misoenergy.org, which serves them without a challenge). A person adds each new planning
year's deck (posted in late April; MISO replaces a deck with "_Corrections" when it reposts). FACT_SHEETS are
stored too: the 2026 fact sheet carries the 2026/27 prices only as an image, so it is queued for extraction.
A URL that no longer answers is recorded on the source (the run fails) and nothing is invented for it.

Cadence: monthly (3rd, 11:29 UTC); results post once a year (20th business day of April).
`python -m marden collect miso_pra`.
"""

from __future__ import annotations

import argparse
import re
import statistics
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pymupdf

from marden import cli
from marden.collectors import capacity_zones
from marden.collectors.capacity_common import (
    MW,
    USD_MW_DAY,
    Auction,
    CapacityWriter,
    Value,
    ZoneResult,
    decimal,
    zone_slug,
)
from marden.collectors.capacity_headlines import Headline, announce
from marden.collectors.rates_common import ParseError, RunStats, collect, print_stats, today_et
from marden.core import db, http
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, FetchError, fetch_document
from marden.core.queue import app
from marden.core.sources import declare_source

SOURCE_ID = "miso_pra"
PRODUCER = "collector:miso_pra"
SOURCE_NAME = "MISO"
HOST = "cdn.misoenergy.org"
CRON_UTC = "29 11 3 * *"
ZONES = tuple(f"Z{n}" for n in range(1, 11))
COLUMNS = (*ZONES, "ERZ", "North", "South", "System")


@dataclass(frozen=True)
class Posting:
    planning_year: str  # '2025-2026'
    url: str
    note: str = ""


POSTINGS = (
    Posting("2024-2025", f"https://{HOST}/2024%20PRA%20Results%20Posting%2020240425632665.pdf",
            "reposted 4/26/24 (slides 28, 40 corrected; zone tables unchanged)"),
    Posting("2025-2026", f"https://{HOST}/2025%20PRA%20Results%20Posting%2020250529_Corrections694160.pdf",
            "corrected repost of 05/29/2025; the 04/28/2025 original is no longer served"),
    Posting("2026-2027", f"https://{HOST}/2026%20PRA%20Results%20Posting%2020260428754715.pdf",
            "posted 04/28/2026; on 2026-10-21 the CDN answers 403 AccessDenied (removed or replaced)"),
)
FACT_SHEETS = (
    Posting("2026-2027", f"https://{HOST}/2026%20Planning%20Resource%20Auction%20Results754649.pdf",
            "one-page fact sheet of 04/28/2026: seasonal prices only in an image"),
)
TERMS_NOTE = (
    "Checked 2026-10-21. The terms could not be read directly: www.misoenergy.org, its legal pages "
    "(https://www.misoenergy.org/meet-miso/legal-and-privacy/) and robots.txt answer automated clients with HTTP 403 "
    "and a Cloudflare managed challenge (\"Just a moment... Enable JavaScript and cookies to continue\"); Marden "
    "does not solve or route around it, so it never collects from www.misoenergy.org. A search-engine excerpt "
    "of that page (not verified against the live page) reads: \"The Privacy Notice and Terms of Use "
    "(collectively \"Terms\") govern your use of this Website and the App\"; \"All materials published on this "
    "Website or the App, including written content, photographs, graphics, images, and logos, are protected by "
    "MISO's copyrights or trademarks\"; \"You are not permitted to modify, publish, transmit ... reproduce, "
    "create derivative works of, distribute ... in whole or in part\". No clause forbidding automated access was "
    "found. The result decks are published for the public on https://cdn.misoenergy.org (no challenge; "
    "cdn.misoenergy.org/robots.txt does not exist). Marden stores the decks privately and writes the published "
    "clearing prices and cleared MW (facts) with a link to MISO's deck; it never republishes the decks. Needs "
    "a legal read of the full Terms before the prices are shown to customers. A few requests a month, 0.5/s."
)
SOURCE = declare_source(SOURCE_ID, "MISO Planning Resource Auction", "portal", f"https://{HOST}", 31 * 24 * 60,
                        TERMS_NOTE)
http.set_rate_limit(HOST, 0.5)

_PY = re.compile(r"Planning Year\s+(\d{4})\s*[-/\u2013]\s*(\d{2,4})")
_COVER_DATE = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|"
                         r"December) (\d{1,2}), (\d{4})\b")
_HEADER_DATE = re.compile(r"^(\d{2}/\d{2}/\d{4}):", re.M)
_SEASON_TITLE = re.compile(r"^(Summer|Fall|Winter|Spring) (\d{4})(?:/\d{2,4})? PRA Results by Zone\s*$", re.M)
_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
SEASON_MONTHS = {"summer": (6, 8), "fall": (9, 11), "winter": (12, 2), "spring": (3, 5)}


@dataclass(frozen=True)
class SeasonTable:
    season: str  # 'summer'
    year: int  # the season's calendar year as titled (winter: the December year)
    acp: dict[str, Any]  # 'Z1' -> Decimal USD/MW-day
    committed: dict[str, Any]  # 'Z1' -> Decimal MW
    page: int


@dataclass(frozen=True)
class PostingResults:
    planning_year: int  # first year: 2025 for 2025-26
    published: date
    published_loc: str
    tables: list[SeasonTable]


def season_dates(season: str, py_start: int) -> tuple[date, date]:
    first, last = SEASON_MONTHS[season]
    start_year = py_start if season in ("summer", "fall", "winter") else py_start + 1
    end_year = start_year + 1 if season == "winter" else start_year
    end_day = date(end_year, last % 12 + 1, 1) if last != 12 else date(end_year + 1, 1, 1)
    return date(start_year, first, 1), date.fromordinal(end_day.toordinal() - 1)


def _rows(numbers: list[tuple[float, float, str]]) -> list[tuple[float, list[tuple[float, str]]]]:
    """Numeric words (x-center, y-center, text) grouped into rows by y-center (3 pt tolerance)."""
    rows: list[tuple[float, list[tuple[float, str]]]] = []
    for x, y, text in sorted(numbers, key=lambda n: n[1]):
        if rows and abs(rows[-1][0] - y) <= 3:
            rows[-1][1].append((x, text))
        else:
            rows.append((y, [(x, text)]))
    return rows


def parse_table(page: pymupdf.Page, what: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(ACP by zone, committed MW by zone) from one 'Results by Zone' slide, read by word positions: each number
    is assigned to the nearest column header (Z1..Z10, ERZ, North, South, System), each label to the first row
    of numbers at or below its first line (labels are centred in their cells)."""
    words = page.get_text("words")
    header = {w[4]: w for w in words if w[4] in COLUMNS}
    if any(z not in header for z in ZONES):
        raise ParseError(f"{what}: zone header Z1..Z10 not found")
    header_y = statistics.median(header[z][3] for z in ZONES)
    centers = {c: (w[0] + w[2]) / 2 for c, w in header.items() if abs(w[3] - header_y) < 4}
    pitch = statistics.median(centers[f"Z{n + 1}"] - centers[f"Z{n}"] for n in range(1, 10))
    left = header["Z1"][0] - pitch / 2
    numbers = [((w[0] + w[2]) / 2, (w[1] + w[3]) / 2, w[4]) for w in words
               if w[1] > header_y and w[0] >= left and _NUMBER.fullmatch(w[4])]
    rows = _rows(numbers)

    def row_after(label: str) -> dict[str, Any]:
        word = next((w for w in words if w[4] == label and w[0] < left and w[1] > header_y), None)
        if word is None:
            raise ParseError(f"{what}: row label {label!r} not found")
        y = (word[1] + word[3]) / 2
        row = next((cells for cy, cells in rows if cy >= y - 3), None)
        if row is None:
            raise ParseError(f"{what}: no numbers beside {label!r}")
        out: dict[str, Any] = {}
        for x, text in row:
            col = min(centers, key=lambda c: abs(centers[c] - x))
            if abs(centers[col] - x) > pitch / 2 or col not in ZONES:
                continue
            if col in out:
                raise ParseError(f"{what}: two {label} values under {col}")
            out[col] = decimal(text, f"{what} {label} {col}")
        missing = [z for z in ZONES if z not in out]
        if missing:
            raise ParseError(f"{what}: {label} row lacks {missing}")
        return out

    return row_after("ACP"), row_after("Committed")


def parse_posting(data: bytes) -> PostingResults:
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except (RuntimeError, ValueError) as exc:
        raise ParseError(f"MISO PRA deck is not a readable PDF: {exc}") from None
    with doc:
        first = doc[0].get_text()
        py = _PY.search(first) or (_PY.search(doc[1].get_text()) if doc.page_count > 1 else None)
        if py is None:
            raise ParseError("MISO PRA deck: 'Planning Year YYYY-YY' not found on the cover")
        py_start = int(py.group(1))
        published, loc = _published(doc)
        tables = []
        for page in doc:
            m = _SEASON_TITLE.search(page.get_text())
            if m is None:
                continue
            season = m.group(1).lower()
            what = f"PY {py_start} {m.group(0).strip()} (page {page.number + 1})"
            acp, committed = parse_table(page, what)
            tables.append(SeasonTable(season, int(m.group(2)), acp, committed, page.number + 1))
    seasons = sorted(t.season for t in tables)
    if seasons != sorted(SEASON_MONTHS):
        raise ParseError(f"MISO PRA deck PY {py_start}: season tables {seasons}, expected all four")
    for t in tables:
        expected = py_start + (1 if t.season == "spring" else 0)
        if t.year != expected:
            raise ParseError(f"MISO PRA deck PY {py_start}: {t.season} table titled {t.year}")
    return PostingResults(py_start, published, loc, tables)


def _published(doc: pymupdf.Document) -> tuple[date, str]:
    """The deck's date: a full date on the cover ('April 25, 2024'), else the slide header date of a repost
    ('05/29/2025: MISO Planning Resource Auction ...')."""
    cover = _COVER_DATE.search(doc[0].get_text())
    if cover:
        return datetime.strptime(" ".join(cover.groups()), "%B %d %Y").date(), f"cover date {cover.group(0)!r}"
    for page in doc:
        m = _HEADER_DATE.search(page.get_text())
        if m:
            day = datetime.strptime(m.group(1), "%m/%d/%Y").date()
            return day, f"slide header date {m.group(1)} (page {page.number + 1})"
    raise ParseError("MISO PRA deck: no publication date on the cover or slide headers")


def auctions(results: PostingResults, *, source_url: str, document_id: int | None) -> list[Auction]:
    py = results.planning_year
    out = []
    for t in sorted(results.tables, key=lambda t: season_dates(t.season, py)[0]):
        start, end = season_dates(t.season, py)
        label = f"{t.season.title()} {t.year}" + (f"/{str(t.year + 1)[2:]}" if t.season == "winter" else "")
        auction = Auction(
            iso="miso", period=f"{py}-{py + 1}-{t.season}",
            name=f"MISO {py}-{py + 1} Planning Resource Auction ({label})",
            delivery_start=start, delivery_end=end, published=results.published, published_loc=results.published_loc,
            source_url=source_url, document_id=document_id,
        )
        for z in ZONES:
            code = f"LRZ{z[1:]}"
            where = f"page {t.page}: {label} PRA Results by Zone, {z}"
            auction.zones.append(ZoneResult(code, {
                "clearing_price": Value(t.acp[z], USD_MW_DAY, f"{where}, ACP ($/MW-Day)"),
                "cleared_mw": Value(t.committed[z], MW, f"{where}, Committed (Offer Cleared + FRAP)"),
            }))
        out.append(auction)
    return out


def load_document(doc: DocumentRow, stats: RunStats, *, today: date) -> list[Auction]:
    found = auctions(parse_posting(doc.read()), source_url=doc.url, document_id=doc.id)
    headlines: dict[str, list[str]] = {}
    with db.transaction() as conn:
        writer = CapacityWriter(conn, stats, source_name=SOURCE_NAME, producer=PRODUCER)
        new = sum(writer.write(a) for a in found)
        if new:
            for a in found:
                for z in a.zones:
                    season = a.period.rsplit("-", 1)[1]
                    headlines.setdefault(z.zone, []).append(f"{season} ${z.values['clearing_price'].value:,.2f}")
        py = found[0].period.rsplit("-", 1)[0]
        lines = [Headline(zone_slug(code), f"MISO {py} Planning Resource Auction: {code} cleared "
                          f"{', '.join(prices)} /MW-day", found[0].published, doc.url, doc.id)
                 for code, prices in headlines.items()]
        announce(conn, stats, market="miso", source_name=SOURCE_NAME, headlines=lines, series_zone=zone_slug,
                 today=today)
    return found


def run(*, postings: tuple[Posting, ...] = POSTINGS, fact_sheets: tuple[Posting, ...] = FACT_SHEETS,
        today: date | None = None, fetcher: Fetcher | None = None) -> dict[str, Any]:
    today = today or today_et()

    def body(stats: RunStats) -> None:
        with db.transaction() as conn:  # zones first, so a new auction reaches the right V1 sites
            zones = capacity_zones.assign(conn, RunStats())
        stats.extra["v1_zones"] = {k: v for k, v in zones["v1_sites"].items() if v.startswith("miso")}
        errors: list[Exception] = []
        loaded: dict[str, str] = {}
        for p in (*postings, *fact_sheets):
            sheet = p in fact_sheets
            try:
                doc = fetch_document(
                    SOURCE_ID, p.url, doc_type="miso_pra_fact_sheet" if sheet else "miso_pra_results",
                    title=f"MISO PRA {p.planning_year} {'fact sheet' if sheet else 'results posting'}",
                    external_id=p.planning_year, extract_state="queued" if sheet else "none", fetcher=fetcher,
                )
                stats.document(doc)
                if not sheet:
                    found = load_document(doc, stats, today=today)
                    loaded[p.planning_year] = f"{len(found)} seasons, published {found[0].published}"
            except (FetchError, ParseError) as exc:
                errors.append(exc)
                note = f"{p.planning_year} {p.url}: {type(exc).__name__}: {exc}"
                stats.extra.setdefault("errors", []).append(note[:400])
        stats.extra["loaded"] = loaded
        if errors:
            raise errors[0]

    return collect(SOURCE, body)


@app.periodic(cron=CRON_UTC, periodic_id="monthly_3rd_1129_utc")
@app.task(name="collect.miso_pra", queue="collect", queueing_lock="collect.miso_pra", lock="collect.miso_pra")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


@cli.action("collect", "miso_pra", help="MISO Planning Resource Auction seasonal clearing prices by LRZ")
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run())
