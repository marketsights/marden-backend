"""ERCOT day-ahead and real-time settlement point prices at load zones and hubs -> `series` facts (tag F, ERCOT),
and the ERCOT load zone of each Texas site, campus and plant (ercot_load_zones). Parsers: ercot_prices_parse.

Products (ERCOT Public API, https://api.ercot.com/api/public-reports; access, sign-in and rate limit in ercot_api):
- NP4-190-CD DAM Settlement Point Prices (`/np4-190-cd/dam_stlmnt_pnt_prices`): hourly prices for the next
  operating day, posted after the day-ahead market clears (~13:30 Central). The query takes one settlement
  point at a time, so each load zone and hub is asked for separately.
- NP6-905-CD Settlement Point Prices at Resource Nodes, Hubs and Load Zones (`/np6-905-cd/spp_node_zone_hub`):
  15-minute real-time prices, queried by settlement point type: LZ (load zones), HU (hubs), SH (HB_BUSAVG) and
  AH (HB_HUBAVG). The energy-weighted load zone rows (LZEW) and the DC-tie load zones (LZ_DC*) are not kept.
Points: the eight load zones LZ_AEN, LZ_CPS, LZ_HOUSTON, LZ_LCRA, LZ_NORTH, LZ_RAYBN, LZ_SOUTH, LZ_WEST and the
seven hubs HB_BUSAVG, HB_HOUSTON, HB_HUBAVG, HB_NORTH, HB_PAN, HB_SOUTH, HB_WEST.

What is written, per point and operating day (valid_from = the delivery date), on subject
`power_price:ercot:<point lower case>:dam|rt` (e.g. `power_price:ercot:lz_west:rt`), unit USD/MWh, tag F,
source_name 'ERCOT', source_url the product URL, document_id the stored API answer: `avg`, `on_peak_avg`
(hours ending 07-22, 7x16), `max`, plus `hourly` for DAM and `intervals_over_1000` (unit 'intervals') for RT;
only complete days (see ercot_prices_parse), so a gap stays a gap. Fields are not named `value`: these are
not rate-curve observations.

Requests. Each run computes, per DAM point and RT type, the days from BACKFILL_DAYS ago through the latest
operating day (DAM: tomorrow; RT: yesterday, Central) that have no `avg` fact yet, and asks for them in windows
of at most one calendar month (PAGE_SIZE rows a page; further pages are followed). Every answer is stored raw
(`http.fetch_document`); a window that ended more than SETTLED_DAYS ago and was stored before is parsed from
storage and never asked for again (ERCOT's API terms: store reports rather than downloading them repeatedly).
The first run backfills two years (about 475 requests at 0.4 a second, ~20 minutes); later runs ask only for
new days (about 19 requests). MAX_REQUESTS caps one run; the rest follows on the next.

Cadence: daily at 14:07 Central (DAM for the next day) and 00:47 Central (real time for the day just ended),
each scheduled at both UTC hours with `due_et`. `python -m marden collect ercot_prices`.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from marden import cli
from marden.collectors import ercot_api, ercot_load_zones
from marden.collectors import ercot_prices_parse as parse
from marden.collectors.rates_common import ParseError, RunStats, collect, due_et, print_stats
from marden.core import checks, db, documents
from marden.core.documents import DocumentRow
from marden.core.facts import FactWriter
from marden.core.http import Fetcher, FetchError, default_fetcher
from marden.core.queue import app
from marden.core.sources import SourceBlocked, declare_source

SOURCE_ID = "ercot_prices"
SOURCE_NAME = "ERCOT"
PRODUCER = f"collector:{SOURCE_ID}"
DAM_PRODUCT_URL = f"{ercot_api.API_BASE}/np4-190-cd"
DAM_URL = f"{DAM_PRODUCT_URL}/dam_stlmnt_pnt_prices"
RT_PRODUCT_URL = f"{ercot_api.API_BASE}/np6-905-cd"
RT_URL = f"{RT_PRODUCT_URL}/spp_node_zone_hub"
LOAD_ZONES = ("LZ_AEN", "LZ_CPS", "LZ_HOUSTON", "LZ_LCRA", "LZ_NORTH", "LZ_RAYBN", "LZ_SOUTH", "LZ_WEST")
TRADING_HUBS = ("HB_HOUSTON", "HB_NORTH", "HB_PAN", "HB_SOUTH", "HB_WEST")
HUBS = ("HB_BUSAVG", *TRADING_HUBS, "HB_HUBAVG")
RT_TYPES = {"LZ": LOAD_ZONES, "HU": TRADING_HUBS, "SH": ("HB_BUSAVG",), "AH": ("HB_HUBAVG",)}
UNIT = "USD/MWh"
BACKFILL_DAYS = 730
SETTLED_DAYS = 7
PAGE_SIZE = 50_000
MAX_REQUESTS = 800
DAM_RUN_ET = (15, 7)  # 14:07 Central, after the DAM posts (~13:30 Central)
RT_RUN_ET = (1, 47)  # 00:47 Central, the operating day just ended
CRON_DAM_UTC = "7 19,20 * * *"
CRON_RT_UTC = "47 5,6 * * *"
TERMS_NOTE = (
    "Checked 2026-10-21. Prices come from the ERCOT Public API (api.ercot.com), products NP4-190-CD and "
    "NP6-905-CD, with a free account registered by a person: Terms of Use for the ERCOT API "
    "(https://www.ercot.com/help/terms/data-portal): \"Accounts registered by \"bots\" or other automated methods "
    "are not permitted\"; \"You may not download the same report from the Data Access Portal more than three "
    "times within a 12-month period. Reports should be downloaded and stored on the users' own hardware\": every "
    "answer is stored and a settled window is never asked for again. 30 requests a minute (developer.ercot.com, "
    "Known Limitations): 0.4 a second here, backing off on 429. ERCOT Terms of Use "
    "(https://www.ercot.com/help/terms): \"raw data provided in public portions of this website may be used, "
    "reproduced, and redistributed in compilations, charts, and analyses without maintaining such notices.\" "
    "The Load Zone Map (www.ercot.com/files/assets/...) is under /files, which robots.txt allows; it is "
    "fetched with a conditional GET."
)
SOURCE = declare_source(SOURCE_ID, SOURCE_NAME, "api", f"https://{ercot_api.API_HOST}", 24 * 60, TERMS_NOTE)


def series_id(point: str, market: str) -> str:
    return f"power_price:ercot:{point.lower()}:{market}"


@dataclass(frozen=True)
class Stream:
    """One kind of request: DAM for one settlement point, or RT for one settlement point type."""

    market: str  # 'dam' | 'rt'
    key: str  # DAM: the settlement point; RT: the settlement point type
    points: tuple[str, ...]

    @property
    def url(self) -> str:
        return DAM_URL if self.market == "dam" else RT_URL

    @property
    def product_url(self) -> str:
        return DAM_PRODUCT_URL if self.market == "dam" else RT_PRODUCT_URL

    def query(self, start: date, end: date, page: int) -> str:
        params: dict[str, Any] = {"deliveryDateFrom": start.isoformat(), "deliveryDateTo": end.isoformat()}
        params["settlementPoint" if self.market == "dam" else "settlementPointType"] = self.key
        params["size"] = PAGE_SIZE
        if page > 1:
            params["page"] = page
        return f"{self.url}?{urlencode(params)}"


STREAMS = (*(Stream("dam", p, (p,)) for p in LOAD_ZONES + HUBS),
           *(Stream("rt", t, points) for t, points in RT_TYPES.items()))


def today_ct() -> date:
    return datetime.now(parse.CT).date()


def windows(days: list[date]) -> Iterator[tuple[date, date]]:
    """Runs of consecutive days, cut at month ends (one request covers at most a calendar month)."""
    run: list[date] = []
    for d in sorted(days):
        if run and (d != run[-1] + timedelta(days=1) or d.month != run[-1].month):
            yield run[0], run[-1]
            run = []
        run.append(d)
    if run:
        yield run[0], run[-1]


def written_days(since: date) -> dict[str, set[date]]:
    with db.transaction() as conn:
        rows = conn.execute(
            """SELECT subject_id, valid_from FROM fact WHERE subject_type = 'series' AND subject_id LIKE %s
                 AND field = 'avg' AND producer = %s AND valid_from >= %s""",
            ("power_price:ercot:%", PRODUCER, since)).fetchall()
    out: dict[str, set[date]] = {}
    for r in rows:
        out.setdefault(r["subject_id"], set()).add(r["valid_from"])
    return out


class Run:
    def __init__(self, stats: RunStats, fetcher: Fetcher, today: date):
        self.stats, self.fetcher, self.today = stats, fetcher, today
        self.session: ercot_api.Session | None = None
        self.info: dict[str, Any] = {"requests": 0, "stored_reused": 0, "windows": 0, "days_written": {},
                                     "incomplete": [], "incomplete_count": 0, "capped": False}
        stats.extra["ercot_prices"] = self.info

    def missing(self, stream: Stream, have: dict[str, set[date]]) -> list[date]:
        end = self.today + timedelta(days=1) if stream.market == "dam" else self.today - timedelta(days=1)
        end = min(end, checks.latest_today())
        start = self.today - timedelta(days=BACKFILL_DAYS)
        span = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        return [d for d in span if any(d not in have.get(series_id(p, stream.market), ()) for p in stream.points)]

    def document(self, stream: Stream, start: date, end: date, page: int) -> DocumentRow | None:
        url = stream.query(start, end, page)
        if end < self.today - timedelta(days=SETTLED_DAYS):
            stored = documents.latest_document(SOURCE_ID, url)
            if stored is not None:
                self.info["stored_reused"] += 1
                return stored
        if self.info["requests"] >= MAX_REQUESTS:
            self.info["capped"] = True
            return None
        if self.session is None:
            self.session = ercot_api.Session(self.fetcher, first_url=url)
        self.info["requests"] += 1
        return self.session.document(SOURCE_ID, url, doc_type=f"{stream.market}_spp_json",
                                     title=f"ERCOT {stream.market.upper()} prices {stream.key} {start}..{end}")

    def load_window(self, stream: Stream, start: date, end: date) -> bool:
        """Fetch (or reread) every page of one window and write its complete days. False when capped."""
        rows: list[parse.Price] = []
        doc_of: dict[tuple[str, date], int] = {}
        page, pages = 1, 1
        while page <= pages:
            doc = self.document(stream, start, end, page)
            if doc is None:
                return False
            self.stats.document(doc)
            got = parse.dam_page(doc.read()) if stream.market == "dam" else parse.rt_page(
                doc.read(), frozenset({stream.key}))
            pages = max(got.total_pages, 1)
            for p in got.rows:
                if p.point in stream.points and start <= p.day <= end:
                    rows.append(p)
                    doc_of.setdefault((p.point, p.day), doc.id)
            page += 1
        self.info["windows"] += 1
        days, incomplete = parse.daily(rows, per_hour=1 if stream.market == "dam" else 4)
        self.info["incomplete_count"] += len(incomplete)
        self.info["incomplete"] = (self.info["incomplete"] + incomplete)[:20]
        with db.transaction() as conn:
            writer = FactWriter(conn)
            for s in days:
                self.write(writer, stream, s, doc_of[(s.point, s.day)])
        return True

    def write(self, writer: FactWriter, stream: Stream, s: parse.DayStats, document_id: int) -> None:
        sid = series_id(s.point, stream.market)
        values: dict[str, tuple[Any, str]] = {"avg": (s.avg, UNIT), "on_peak_avg": (s.on_peak_avg, UNIT),
                                              "max": (s.max, UNIT)}
        if s.hourly is not None:
            values["hourly"] = (s.hourly, UNIT)
        if s.intervals_over_1000 is not None:
            values["intervals_over_1000"] = (s.intervals_over_1000, "intervals")
        loc = (f"{'NP4-190-CD' if stream.market == 'dam' else 'NP6-905-CD'} settlementPoint {s.point}, "
               f"deliveryDate {s.day.isoformat()}: {s.periods} {'hourly' if stream.market == 'dam' else '15-minute'}"
               " prices")
        for name, (value, unit) in values.items():
            res = writer.add("series", sid, name, value, tag="F", source_name=SOURCE_NAME,
                             source_url=stream.product_url, valid_from=s.day, producer=PRODUCER,
                             status="published", unit=unit, document_id=document_id, quote_loc=loc)
            self.stats.facts_seen += 1
            if res.created:
                self.stats.facts_new += 1
                self.stats.new_by_series[sid] = self.stats.new_by_series.get(sid, 0) + 1
        written = self.info["days_written"]
        written[stream.market] = written.get(stream.market, 0) + 1
        newest = self.stats.latest.get(sid)
        if newest is None or newest[0] < s.day.isoformat():
            self.stats.latest[sid] = [s.day.isoformat(), float(s.avg)]

    def prices(self) -> None:
        have = written_days(self.today - timedelta(days=BACKFILL_DAYS))
        for stream in STREAMS:
            for start, end in windows(self.missing(stream, have)):
                if not self.load_window(stream, start, end):
                    return


def collect_all(stats: RunStats, fetcher: Fetcher, today: date | None = None) -> None:
    """Load zones first (one map request, no key needed), then prices; both are tried, the first error fails
    the run after the other step ran."""
    errors: list[Exception] = []
    for name, step in (("load_zones", lambda: ercot_load_zones.assign(stats, SOURCE_ID, fetcher)),
                       ("prices", lambda: Run(stats, fetcher, today or today_ct()).prices())):
        try:
            step()
        except (FetchError, ParseError, SourceBlocked) as exc:
            stats.extra.setdefault("errors", {})[name] = f"{type(exc).__name__}: {exc}"
            errors.append(exc)
    if errors:
        raise errors[0]


def run(*, fetcher: Fetcher | None = None, today: date | None = None) -> dict[str, Any]:
    return collect(SOURCE, lambda stats: collect_all(stats, fetcher or default_fetcher(), today))


@app.periodic(cron=CRON_DAM_UTC, periodic_id="daily_dam_1407_ct")
@app.periodic(cron=CRON_RT_UTC, periodic_id="daily_rt_0047_ct")
@app.task(name="collect.ercot_prices", queue="collect", queueing_lock="collect.ercot_prices",
          lock="collect.ercot_prices")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    if not (due_et(timestamp, *DAM_RUN_ET) or due_et(timestamp, *RT_RUN_ET)):
        return {"skipped": "not 14:07 or 00:47 Central"}
    return run()


@cli.action("collect", "ercot_prices",
            help="ERCOT DAM and real-time prices at load zones and hubs, and site load zones (needs ERCOT API key)")
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run())
