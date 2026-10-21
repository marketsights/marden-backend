"""ICE BofA option-adjusted spread indexes from FRED -> series facts (tag R), for the tier curves.

Series (docs/methods/pricing-v0.1.md "Tier curves": HY, BB, single-B, CCC, BBB, IG):
BAMLH0A0HYM2, BAMLH0A1HYBB, BAMLH0A2HYB, BAMLH0A3HYC, BAMLC0A4CBBB, BAMLC0A0CM.

Access: the FRED API (`series/observations`, JSON) when FRED_API_KEY is set, else the public
`fredgraph.csv` download. The key is sent as a query parameter but never stored or logged.
Each series is one stable URL (full available history; FRED keeps 3 years of these series), so
a re-run stores nothing new until FRED publishes a new observation. Facts: subject_type 'series',
field 'value' (percent), one per observation date; plus `display_allowed` = false and the ICE
copyright notice on each series, since the data may not be shown or passed to anyone.

TERMS (checked 2026-10-21): every one of these series is labelled "Copyrighted: Pre-Approval
Required" on FRED, and FRED's legal page says such series "may only be used for non-commercial
educational or personal use. For any other use, you must obtain permission from the copyright
holder"; FRED allows internal commercial use only for Public Domain and Citation Required series.
Marden is a commercial product, so the source is declared blocked until ICE Data Indices grants
written permission (or Marden licenses the indices from ICE). While blocked, a run makes no
request, writes nothing and records the block on the source. Cadence: daily 18:00 ET.
`python -m marden collect fred`.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from marden import cli
from marden.collectors.rates_common import (
    ParseError,
    RunStats,
    SeriesWriter,
    check_pct,
    collect,
    due_et,
    number,
    print_stats,
)
from marden.core import db, http
from marden.core.config import get_settings
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, fetch_document
from marden.core.queue import app
from marden.core.sources import SourceSpec, declare_source

SOURCE_ID = "fred_ice_bofa"
PRODUCER = "collector:fred"
CSV_HOST = "fred.stlouisfed.org"
API_HOST = "api.stlouisfed.org"
RUN_AT_ET = (18, 0)
CRON_UTC = "0 22,23 * * *"  # 18:00 ET is 22:00 UTC (EDT) or 23:00 UTC (EST); due_et keeps the right one
TERMS_CHECKED = date(2026, 10, 21)  # the day the series pages and FRED legal terms were read
LEGAL_URL = "https://fred.stlouisfed.org/legal/"

ICE_NOTICE = (
    "Copyright, 2023, ICE Data Indices. Reproduction of this data in any form is prohibited except with the "
    "prior written permission of ICE Data Indices. The end of day Index values, Index returns, and Index "
    "statistics (“Top Level Data”) are being provided for your internal use only and you are not "
    "authorized or permitted to publish, distribute or otherwise furnish Top Level Data to any third-party "
    "without prior written approval of ICE Data."
)
TERMS_NOTE = (
    "Checked 2026-10-21. Series pages (e.g. https://fred.stlouisfed.org/series/BAMLH0A0HYM2): all six series "
    "are tagged \"Copyrighted: Pre-Approval Required\"; notes: \"" + ICE_NOTICE + "\" FRED legal "
    f"({LEGAL_URL}): \"Copyrighted: Pre-approval required ... these series may only be used for non-commercial "
    "educational or personal use. For any other use, you must obtain permission from the copyright holder and "
    "include proper attribution of ownership. Neither the Federal Reserve Bank of St. Louis nor FRED can provide "
    "permission(s) on behalf of copyright holders\"; \"IV. Commercial Use: Series with the following copyright "
    "labels—Public Domain: Citation requested and Copyrighted: Citation required—may be used for "
    "internal commercial uses\"; prohibited: \"(g) Use scripts, spiders, scrapers, bots, or any manual or "
    "automatic technology ... in any manner that is excessive, disruptive, or adversely impacts the stability, "
    "performance, or availability of the FRED Services\" (automated access itself is allowed; robots.txt "
    "\"Crawl-delay: 1\", /graph/fredgraph.csv not disallowed) and \"Use the FRED Services or FRED Content in "
    "connection with the development or training of any software program or system or machine learning\" "
    "(needs a legal read for model use). FRED API terms (https://fred.stlouisfed.org/docs/api/terms_of_use.html): "
    "\"Before using data series owned by third parties for anything other than your own personal use, you must "
    "contact the data owner to obtain permission.\" If permitted: model input only, never displayed or "
    "furnished to users (fact display_allowed = false); 1 request/second."
)
BLOCKED_REASON = (
    "ICE BofA OAS series are 'Copyrighted: Pre-Approval Required' on FRED: commercial use, including internal "
    "model inputs for Marden's priced output, needs ICE Data Indices' prior written permission (FRED allows "
    "internal commercial use only for Public Domain and Citation Required series). Unblock once ICE grants "
    "permission or the indices are licensed from ICE."
)

SOURCE = declare_source(
    SOURCE_ID, "ICE BofA indices via FRED", "api", "https://fred.stlouisfed.org", 24 * 60, TERMS_NOTE,
    blocked=True, blocked_reason=BLOCKED_REASON,
)
http.set_rate_limit(CSV_HOST, 1.0)
http.set_rate_limit(API_HOST, 1.0)


@dataclass(frozen=True)
class FredSeries:
    fred_id: str
    series_id: str  # Marden subject_id
    source_name: str
    tag: str = "R"  # a third party's index value
    display_allowed: bool | None = None  # written as a fact when not None
    notice: str | None = None  # copyright notice fact

    @property
    def page_url(self) -> str:
        return f"https://{CSV_HOST}/series/{self.fred_id}"


def _ice(fred_id: str, series_id: str) -> FredSeries:
    return FredSeries(fred_id, series_id, "ICE Data Indices, LLC via FRED", display_allowed=False, notice=ICE_NOTICE)


ICE_SERIES: tuple[FredSeries, ...] = (
    _ice("BAMLH0A0HYM2", "ice_bofa_us_hy_oas"),
    _ice("BAMLH0A1HYBB", "ice_bofa_us_bb_oas"),
    _ice("BAMLH0A2HYB", "ice_bofa_us_b_oas"),
    _ice("BAMLH0A3HYC", "ice_bofa_us_ccc_oas"),
    _ice("BAMLC0A4CBBB", "ice_bofa_us_bbb_oas"),
    _ice("BAMLC0A0CM", "ice_bofa_us_ig_oas"),
)


def csv_url(fred_id: str) -> str:
    return f"https://{CSV_HOST}/graph/fredgraph.csv?id={fred_id}"


def api_url(fred_id: str) -> str:
    """The API URL as stored (the api_key is added to the request only)."""
    return f"https://{API_HOST}/fred/series/observations?series_id={fred_id}&file_type=json"


# ---------------------------------------------------------------- parse
def parse_csv(data: bytes, fred_id: str) -> tuple[list[tuple[date, Decimal]], int]:
    """fredgraph.csv: header 'observation_date,<ID>' (older files: 'DATE,<ID>'). Returns (rows, blanks)."""
    reader = csv.reader(io.StringIO(data.decode("utf-8-sig")))
    header = [h.strip() for h in next(reader, [])]
    if len(header) != 2 or header[0] not in ("observation_date", "DATE") or header[1] != fred_id:
        raise ParseError(f"fredgraph.csv for {fred_id}: unexpected header {header!r}")
    rows: list[tuple[date, Decimal]] = []
    blanks = 0
    for line in reader:
        if not line or not line[0].strip():
            continue
        try:
            day = date.fromisoformat(line[0].strip())
        except ValueError:
            raise ParseError(f"fredgraph.csv for {fred_id}: bad date {line[0]!r}") from None
        value = number(line[1] if len(line) > 1 else "")
        if value is None:
            blanks += 1
            continue
        rows.append((day, value))
    return rows, blanks


def parse_api(data: bytes, fred_id: str) -> tuple[list[tuple[date, Decimal]], int]:
    """FRED API series/observations JSON ('.' is a missing value). Returns (rows, blanks)."""
    try:
        payload = json.loads(data)
    except ValueError as exc:
        raise ParseError(f"FRED API response for {fred_id} is not JSON: {exc}") from None
    if not isinstance(payload, dict) or "error_code" in payload:
        raise ParseError(f"FRED API error for {fred_id}: {str(payload)[:200]}")
    observations = payload.get("observations")
    if not isinstance(observations, list):
        raise ParseError(f"FRED API response for {fred_id} has no observations list")
    rows: list[tuple[date, Decimal]] = []
    blanks = 0
    for obs in observations:
        try:
            day = date.fromisoformat(obs["date"])
        except (KeyError, TypeError, ValueError):
            raise ParseError(f"FRED API observation without a valid date: {obs!r}"[:200]) from None
        value = number(obs.get("value"))
        if value is None:
            blanks += 1
            continue
        rows.append((day, value))
    return rows, blanks


# ---------------------------------------------------------------- write
def load_document(doc: DocumentRow, series: FredSeries, stats: RunStats) -> int:
    """Parse one stored series file and write its facts (and the series' terms facts)."""
    parse = parse_api if doc.doc_type == "fred_api_json" else parse_csv
    rows, blanks = parse(doc.read(), series.fred_id)
    with db.transaction() as conn:
        writer = SeriesWriter(conn, stats, tag=series.tag, source_name=series.source_name, producer=PRODUCER)
        if series.display_allowed is not None:
            writer.attribute(series.series_id, "display_allowed", series.display_allowed, valid_from=TERMS_CHECKED,
                             source_url=series.page_url)
        if series.notice:
            writer.attribute(series.series_id, "copyright_notice", series.notice, valid_from=TERMS_CHECKED,
                             source_url=series.page_url)
        for day, value in rows:
            writer.observation(series.series_id, day, check_pct(series.series_id, day, value),
                               source_url=doc.url, document_id=doc.id)
    stats.blank_cells += blanks
    return len(rows)


def fetch_series(source_id: str, series: FredSeries, fetcher: Fetcher | None) -> DocumentRow:
    key = get_settings().fred_api_key
    if key:
        return fetch_document(
            source_id, api_url(series.fred_id), doc_type="fred_api_json", title=f"FRED {series.fred_id}",
            external_id=series.fred_id, fetcher=fetcher, secret_params={"api_key": key},
        )
    return fetch_document(
        source_id, csv_url(series.fred_id), doc_type="fredgraph_csv", title=f"FRED {series.fred_id}",
        external_id=series.fred_id, fetcher=fetcher,
    )


def run(
    *, series: tuple[FredSeries, ...] = ICE_SERIES, source: SourceSpec = SOURCE, fetcher: Fetcher | None = None,
) -> dict[str, Any]:
    """One collection run of `series` from `source` (tests pass a public-domain series and source)."""

    def body(stats: RunStats) -> None:
        stats.extra["access"] = "api" if get_settings().fred_api_key else "fredgraph.csv"
        for s in series:
            doc = fetch_series(source.id, s, fetcher)
            stats.document(doc)
            load_document(doc, s, stats)

    return collect(source, body)


# ---------------------------------------------------------------- schedule and CLI
@app.periodic(cron=CRON_UTC, periodic_id="daily_1800_et")
@app.task(name="collect.fred", queue="collect", queueing_lock="collect.fred", lock="collect.fred")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    if not due_et(timestamp, *RUN_AT_ET):
        return {"skipped": "not 18:00 ET"}
    return run()


@cli.action("collect", "fred", help="ICE BofA OAS indexes via FRED (blocked pending ICE permission)")
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run())
