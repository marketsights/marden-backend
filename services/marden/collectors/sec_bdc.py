"""`sec_bdc`: BDC schedules of investments from the SEC BDC Data Sets (docs/methods/price-sources.md
§5 `sec_bdc_soi`) -> holdings, holding facts and fund marks for AI-infrastructure borrowers.

A run:
1. Stores the BDC Data Sets page and reads its zip list ({YYYY}_{MM}_bdc.zip monthly; 2025 and earlier
   as {YYYY}q{N}_bdc.zip).
2. Fetches every zip covering months from `from_month` on (default 2025-10, i.e. the 9/30/2025
   schedules onward) through fetch_document; conditional GETs make an unchanged file a 304. Older
   zips are not fetched (counted as outside the window; `--from 2022-10` backfills them). A file
   is parsed when its bytes are new or were never parsed (marker on the document), or with `--reparse`.
3. Parses soi.tsv (sec_bdc_parse) for the followed borrowers (sec_bdc_borrowers, collisions such as
   Vantage Specialty Chemicals excluded), then writes per line (sec_bdc_store): a `holding`
   (filing_type 10-Q/10-K, BDC entity by CIK, par, fair value, raw mark, % of net assets), facts on
   the holding (cost_usd, spread_bp, interest_rate_pct, maturity, unfunded_commitment_usd), and for
   lines linked to a tranche instrument (sec_bdc_link) a raw fund mark (F) plus, when the unfunded
   commitment is known, the adjusted mark (M, bdc-unfunded-adj@0.1). Unsure links stay unlinked.

Structured filed data publishes directly; nothing here needs an LLM. Reruns write nothing new.
Cadence (§5): monthly on the 9th, and daily Nov 1-20, Feb 15-Mar 10, May 1-20, Aug 1-20 (the data
set is also refreshed mid-month). `python -m marden collect sec_bdc [--from YYYY-MM] [--file NAME] [--reparse]`.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from marden import cli
from marden.collectors import sec_bdc_borrowers as bb
from marden.collectors import sec_bdc_link as lk
from marden.collectors import sec_bdc_store as store
from marden.collectors.sec_bdc_parse import PAGE_URL, DataSetFile, ParseError, SoiLine, parse_index_page, parse_zip
from marden.core import db, heartbeat, http, sources
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, FetchError
from marden.core.queue import app
from marden.core.sources import SourceError, declare_source

log = logging.getLogger(__name__)

SOURCE_ID = "sec_bdc"
PRODUCER = f"collector:{SOURCE_ID}"
SEC_RATE_PER_SECOND = 5.0  # price-sources.md §5: every SEC collector shares <= 5 req/s
FROM_MONTH = date(2025, 10, 1)
EASTERN = ZoneInfo("America/New_York")
WRITE_ATTEMPTS = 4  # other writers mark the same read models dirty: a deadlock rolls back one filing, retried
RETRYABLE = (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure)
CRON_UTC = "17 11 * * *"  # daily 11:17 UTC (07:17 EDT / 06:17 EST); `due_on` keeps the spec's days

TERMS_NOTE = (
    'SEC: "Information presented on sec.gov is considered public information and may be copied or further '
    "distributed by users of the web site without the SEC's permission. Please consider appropriate citation "
    'to the SEC as the source." (https://www.sec.gov/about/privacy-information, last reviewed Nov. 29, 2023). '
    'BDC Data Sets page (https://www.sec.gov/data-research/sec-markets-data/bdc-data-sets, read 2026-10-21): '
    '"The data has been automatically and directly taken from submissions created by the registrants and '
    'provided as filed with the Commission. The data sets will be updated monthly." Fair access: "Current max '
    'request rate: 10 requests/second." and "Please declare your user agent in request headers" '
    "(https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data); \"The SEC does not allow "
    '"unclassified" bots or automated tools to crawl the site." robots.txt (https://www.sec.gov/robots.txt, read '
    "2026-10-21) disallows /cgi-bin, /search/ and /node but not /files/ or /data-research/. Marden sends "
    "SEC_USER_AGENT, shares the 5 req/s SEC bucket and re-reads the zips with conditional GETs. Marks are "
    'cited "as filed by <BDC> on Form 10-Q/10-K": Level 3 board marks, not vendor prices.'
)

SOURCE = declare_source(
    SOURCE_ID, "SEC BDC Data Sets (schedules of investments)", "api",
    "https://www.sec.gov/data-research/sec-markets-data/bdc-data-sets", 24 * 60, TERMS_NOTE,
)
if http.bucket_for("www.sec.gov")[1].rate > SEC_RATE_PER_SECOND:  # never raise a lower shared SEC limit
    http.set_rate_limit("sec.gov", SEC_RATE_PER_SECOND)


class CollectError(RuntimeError):
    """Some files failed; the run is recorded as failed after the others were collected."""


@dataclass
class RunStats:
    from_month: date
    files_listed: int = 0
    files_outside_window: list[str] = field(default_factory=list)
    files_due: list[str] = field(default_factory=list)
    documents_new: int = 0
    files_parsed: list[str] = field(default_factory=list)
    files_unchanged: list[str] = field(default_factory=list)
    soi_rows: int = 0
    rows_other_period: int = 0
    rows_jv: int = 0
    rows_duplicate: int = 0
    excluded: Counter = field(default_factory=Counter)
    bad_values: Counter = field(default_factory=Counter)
    by_borrower: Counter = field(default_factory=Counter)
    writes: store.WriteStats = field(default_factory=store.WriteStats)
    errors: list[str] = field(default_factory=list)
    ok: bool = True
    error: str | None = None

    def meta(self) -> dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items() if k not in ("writes", "from_month")}
        out.update(from_month=self.from_month.isoformat(), excluded=dict(self.excluded),
                   bad_values=dict(self.bad_values), by_borrower=dict(self.by_borrower.most_common()),
                   files_outside_window=len(self.files_outside_window), **self.writes.meta())
        return out


def due_files(
    files: list[DataSetFile], from_month: date, names: list[str] | None
) -> tuple[list[DataSetFile], list[str]]:
    """(files to fetch, names left out): files whose last month is `from_month` or later, or `names`."""
    if names:
        wanted = set(names)
        return [f for f in files if f.name in wanted], [f.name for f in files if f.name not in wanted]
    due = [f for f in files if f.end >= from_month]
    return due, [f.name for f in files if f not in due]


def _collect(stats: RunStats, hb: heartbeat.RunHandle, fetcher: Fetcher, names: list[str] | None,
             reparse: bool) -> None:
    page = http.fetch_document(SOURCE_ID, PAGE_URL, doc_type="bdc_data_sets_page", title="SEC BDC Data Sets",
                               meta={"producer": PRODUCER}, fetcher=fetcher)
    stats.documents_new += int(page.was_new)
    files = parse_index_page(page.read().decode("utf-8", errors="replace"))
    stats.files_listed = len(files)
    due, stats.files_outside_window = due_files(files, stats.from_month, names)
    stats.files_due = [f.name for f in due]
    with db.transaction() as conn:
        wanted = {i for b in bb.BORROWERS for i in (*b.tranches, b.placeholder) if i}
        instruments = lk.load_instruments(conn, wanted)
    for f in due:
        try:
            doc = http.fetch_document(SOURCE_ID, f.url, doc_type="bdc_data_set_zip", title=f"BDC Data Sets {f.name}",
                                      doc_date=f.start, external_id=f.name, meta={"producer": PRODUCER},
                                      fetcher=fetcher)
            stats.documents_new += int(doc.was_new)
            if store.is_parsed(doc) and not reparse:
                stats.files_unchanged.append(f.name)
                continue
            parsed = parse_zip(doc.read())
        except (FetchError, ParseError) as exc:
            stats.errors.append(f"{f.name}: {exc}")
            continue
        for name in ("rows_other_period", "rows_jv", "rows_duplicate"):
            setattr(stats, name, getattr(stats, name) + getattr(parsed, name))
        stats.soi_rows += parsed.rows
        stats.excluded.update(parsed.excluded)
        stats.bad_values.update(parsed.bad_values)
        stats.by_borrower.update(line.borrower for line in parsed.lines)
        hb.seen(len(parsed.lines))
        before = stats.writes.holdings_new
        by_filing: dict[str, list[SoiLine]] = defaultdict(list)
        for line in parsed.lines:
            by_filing[line.adsh].append(line)
        for adsh in sorted(by_filing):
            _write_filing(by_filing[adsh], instruments, doc, stats.writes)
        with db.transaction() as conn:
            store.mark_parsed(conn, doc, {"lines": len(parsed.lines), "rows": parsed.rows})
        hb.new(stats.writes.holdings_new - before)
        stats.files_parsed.append(f.name)


def _write_filing(lines: list[SoiLine], instruments: dict[str, lk.InstrumentInfo], doc: DocumentRow,
                  totals: store.WriteStats) -> None:
    """One filing's lines in one short transaction; retried after a deadlock (the writes are idempotent)."""
    for attempt in range(1, WRITE_ATTEMPTS + 1):
        part = store.WriteStats()
        try:
            with db.transaction() as conn:
                store.write_lines(conn, lines, instruments, doc, part)
        except RETRYABLE:
            if attempt == WRITE_ATTEMPTS:
                raise
            time.sleep(0.2 * attempt)
            continue
        totals.merge(part)
        return


def run(*, from_month: date | None = None, files: list[str] | None = None, reparse: bool = False,
        fetcher: Fetcher | None = None) -> RunStats:
    """One collection run (module docstring). A failed fetch or unreadable file is recorded on the
    source and the run returns ok=False; nothing is written in its place."""
    stats = RunStats(from_month=from_month or FROM_MONTH)
    sources.upsert_spec(SOURCE)
    try:
        with heartbeat.run(SOURCE_ID) as hb:
            try:
                _collect(stats, hb, fetcher or http.default_fetcher(), files, reparse)
            finally:
                hb.meta.update(stats.meta())
            if stats.errors:
                raise CollectError(f"{len(stats.errors)} file(s) failed; first: {stats.errors[0]}")
    except (CollectError, FetchError, ParseError, SourceError, *RETRYABLE) as exc:
        stats.ok, stats.error = False, f"{type(exc).__name__}: {exc}"
        log.warning("sec_bdc run failed: %s", exc)
    return stats


# ---------------------------------------------------------------- schedule and CLI
def due_on(day: date) -> bool:
    """Monthly on the 9th; daily Nov 1-20, Feb 15-Mar 10, May 1-20, Aug 1-20 (price-sources.md §5)."""
    m, d = day.month, day.day
    return d == 9 or (m in (5, 8, 11) and d <= 20) or (m == 2 and d >= 15) or (m == 3 and d <= 10)


@app.periodic(cron=CRON_UTC, periodic_id="daily_check")
@app.task(name="collect.sec_bdc", queue="collect", queueing_lock="collect.sec_bdc", lock="collect.sec_bdc")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    if timestamp is not None:
        day = datetime.fromtimestamp(timestamp, UTC).astimezone(EASTERN).date()
        if not due_on(day):
            return {"skipped": f"{day.isoformat()} is not a BDC data set day"}
    return run().meta()


def _month(text: str) -> date:
    return date.fromisoformat(f"{text}-01" if len(text) == 7 else text).replace(day=1)


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--from", dest="from_month", type=_month, default=None,
                   help=f"first month to fetch, YYYY-MM (default {FROM_MONTH:%Y-%m})")
    p.add_argument("--file", action="append", dest="files", help="only this zip, e.g. 2026_08_bdc.zip (repeatable)")
    p.add_argument("--reparse", action="store_true", help="parse unchanged files again (after a borrower table change)")


@cli.action("collect", SOURCE_ID, help="SEC BDC Data Sets soi.tsv -> holdings and fund marks for AI-infra borrowers",
            configure=_args)
def collect_command(args: argparse.Namespace) -> int:
    stats = run(from_month=args.from_month, files=args.files, reparse=args.reparse)
    print(json.dumps({"ok": stats.ok, "error": stats.error, **stats.meta()}, indent=2, default=str))
    return 0 if stats.ok else 1
