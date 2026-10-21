"""NY Fed Secured Overnight Financing Rate -> series fact `sofr` (tag F), with the NY Fed notice.

The NY Fed Markets API (docs/methods/price-sources.md section 5) is read one calendar year per
request, so each file's URL is stable and a past year's bytes never change:
https://markets.newyorkfed.org/api/rates/secured/sofr/search.json?startDate={YYYY}-01-01&endDate={YYYY}-12-31

Each run fetches the current year (and the previous year in the first week of January) plus any
backfill year (two calendar years back) not loaded yet. One fact per effective date
(subject_type 'series', subject_id 'sofr', field 'value', percent); a revised rate is a new fact
for the same date (fact_current shows the latest learned). The reference-rate notice the NY Fed
Terms of Use require with any presentation of SOFR data is stored on the source's terms_note and
as fact `notice` on series `sofr`. Cadence: daily 08:30 ET. `python -m marden collect nyfed_sofr`.
"""

from __future__ import annotations

import argparse
import json
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
    print_stats,
    today_et,
    url_loaded,
)
from marden.core import db, http
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, fetch_document
from marden.core.queue import app
from marden.core.sources import declare_source

SOURCE_ID = "nyfed_sofr"
SOURCE_NAME = "Federal Reserve Bank of New York"
PRODUCER = f"collector:{SOURCE_ID}"
HOST = "markets.newyorkfed.org"
BASE_URL = f"https://{HOST}/api/rates/secured/sofr"
SERIES_ID = "sofr"
BACKFILL_YEARS = 2
JANUARY_GRACE_DAYS = 7
RUN_AT_ET = (8, 30)
CRON_UTC = "30 12,13 * * *"  # 08:30 ET is 12:30 UTC (EDT) or 13:30 UTC (EST); due_et keeps the right one

TERMS_URL = "https://www.newyorkfed.org/privacy/termsofuse"
TERMS_UPDATED = date(2023, 6, 9)  # "Terms of Use Last Updated: 6/9/2023"
NOTICE_QUOTE = (
    "If you use or distribute reference rate data or related information posted to the website, you must "
    "include the following notice and disclaimer with your presentation of that data or information: "
    "“The [NAME OF DATA or CONTENT]* is subject to the Terms of Use posted at newyorkfed.org. The New York "
    "Fed is not responsible for publication of the [DATA NAME] by [NAME OF PUBLISHER], does not [sanction] or "
    "[endorse] any particular republication, and has no liability for your use.” ... The Secured Overnight "
    "Financing Rate (SOFR) Data and Broad General Collateral Rate (BGCR) Data are calculated using data provided "
    "under a license granted to the New York Fed by DTCC Solutions LLC (“Solutions”), an affiliate of The "
    "Depository Trust & Clearing Corporation. Solutions, its affiliates, and third parties from which they "
    "obtained data have no liability for the content of this material."
)
NOTICE = (
    "The Secured Overnight Financing Rate (SOFR) data is subject to the Terms of Use posted at newyorkfed.org. "
    "The New York Fed is not responsible for publication of the SOFR data by Marden, does not sanction or "
    "endorse any particular republication, and has no liability for your use. The Secured Overnight Financing "
    "Rate (SOFR) Data and Broad General Collateral Rate (BGCR) Data are calculated using data provided under a "
    "license granted to the New York Fed by DTCC Solutions LLC (“Solutions”), an affiliate of The "
    "Depository Trust & Clearing Corporation. Solutions, its affiliates, and third parties from which they "
    "obtained data have no liability for the content of this material."
)
TERMS_NOTE = (
    f"Checked 2026-10-21 ({TERMS_URL}, Last Updated 6/9/2023). Licence: \"The New York Fed grants you a "
    "non-exclusive license, subject to the Terms, to use, copy, and distribute Content for your personal or "
    "business purposes. You may: Access the Content, manually or through an automated process or device, "
    "provided your access does not have the effect of disabling, damaging, or interfering with the function of "
    "the Website\". The API is covered: \"The New York Fed also makes some Content available through other "
    "electronic means, such as an RSS feed, an API ... These Terms generally apply to Content accessed through "
    "those other distribution channels as well.\" Reference rates (Use Restrictions): every presentation of "
    f"SOFR data must carry this notice (also stored as fact series/sofr/notice): {NOTICE} The New York Fed "
    "may not be named as endorsing Marden; if Marden names SOFR in describing a product or service it offers, "
    "it must add: \"Marden is not affiliated with the New York Fed. The New York Fed does not sanction, endorse, "
    "or recommend any products or services offered by Marden.\" robots.txt: markets.newyorkfed.org has none (404); "
    "www.newyorkfed.org/robots.txt disallows only /PrincipleInterest/ and two calendar PDFs. "
    "Access: one request per calendar year file, 1 request/second. Term SOFR and SOFR futures/forwards are "
    "CME products and are not covered."
)

SOURCE = declare_source(SOURCE_ID, SOURCE_NAME, "api", BASE_URL, 24 * 60, TERMS_NOTE)
http.set_rate_limit(HOST, 1.0)


def year_url(year: int) -> str:
    return f"{BASE_URL}/search.json?startDate={year}-01-01&endDate={year}-12-31"


# ---------------------------------------------------------------- parse
def parse_json(data: bytes) -> list[tuple[date, Decimal, dict[str, Any]]]:
    """(effective date, percent rate, other published fields) for every SOFR record in a response."""
    try:
        payload = json.loads(data, parse_float=Decimal)
    except ValueError as exc:
        raise ParseError(f"SOFR response is not JSON: {exc}") from None
    rates = payload.get("refRates") if isinstance(payload, dict) else None
    if not isinstance(rates, list):
        raise ParseError("SOFR response has no refRates list")
    out: list[tuple[date, Decimal, dict[str, Any]]] = []
    seen: set[date] = set()
    for rec in rates:
        if not isinstance(rec, dict) or rec.get("type") != "SOFR":
            raise ParseError(f"unexpected record in SOFR response: {rec!r}"[:200])
        try:
            day = date.fromisoformat(rec["effectiveDate"])
        except (KeyError, TypeError, ValueError):
            raise ParseError(f"SOFR record without a valid effectiveDate: {rec!r}"[:200]) from None
        if day in seen:
            raise ParseError(f"SOFR response lists {day.isoformat()} twice")
        seen.add(day)
        rate = rec.get("percentRate")
        if rate is None:
            continue  # not published for this date; nothing to write
        if isinstance(rate, bool) or not isinstance(rate, Decimal | int):
            raise ParseError(f"SOFR {day.isoformat()}: percentRate is not a number: {rate!r}")
        extra = {k: v for k, v in rec.items() if k not in ("effectiveDate", "type", "percentRate")}
        out.append((day, check_pct(SERIES_ID, day, Decimal(rate)), extra))
    return out


# ---------------------------------------------------------------- write
def write_notice(writer: SeriesWriter) -> bool:
    return writer.attribute(
        SERIES_ID, "notice", NOTICE, valid_from=TERMS_UPDATED, source_url=TERMS_URL,
        quote=NOTICE_QUOTE, quote_loc="Terms of Use > Use Restrictions > Reference Rates",
    )


def load_document(doc: DocumentRow, stats: RunStats) -> int:
    """Parse a stored year file and write its SOFR facts (and the notice) in one transaction."""
    records = parse_json(doc.read())
    with db.transaction() as conn:
        writer = SeriesWriter(conn, stats, tag="F", source_name=SOURCE_NAME, producer=PRODUCER)
        write_notice(writer)
        for day, rate, extra in records:
            writer.observation(SERIES_ID, day, rate, source_url=doc.url, document_id=doc.id)
            if extra.get("revisionIndicator"):
                stats.extra.setdefault("revised_dates", []).append(day.isoformat())
    stats.extra.setdefault("records", {})[doc.external_id or doc.url] = len(records)
    return len(records)


def years_due(today: date) -> list[int]:
    years = []
    for year in range(today.year - BACKFILL_YEARS, today.year + 1):
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
        if not years:
            stats.extra["skipped_loaded_years"] = [
                y for y in range(today.year - BACKFILL_YEARS, today.year + 1) if y not in due
            ]
        for year in due:
            doc = fetch_document(
                SOURCE_ID, year_url(year), doc_type="sofr_json", title=f"SOFR {year} (NY Fed Markets API)",
                external_id=str(year), meta={"year": year}, fetcher=fetcher,
            )
            stats.document(doc)
            load_document(doc, stats)

    return collect(SOURCE, body)


# ---------------------------------------------------------------- schedule and CLI
@app.periodic(cron=CRON_UTC, periodic_id="daily_0830_et")
@app.task(name="collect.nyfed_sofr", queue="collect", queueing_lock="collect.nyfed_sofr", lock="collect.nyfed_sofr")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    if not due_et(timestamp, *RUN_AT_ET):
        return {"skipped": "not 08:30 ET"}
    return run()


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--year", type=int, action="append", dest="years",
                   help="fetch this calendar year (repeatable; default: current year + 2-year backfill)")


@cli.action("collect", SOURCE_ID, help="NY Fed SOFR -> series sofr (with the NY Fed notice)", configure=_args)
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run(years=args.years))
