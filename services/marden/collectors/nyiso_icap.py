"""NYISO Installed Capacity (ICAP) auction results -> capacity series facts (tag F, source NYISO). See capacity_common.

NYISO sells UCAP in three auctions, all posted on the public ICAP Automated Market System (icappublic.nyiso.com,
linked from https://www.nyiso.com/installed-capacity-market as "ICAP Market Results"):
- the Capability Period (strip) auction: one price per locality for the whole six-month period;
- the monthly auctions: held before each month, selling that month and the rest of the capability period;
- the spot market auction: the month's final, demand-curve-priced auction.
Localities: NYCA, G-J Locality (Lower Hudson Valley, zones G-J), NYC (zone J), LI (zone K). External areas
(HQ, IESO, NE, PJM) are not written. NYISO prices in $/kW-month: `clearing_price_native` ('USD/kW-month') is
the published price and `clearing_price` ('USD/MW-day') its conversion by capacity_common (x 1000 / 30.4167,
noted in quote_loc). `cleared_mw` is "Awarded (MW)"; for the spot auction, Awarded Deficiency + Awarded Excess.
Series: 'capacity:nyiso:<nyca|g-j|nyc|li>:' + 'summer-2026-strip' | '2026-07-monthly-2026-06' (July capacity
from the June monthly auction) | '2026-10-spot'. The pages give the results' "Posted Date" (valid_from) but not
the auction's bidding dates, so `auction_date` is not written. A site's `capacity_zone` is its NYISO load zone
(A-K, capacity_zones); `series_zone` maps it to the locality its capacity is priced in.

Pages (raw first: every page is stored before parsing; a GET with display=Display returns the same summary as
the site's form POST): the season list (JSON at /ucap/rest/seasons/public), then for each capability period
from FIRST_SEASON_START to the next one: the strip page, six monthly pages and six spot pages. A page whose
results are loaded, or that was fetched after its period ended, is not fetched again. "No data matches your
filter criteria" (not posted yet) writes nothing.

TERMS (checked 2026-10-21): see TERMS_NOTE; nothing restricts automated access to these pages.
Cadence: monthly (2nd, 11:17 UTC); strip results post in early April and October, monthly auctions mid-month,
spot auctions in the last days of the month. `python -m marden collect nyiso_icap`.
"""

from __future__ import annotations

import argparse
import calendar
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any

from marden import cli
from marden.collectors import capacity_zones
from marden.collectors.capacity_common import (
    MW,
    USD_KW_MONTH,
    USD_MW_DAY,
    Auction,
    CapacityWriter,
    Value,
    ZoneResult,
    conversion_note,
    decimal,
    kw_month_to_mw_day,
    zone_slug,
)
from marden.collectors.capacity_headlines import Headline, announce
from marden.collectors.rates_common import ParseError, RunStats, collect, document_loaded, print_stats, today_et
from marden.core import db, documents, http
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, FetchError, fetch_document
from marden.core.queue import app
from marden.core.sources import declare_source

SOURCE_ID = "nyiso_icap"
PRODUCER = "collector:nyiso_icap"
SOURCE_NAME = "NYISO"
HOST = "icappublic.nyiso.com"
BASE = f"https://{HOST}/ucap"
SEASONS_URL = f"{BASE}/rest/seasons/public"
TERMS_URL = "https://www.nyiso.com/legal-notice"
FIRST_SEASON_START = date(2021, 5, 1)  # Summer 2021: five capability years of history
LOOKAHEAD_DAYS = 62  # the next period's strip auction posts about a month before it starts
EVENT_WINDOW_DAYS = 45  # announce auctions posted since about the last monthly check, not the backfill
CRON_UTC = "17 11 2 * *"
PAGES = {kind: f"auc_view_{kind}_detail.do" for kind in ("strip", "monthly", "spot")}
# page label -> zone code (series zone = zone_slug(code)): NYCA, Lower Hudson Valley G-J, New York City, Long Island
LOCALITIES = {"NYCA": "NYCA", "G-J Locality": "G-J", "NYC": "NYC", "LI": "LI"}
# NYISO ICAP Manual localities: NYC = load zone J, LI = zone K, G-J Locality = zones G-J; the rest is NYCA.
ZONE_LOCALITY = {"J": "NYC", "K": "LI", "G": "G-J", "H": "G-J", "I": "G-J"}
TERMS_NOTE = (
    f"Checked 2026-10-21. NYISO Legal Notice ({TERMS_URL}): \"The NYISO maintains this Web site for the benefit "
    "of its Market Participants and other authorized users. The materials on this site are provided \"as is.\"\"; "
    "\"Access to this Web site does not confer any license or ownership interest in either the form or content of "
    "the Web site ... Downloading, republishing, retransmitting, reproducing, or other use of any image or video "
    "on this website as a stand-alone file is strictly prohibited\" (images and video only; no clause on market "
    "data or automated access). The ICAP market results pages are public (username 'public', no login) and "
    "https://icappublic.nyiso.com/robots.txt does not exist (404); www.nyiso.com/robots.txt does not cover that "
    "host. Marden stores the pages and writes the published prices and awards with a link to NYISO; it does not "
    "republish NYISO's pages. About 15 pages a month at 1 request/second."
)
SOURCE = declare_source(SOURCE_ID, "NYISO ICAP auctions", "portal", f"https://{HOST}", 31 * 24 * 60, TERMS_NOTE)
http.set_rate_limit(HOST, 1.0)


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


@dataclass(frozen=True)
class Season:
    id: int
    description: str  # 'Summer 2026', 'Winter 2026-2027'
    start: date
    end: date

    @property
    def slug(self) -> str:
        return self.description.lower().replace(" ", "-")

    def months(self) -> list[date]:
        out, day = [], self.start
        while day <= self.end:
            out.append(day)
            day = month_end(day.year, day.month) + timedelta(days=1)
        return out


def parse_seasons(data: bytes) -> list[Season]:
    try:
        payload = json.loads(data)
        rows = payload["rows"]
    except (ValueError, KeyError, TypeError):
        raise ParseError("NYISO season list is not the expected JSON ({rows: [...]})") from None
    out = []
    for r in rows:
        try:
            start = datetime.strptime(r["startDate"], "%m/%d/%Y %H:%M:%S").date()
            end = datetime.strptime(r["endDate"], "%m/%d/%Y %H:%M:%S").date()
            out.append(Season(int(r["id"]), str(r["description"]).strip(), start, end))
        except (KeyError, TypeError, ValueError):
            raise ParseError(f"NYISO season row not understood: {str(r)[:200]}") from None
    for s in out:
        if not re.fullmatch(r"(Summer \d{4}|Winter \d{4}-\d{4})", s.description) or len(s.months()) != 6:
            raise ParseError(f"unexpected NYISO capability period {s.description!r} {s.start}..{s.end}")
    return sorted(out, key=lambda s: s.start)


def seasons_due(seasons: list[Season], today: date, first: date = FIRST_SEASON_START) -> list[Season]:
    return [s for s in seasons if s.start >= first and s.start <= today + timedelta(days=LOOKAHEAD_DAYS)]


def page_url(kind: str, season: Season, month: date | None = None) -> str:
    url = f"{BASE}/public/{PAGES[kind]}?seasonId={season.id}"
    if month is not None:
        url += f"&month={month:%m/%Y}"
    return url + "&display=Display"


def pages(season: Season) -> list[tuple[str, str, date | None]]:
    out: list[tuple[str, str, date | None]] = [("strip", page_url("strip", season), None)]
    for kind in ("monthly", "spot"):
        out += [(kind, page_url(kind, season, m), m) for m in season.months()]
    return out


# ---------------------------------------------------------------- parse
class _Rows(HTMLParser):
    """Table rows as lists of cell texts (innermost row wins for nested tables); <br> becomes a newline."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._stack: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._stack.append([])
        elif tag in ("td", "th") and self._stack:
            self._stack[-1].append("")
        elif tag == "br" and self._stack and self._stack[-1]:
            self._stack[-1][-1] += "\n"

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr" and self._stack:
            cells = [" ".join(c.replace("\xa0", " ").split()) if "\n" not in c else
                     "\n".join(" ".join(p.replace("\xa0", " ").split()) for p in c.split("\n")).strip()
                     for c in self._stack.pop()]
            if any(cells):
                self.rows.append(cells)

    def handle_data(self, data: str) -> None:
        if self._stack and self._stack[-1]:
            self._stack[-1][-1] += data


@dataclass(frozen=True)
class PageResults:
    season: str
    first_month: date  # the auction month (strip: the period's first month)
    posted: date
    columns: list[date]  # priced months (strip and spot: one column; `auctions` gives the delivery dates)
    values: dict[str, list[dict[str, Decimal]]]  # locality -> per column {'price','awarded'[,'excess']}


_POSTED = re.compile(r"Posted Date:\s*(\d{2}/\d{2}/\d{4})")
_SEASON = re.compile(r"(Summer \d{4}|Winter \d{4}-\d{4})")
_STARTING = re.compile(r"Auction Starting\s*(\d{2})/(\d{4})")
_SPOT_MONTH = re.compile(r"(\d{2})/(\d{4})\s*Spot Market Auction")
_MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
                                        "Nov", "Dec"), start=1)}


def _metric(label: str) -> str | None:
    key = label.lower().replace(" ", "")
    if key.startswith("price"):
        return "price"
    if key in ("awarded(mw)", "awardeddeficiency(mw)"):
        return "awarded"
    if key == "awardedexcess(mw)":
        return "excess"
    return None


def parse_page(data: bytes, kind: str) -> PageResults | None:
    """One strip, monthly or spot results page; None when NYISO has posted nothing for it yet."""
    html = data.decode("utf-8", errors="replace")
    if "No data matches your filter criteria" in html:
        return None
    parser = _Rows()
    parser.feed(html)
    rows = parser.rows
    title = next((r[0] for r in rows if r and "Posted Date" in r[0]), None)
    if title is None:
        raise ParseError(f"NYISO {kind} page has neither results nor 'No data matches' (page changed?)")
    posted, season = _POSTED.search(title), _SEASON.search(title)
    start = (_SPOT_MONTH if kind == "spot" else _STARTING).search(title)
    if not (posted and season and start):
        raise ParseError(f"NYISO {kind} page title not understood: {title!r}")
    first = date(int(start.group(2)), int(start.group(1)), 1)
    columns = [first]
    values: dict[str, list[dict[str, Decimal]]] = {}
    current: str | None = None
    for cells in rows:
        label = cells[0]
        if kind == "monthly" and label == "" and len(cells) > 1 and all(c in _MONTHS for c in cells[1:]):
            columns = _month_columns(first, cells[1:])
        elif len(cells) == 1:  # a section heading: a locality, an external area, or the spot page's month
            current = label
        elif label.lower().startswith(("total", "ros price", "*ros price")):
            current = None
        elif current in LOCALITIES and (metric := _metric(label)) is not None:
            cols = values.setdefault(current, [{} for _ in columns])
            raw = cells[1:]
            if len(raw) != len(columns):
                raise ParseError(f"NYISO {kind} {current} {label!r}: {len(raw)} values for {len(columns)} months")
            for i, text in enumerate(raw):
                if metric in cols[i]:
                    raise ParseError(f"NYISO {kind} page for {first:%m/%Y}: {current} {label!r} appears twice")
                cols[i][metric] = decimal(text, f"NYISO {kind} {current} {label}")
    missing = sorted(set(LOCALITIES) - set(values))
    if missing:
        raise ParseError(f"NYISO {kind} page for {first:%m/%Y}: localities {missing} not found")
    for loc, cols in values.items():
        for col in cols:
            if "price" not in col or "awarded" not in col:
                raise ParseError(f"NYISO {kind} page for {first:%m/%Y}: {loc} lacks a price or award")
    return PageResults(season.group(1), first, datetime.strptime(posted.group(1), "%m/%d/%Y").date(), columns,
                       values)


def _month_columns(first: date, names: list[str]) -> list[date]:
    out, day = [], first
    for name in names:
        if _MONTHS[name] != day.month:
            raise ParseError(f"NYISO monthly auction columns {names} do not run on from {first:%b %Y}")
        out.append(day)
        day = month_end(day.year, day.month) + timedelta(days=1)
    return out


def _auction(kind: str, season: Season, page: PageResults, month: date) -> tuple[str, str, str, date, date]:
    """(period key, auction name, auction title, delivery start, delivery end) of one priced column."""
    if kind == "strip":
        name = f"NYISO {season.description} Strip Auction"
        return f"{season.slug}-strip", name, name, season.start, season.end
    end = month_end(month.year, month.month)
    if kind == "monthly":
        title = f"NYISO Monthly Auction for {page.first_month:%b %Y}"
        return (f"{month:%Y-%m}-monthly-{page.first_month:%Y-%m}", f"{title} ({month:%b %Y} capacity)", title,
                month, end)
    name = f"NYISO {month:%b %Y} Spot Market Auction"
    return f"{month:%Y-%m}-spot", name, name, month, end


def auctions(page: PageResults, kind: str, season: Season, *, source_url: str, document_id: int | None,
             ) -> list[tuple[Auction, bool, str]]:
    """(Auction, is the headline column, auction title) per priced column of a page. Monthly pages hold one
    auction per delivery month; the headline is the auction month itself."""
    if page.season != season.description:
        raise ParseError(f"NYISO {kind} page is for {page.season!r}, asked for {season.description!r}")
    out = []
    for i, month in enumerate(page.columns):
        period, name, title, start, end = _auction(kind, season, page, month)
        auction = Auction(
            iso="nyiso", period=period, name=name, delivery_start=start, delivery_end=end, published=page.posted,
            published_loc="'Posted Date' in the results page heading", source_url=source_url,
            document_id=document_id,
        )
        for label, code in LOCALITIES.items():
            v = page.values[label][i]
            cleared = v["awarded"] + v.get("excess", Decimal(0))
            what = "Awarded Deficiency + Awarded Excess (MW)" if kind == "spot" else "Awarded (MW)"
            auction.zones.append(ZoneResult(code, {
                "clearing_price": Value(kw_month_to_mw_day(v["price"]), USD_MW_DAY, conversion_note(v["price"])),
                "clearing_price_native": Value(v["price"], USD_KW_MONTH, f"{label} Price ($/kW-Month), {month:%b %Y}"),
                "cleared_mw": Value(cleared, MW, f"{label} {what}, {month:%b %Y}"),
            }))
        out.append((auction, month == page.first_month, title))
    return out


# ---------------------------------------------------------------- load and run
def series_zone(load_zone: str) -> str | None:
    """A site's NYISO load zone (capacity_zone 'A'-'K') -> the locality slug its capacity is priced in."""
    zone = load_zone.strip().upper()
    if len(zone) != 1 or not "A" <= zone <= "K":
        return None
    return zone_slug(ZONE_LOCALITY.get(zone, "NYCA"))


def load_document(doc: DocumentRow, kind: str, season: Season, stats: RunStats, *, today: date) -> int:
    """Parse one stored page and write its auctions; announce the new headline to linked V1 sites."""
    page = parse_page(doc.read(), kind)
    if page is None:
        stats.extra["not_posted"] = stats.extra.get("not_posted", 0) + 1
        return 0
    found = auctions(page, kind, season, source_url=doc.url, document_id=doc.id)
    headlines: list[Headline] = []
    with db.transaction() as conn:
        writer = CapacityWriter(conn, stats, source_name=SOURCE_NAME, producer=PRODUCER)
        for auction, headline, title in found:
            if writer.write(auction) and headline:
                for zone in auction.zones:
                    native = zone.values["clearing_price_native"].value
                    price = zone.values["clearing_price"].value
                    headlines.append(Headline(
                        zone_slug(zone.zone), f"{title}: {zone.zone} ${native:,.2f}/kW-month (${price:,.2f}/MW-day)",
                        auction.published, doc.url, doc.id))
        announce(conn, stats, market="nyiso", source_name=SOURCE_NAME, headlines=headlines,
                 series_zone=series_zone, today=today, window_days=EVENT_WINDOW_DAYS)
    return len(found)


def settled(url: str, season: Season) -> bool:
    """Stored and final: its results are loaded (NYISO does not revise posted results), or it was fetched after
    the capability period ended (nothing more will be posted for it)."""
    doc = documents.latest_document(SOURCE_ID, url)
    if doc is None:
        return False
    if doc.fetched_at.date() > season.end + timedelta(days=7):
        return True
    with db.transaction() as conn:
        return document_loaded(conn, doc.id)


def run(*, today: date | None = None, first: date = FIRST_SEASON_START, fetcher: Fetcher | None = None,
        ) -> dict[str, Any]:
    today = today or today_et()

    def body(stats: RunStats) -> None:
        with db.transaction() as conn:  # zones first, so a new auction reaches the right V1 sites
            zones = capacity_zones.assign(conn, RunStats())
        stats.extra["v1_zones"] = {k: v for k, v in zones["v1_sites"].items() if v.startswith("nyiso")}
        seasons_doc = fetch_document(SOURCE_ID, SEASONS_URL, doc_type="nyiso_icap_seasons_json",
                                     title="NYISO ICAP capability periods", fetcher=fetcher)
        stats.document(seasons_doc)
        errors: list[Exception] = []
        stats.extra["skipped_settled"] = 0
        for season in seasons_due(parse_seasons(seasons_doc.read()), today, first):
            for kind, url, month in pages(season):
                if settled(url, season):
                    stats.extra["skipped_settled"] += 1
                    continue
                try:
                    doc = fetch_document(
                        SOURCE_ID, url, doc_type=f"nyiso_icap_{kind}_html",
                        title=f"NYISO ICAP {kind} auction {month:%m/%Y}" if month else
                        f"NYISO ICAP strip auction {season.description}",
                        doc_date=month or season.start, external_id=f"{season.id}:{kind}:{month or ''}",
                        fetcher=fetcher,
                    )
                    stats.document(doc)
                    load_document(doc, kind, season, stats, today=today)
                except (FetchError, ParseError) as exc:
                    errors.append(exc)
                    stats.extra.setdefault("errors", []).append(f"{url}: {type(exc).__name__}: {exc}"[:400])
        if errors:
            raise errors[0]

    return collect(SOURCE, body)


@app.periodic(cron=CRON_UTC, periodic_id="monthly_2nd_1117_utc")
@app.task(name="collect.nyiso_icap", queue="collect", queueing_lock="collect.nyiso_icap", lock="collect.nyiso_icap")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


@cli.action("collect", "nyiso_icap", help="NYISO ICAP strip, monthly and spot auction results by locality")
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run())
