"""U.S. Treasury daily par yield curve -> series facts `ust_par_<tenor>` (tag F, 'U.S. Treasury').

Treasury publishes one CSV per calendar year (docs/methods/price-sources.md section 5):
https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{YYYY}/all?type=daily_treasury_yield_curve&field_tdr_date_value={YYYY}&page&_format=csv

Each run fetches the current year (and the previous year in the first week of January) plus any
backfill year from 2025 whose file has not been loaded yet. Every file is stored raw first; one
fact per tenor and date (subject_type 'series', field 'value', percent). Blank cells are tenors
Treasury did not publish that day (the 1.5-month series starts 2025-02-18) and are skipped.
Cadence: daily 18:30 ET. `python -m marden collect ust_par_curve [--year YYYY ...]`.
"""

from __future__ import annotations

import argparse
import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime
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
    today_et,
    url_loaded,
)
from marden.core import db, http
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, fetch_document
from marden.core.queue import app
from marden.core.sources import declare_source

SOURCE_ID = "ust_par_curve"
SOURCE_NAME = "U.S. Treasury"
PRODUCER = f"collector:{SOURCE_ID}"
HOST = "home.treasury.gov"
BASE_URL = f"https://{HOST}/resource-center/data-chart-center/interest-rates"
BACKFILL_FROM_YEAR = 2025
JANUARY_GRACE_DAYS = 7  # keep re-reading last year's file while late revisions are possible
RUN_AT_ET = (18, 30)
CRON_UTC = "30 22,23 * * *"  # 18:30 ET is 22:30 UTC (EDT) or 23:30 UTC (EST); due_et keeps the right one

TERMS_NOTE = (
    "Checked 2026-10-21. robots.txt (https://home.treasury.gov/robots.txt): \"User-agent: * Allow: /\". "
    "Site policies (https://home.treasury.gov/subfooter/site-policies-and-notices) and the privacy and legal "
    "notices (https://home.treasury.gov/subfooter/privacy-policy) contain no copyright, reuse or "
    "automated-access restriction. The par yield curve is a work of the U.S. Government: \"Copyright "
    "protection under this title is not available for any work of the United States Government\" "
    "(17 U.S.C. 105). Use: model input and display, cite 'U.S. Treasury'. Access: the published CSV "
    "download, one file per year, 1 request/second, descriptive User-Agent with contact."
)

SOURCE = declare_source(SOURCE_ID, SOURCE_NAME, "feed", BASE_URL, 24 * 60, TERMS_NOTE)
http.set_rate_limit(HOST, 1.0)

# CSV column -> series suffix. Treasury labels the 6-week bill benchmark "1.5 Month".
TENORS: dict[str, str] = {
    "1 Mo": "1m", "1.5 Month": "6w", "2 Mo": "2m", "3 Mo": "3m", "4 Mo": "4m", "6 Mo": "6m",
    "1 Yr": "1y", "2 Yr": "2y", "3 Yr": "3y", "5 Yr": "5y", "7 Yr": "7y", "10 Yr": "10y",
    "20 Yr": "20y", "30 Yr": "30y",
}
# Series id -> maturity in years, for the models that read the curve.
TENOR_YEARS: dict[str, float] = {
    "ust_par_1m": 1 / 12, "ust_par_6w": 1.5 / 12, "ust_par_2m": 2 / 12, "ust_par_3m": 0.25,
    "ust_par_4m": 4 / 12, "ust_par_6m": 0.5, "ust_par_1y": 1.0, "ust_par_2y": 2.0, "ust_par_3y": 3.0,
    "ust_par_5y": 5.0, "ust_par_7y": 7.0, "ust_par_10y": 10.0, "ust_par_20y": 20.0, "ust_par_30y": 30.0,
}


def series_id(suffix: str) -> str:
    return f"ust_par_{suffix}"


def year_url(year: int) -> str:
    return (
        f"{BASE_URL}/daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
        f"&field_tdr_date_value={year}&page&_format=csv"
    )


# ---------------------------------------------------------------- parse
@dataclass
class ParsedCurve:
    rows: list[tuple[date, dict[str, Decimal]]] = field(default_factory=list)  # (date, series id -> pct)
    blank_cells: int = 0
    unknown_columns: list[str] = field(default_factory=list)  # not written: a new tenor needs an id first


def parse_csv(data: bytes) -> ParsedCurve:
    """Parse one year's par yield curve CSV (header 'Date', then one column per tenor)."""
    reader = csv.reader(io.StringIO(data.decode("utf-8-sig")))
    header = next(reader, None)
    if not header or header[0].strip() != "Date":
        raise ParseError(f"par curve CSV: expected a 'Date' header, got {header[:3] if header else header!r}")
    columns: list[str | None] = []
    out = ParsedCurve()
    for name in header[1:]:
        suffix = TENORS.get(name.strip())
        columns.append(series_id(suffix) if suffix else None)
        if suffix is None:
            out.unknown_columns.append(name.strip())
    if not any(columns):
        raise ParseError(f"par curve CSV: no known tenor columns in {header!r}")
    seen: set[date] = set()
    for line in reader:
        if not line or not line[0].strip():
            continue
        try:
            day = datetime.strptime(line[0].strip(), "%m/%d/%Y").date()
        except ValueError:
            raise ParseError(f"par curve CSV: bad date {line[0]!r}") from None
        if day in seen:
            raise ParseError(f"par curve CSV: {day.isoformat()} appears twice")
        seen.add(day)
        values: dict[str, Decimal] = {}
        for i, sid in enumerate(columns, start=1):
            if sid is None:
                continue
            value = number(line[i] if i < len(line) else "")  # a short row is blank at the end
            if value is None:
                out.blank_cells += 1
                continue
            values[sid] = check_pct(sid, day, value)
        out.rows.append((day, values))
    return out


# ---------------------------------------------------------------- write
def load_document(doc: DocumentRow, stats: RunStats) -> ParsedCurve:
    """Parse a stored year file (from the raw store) and write its facts in one transaction."""
    parsed = parse_csv(doc.read())
    with db.transaction() as conn:
        writer = SeriesWriter(conn, stats, tag="F", source_name=SOURCE_NAME, producer=PRODUCER)
        for day, values in parsed.rows:
            for sid, value in values.items():
                writer.observation(sid, day, value, source_url=doc.url, document_id=doc.id)
    stats.blank_cells += parsed.blank_cells
    if parsed.unknown_columns:
        unknown = stats.extra.setdefault("unknown_columns", [])
        unknown.extend(c for c in parsed.unknown_columns if c not in unknown)
    return parsed


def years_due(today: date) -> list[int]:
    """The current year, last year in early January, and every backfill year not loaded yet."""
    years = []
    for year in range(BACKFILL_FROM_YEAR, today.year + 1):
        live = year == today.year or (year == today.year - 1 and today.timetuple().tm_yday <= JANUARY_GRACE_DAYS)
        if live or not url_loaded(SOURCE_ID, year_url(year)):
            years.append(year)
    return years


def run(*, years: list[int] | None = None, today: date | None = None, fetcher: Fetcher | None = None) -> dict[str, Any]:
    """One collection run (see module docstring). Returns the run stats."""
    today = today or today_et()

    def body(stats: RunStats) -> None:
        due = sorted(set(years)) if years else years_due(today)
        stats.extra["years"] = due
        if not years:  # backfill files already loaded are not fetched again
            stats.extra["skipped_loaded_years"] = [y for y in range(BACKFILL_FROM_YEAR, today.year + 1) if y not in due]
        for year in due:
            doc = fetch_document(
                SOURCE_ID, year_url(year), doc_type="par_yield_curve_csv",
                title=f"Daily Treasury Par Yield Curve Rates {year}", external_id=str(year),
                meta={"year": year}, fetcher=fetcher,
            )
            stats.document(doc)
            load_document(doc, stats)

    return collect(SOURCE, body)


# ---------------------------------------------------------------- schedule and CLI
@app.periodic(cron=CRON_UTC, periodic_id="daily_1830_et")
@app.task(name="collect.ust_par_curve", queue="collect", queueing_lock="collect.ust_par_curve",
          lock="collect.ust_par_curve")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    if not due_et(timestamp, *RUN_AT_ET):
        return {"skipped": "not 18:30 ET"}
    return run()


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--year", type=int, action="append", dest="years",
                   help="fetch this calendar year's file (repeatable; default: current year + backfill)")


@cli.action("collect", SOURCE_ID, help="Treasury daily par yield curve -> series ust_par_<tenor>", configure=_args)
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run(years=args.years))
