"""`sec_nport`: fund holdings and marks from SEC Form N-PORT (docs/methods/price-sources.md §4-5,
`sec_nport_marks`).

Discovery:
1. Daily form index `form.YYYYMMDD.idx`: every NPORT-P and NPORT-P/A filed on each business day of
   the window (default the last 10 days up to yesterday; today's index is built at night).
2. Backfill: the DERA quarterly Form N-PORT data set zip (`{YYYY}q{n}_nport.zip`, published around
   the 7th-10th after quarter end). Its FUND_REPORTED_HOLDING and IDENTIFIERS tables name the
   accessions with a position in a watched identifier, optionally limited to report periods.

For each filing, `primary_doc.xml` is fetched and its identifiers (CUSIP, ISIN, other ids such as
LoanX) are checked against the watched set (instrument CUSIPs/ISINs and published `identifiers`
facts). Only matching filings are stored raw (document, doc_type = form, external_id = accession)
and parsed; the others are counted in the run meta and dropped. Structured filed data publishes
directly (tag F): holdings with their attribute facts (units, cur_cd, exchange_rt,
fair_value_level, asset_cat, public_date = filing date), one `fund_mark` price per position (units
PA only; valid as of the report period), the fund's `net_assets_usd`, and per instrument and
period the cross-fund median price ('N-PORT median of N funds (<period>)') with its min/max in
fact `fund_mark_dispersion` (sec_nport_store, sec_nport_marks).

A daily index or data set whose filings were all checked gets extract_state 'done' and is not
re-read (the data set only after a pass without a period filter); a fetch failure leaves it open
for the next run. Only /Archives/edgar/ and /files/dera/ are read, never /cgi-bin or /search/;
all SEC hosts share one 5 req/s bucket with the declared SEC_USER_AGENT. Cadence: daily 07:00 ET.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from marden import cli
from marden.collectors import sec_nport_store as store
from marden.collectors.rates_common import due_et, today_et
from marden.collectors.sec_index_parse import daily_index_url, parse_form_index
from marden.collectors.sec_nport_dataset import dataset_candidates
from marden.collectors.sec_nport_marks import write_medians
from marden.collectors.sec_nport_parse import (
    NportParseError,
    dataset_label,
    dataset_url,
    nport_lines,
    parse_label,
    parse_nport,
    prescan,
    previous_quarter,
    primary_doc_url,
    quarter_end,
)
from marden.core import db, documents, heartbeat, http, sources
from marden.core.checks import ValidationError
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, FetchError
from marden.core.queue import app
from marden.core.sources import SourceError, declare_source

log = logging.getLogger(__name__)

SOURCE_ID = "sec_nport"
PRODUCER = f"collector:{SOURCE_ID}"
SEC_RATE_PER_SECOND = 5.0  # price-sources.md §5: all SEC collectors share <= 5 req/s
LOOKBACK_DAYS = 10
DATASET_FROM_DAY = 7  # the quarterly zip appears around the 7th-10th after quarter end
MEDIAN_WINDOW_DAYS = 400  # report periods whose medians are recomputed each run
RUN_AT_ET = (7, 0)
CRON_UTC = "0 11,12 * * *"  # 07:00 ET is 11:00 UTC (EDT) or 12:00 UTC (EST); due_et keeps the right one
INDEX_DOC_TYPE = "edgar_daily_index"
DATASET_DOC_TYPE = "nport_dataset_zip"
MAX_LISTED = 100

TERMS_NOTE = (
    'Checked 2026-10-21. SEC: "Information presented on sec.gov is considered public information and may be '
    "copied or further distributed by users of the web site without the SEC's permission. Please consider "
    'appropriate citation to the SEC as the source." and "Current guidelines limit users to a total of no more '
    'than 10 requests per second, regardless of the number of machines used to submit requests." '
    '(https://www.sec.gov/about/privacy-information, last reviewed Nov 29, 2023); "Please declare your user '
    'agent in request headers" (https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data). '
    "robots.txt (https://www.sec.gov/robots.txt) allows /Archives/edgar/data and disallows /cgi-bin and /search/; "
    "/Archives/edgar/daily-index/ and /files/dera/ are not disallowed. The Form N-PORT data sets page "
    '(https://www.sec.gov/data-research/sec-markets-data/form-n-port-data-sets): "The data sets will be updated '
    'quarterly." Marks are the funds\' own fair values as filed: cite "as filed on Form N-PORT by <fund>", never '
    "as a vendor price. Marden sends SEC_USER_AGENT, caps all SEC hosts at 5 req/s, uses conditional GETs, and "
    "stores only filings that hold a watched instrument."
)

SOURCE = declare_source(
    SOURCE_ID, "SEC EDGAR Form N-PORT (fund holdings and marks)", "api", "https://www.sec.gov/Archives/edgar/",
    24 * 60, TERMS_NOTE,
)
if http.bucket_for("www.sec.gov")[1].rate > SEC_RATE_PER_SECOND:  # never raise a lower shared SEC limit
    http.set_rate_limit("sec.gov", SEC_RATE_PER_SECOND)


class CollectError(RuntimeError):
    """Some fetches or filings failed; the run is recorded as failed after everything else was collected."""


@dataclass
class RunStats:
    today: date
    since: date
    until: date
    datasets: list[str]
    periods: list[str] | None = None
    watched_keys: int = 0
    watched_instruments: int = 0
    ambiguous_keys: dict[str, list[str]] = field(default_factory=dict)
    index_days_read: list[str] = field(default_factory=list)
    index_days_done_before: list[str] = field(default_factory=list)
    index_days_missing: list[str] = field(default_factory=list)
    datasets_read: list[str] = field(default_factory=list)
    datasets_done_before: list[str] = field(default_factory=list)
    datasets_missing: list[str] = field(default_factory=list)
    dataset: dict[str, Any] = field(default_factory=dict)
    filings_listed: int = 0
    filings_known: int = 0
    filings_fetched: int = 0
    filings_skipped_no_match: int = 0
    bytes_skipped: int = 0
    filings_reference_only: int = 0
    dataset_candidates_unmatched: int = 0
    filings_matched: int = 0
    matched_by_period: Counter = field(default_factory=Counter)
    documents_new: int = 0
    holdings_new: int = 0
    holdings_seen: int = 0
    positions_marked: int = 0
    positions_without_mark: int = 0
    facts_new: int = 0
    prices_new: int = 0
    median_groups: int = 0
    median_prices_new: int = 0
    median_facts_new: int = 0
    wide_dispersion: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    ok: bool = True
    error: str | None = None

    def meta(self) -> dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items() if k not in ("today", "since", "until", "matched_by_period")}
        out.update(today=self.today.isoformat(), since=self.since.isoformat(), until=self.until.isoformat(),
                   matched_by_period=dict(sorted(self.matched_by_period.items())), errors=self.errors[:MAX_LISTED],
                   wide_dispersion=self.wide_dispersion[:MAX_LISTED])
        return out

    def summary(self) -> str:
        return json.dumps(self.meta(), indent=2, default=str)


class _Run:
    def __init__(self, stats: RunStats, fetcher: Fetcher, *, rescan: bool, periods: set[date] | None):
        self.stats = stats
        self.fetcher = fetcher
        self.rescan = rescan
        self.periods = periods
        self.watched = store.Watched()

    def collect(self) -> None:
        st = self.stats
        with db.transaction() as conn:
            self.watched = store.watched_identifiers(conn)
        st.watched_keys = len(self.watched.keys)
        st.watched_instruments = len({i for ids in self.watched.keys.values() for i in ids})
        st.ambiguous_keys = self.watched.ambiguous()
        if not self.watched.keys:
            st.errors.append("no watched identifiers (instrument cusip/isin or identifiers facts): nothing to match")
            return
        for label in st.datasets:
            self._dataset(label)
        day = st.since
        while day <= st.until:
            if day.weekday() < 5:
                self._index_day(day)
            day += timedelta(days=1)
        oldest = min([st.today - timedelta(days=MEDIAN_WINDOW_DAYS), *(self.periods or ())])
        with db.transaction() as conn:
            res = write_medians(conn, self.watched, since=oldest, producer=PRODUCER)
        st.median_groups, st.median_prices_new, st.median_facts_new = res.groups, res.prices_new, res.facts_new
        st.wide_dispersion = res.wide

    # ------------------------------------------------------------ discovery
    def _fetch_listing(self, url: str, missing: list[str], label: str, **kw: Any) -> DocumentRow | None:
        try:
            doc = http.fetch_document(SOURCE_ID, url, fetcher=self.fetcher, meta={"producer": PRODUCER}, **kw)
        except FetchError as exc:
            if exc.status in (403, 404):  # not published (yet), or no index that day (holiday)
                missing.append(label)
            else:
                self.stats.errors.append(f"{label}: {exc}")
            return None
        self.stats.documents_new += int(doc.was_new)
        return doc

    def _index_day(self, day: date) -> None:
        st = self.stats
        doc = self._fetch_listing(
            daily_index_url(day), st.index_days_missing, day.isoformat(), doc_type=INDEX_DOC_TYPE,
            title=f"EDGAR daily form index {day}", doc_date=day, external_id=f"form.{day:%Y%m%d}.idx",
        )
        if doc is None:
            return
        if doc.extract_state == "done" and not self.rescan:
            st.index_days_done_before.append(day.isoformat())
            return
        lines, _ = parse_form_index(doc.read().decode("utf-8", errors="replace"))
        filings = nport_lines(lines)
        st.filings_listed += len(filings)
        complete = all([self._filing(f.cik, f.accession, f.form, f.filed, via="daily_index") for f in filings])
        st.index_days_read.append(day.isoformat())
        if complete:
            documents.set_extract_state(doc.id, "done")

    def _dataset(self, label: str) -> None:
        st = self.stats
        doc = self._fetch_listing(
            dataset_url(label), st.datasets_missing, label, doc_type=DATASET_DOC_TYPE,
            title=f"Form N-PORT data set {label}", doc_date=quarter_end(*parse_label(label)), external_id=label,
        )
        if doc is None:
            return
        if doc.extract_state == "done" and not self.rescan:
            st.datasets_done_before.append(label)
            return
        with zipfile.ZipFile(io.BytesIO(doc.read())) as zf:
            found, dstats = dataset_candidates(zf, set(self.watched.keys), self.periods)
        st.datasets_read.append(label)
        st.dataset[label] = {**dstats.__dict__, "candidates": len(found)}
        complete = all([
            self._filing(c.cik, c.accession, c.form, c.filing_date, via=f"dera:{label}", expected=True)
            for c in found
        ])
        if complete and self.periods is None:
            documents.set_extract_state(doc.id, "done")

    # ------------------------------------------------------------ one filing
    def _filing(self, cik: str, accession: str, form: str, filed: date, *, via: str, expected: bool = False) -> bool:
        """Check, store and write one filing. False when a fetch failed (the listing stays open)."""
        st = self.stats
        with db.transaction() as conn:
            written = store.accession_written(conn, accession)
        if written and not self.rescan:
            st.filings_known += 1
            return True
        url = primary_doc_url(cik, accession)
        doc = documents.latest_document(SOURCE_ID, url)
        resp = None
        if doc is not None:
            data = doc.read()
        else:
            try:
                resp = self.fetcher.get(url, use_cache=False)
            except FetchError as exc:
                st.errors.append(f"{form} {accession} (CIK {cik}): {exc}")
                return False
            data = resp.content
            st.filings_fetched += 1
        if not prescan(data) & self.watched.keys.keys():
            st.filings_skipped_no_match += 1
            st.bytes_skipped += len(data)
            st.dataset_candidates_unmatched += int(expected)
            return True
        try:
            parsed = parse_nport(data)
        except NportParseError as exc:  # the bytes will not change: recorded once, not retried
            st.errors.append(f"{form} {accession}: unparseable: {exc}")
            return True
        found = store.matches(parsed.positions, self.watched)
        if not found:  # a watched id only as a derivative's reference instrument
            st.filings_reference_only += 1
            st.dataset_candidates_unmatched += int(expected)
            st.bytes_skipped += len(data) if doc is None else 0
            return True
        if doc is None:
            assert resp is not None
            doc = self._store(resp, form, accession, filed, cik, parsed.header, via, found)
        try:
            with db.transaction() as conn:
                w = store.write_filing(conn, doc, parsed.header, found, filing_date=filed, producer=PRODUCER)
        except ValidationError as exc:  # refused by a code validator: recorded once, not retried
            st.errors.append(f"{form} {accession}: refused: {exc}")
            return True
        st.filings_matched += 1
        st.matched_by_period[parsed.header.period.isoformat()] += 1
        st.holdings_new += w.holdings_new
        st.holdings_seen += w.holdings_seen
        st.positions_marked += w.marks
        st.positions_without_mark += w.no_mark
        st.facts_new += w.facts_new
        st.prices_new += w.prices_new
        return True

    def _store(self, resp: http.FetchResponse, form: str, accession: str, filed: date, cik: str,
               header: Any, via: str, found: list[store.Match]) -> DocumentRow:
        http_meta: dict[str, Any] = {"status": resp.status}
        if resp.etag:
            http_meta["etag"] = resp.etag
        if resp.last_modified:
            http_meta["last_modified"] = resp.last_modified
        if resp.final_url != resp.url:
            http_meta["final_url"] = resp.final_url
        doc = documents.record_document(
            SOURCE_ID, resp.url, resp.content, content_type=resp.content_type, doc_type=form,
            title=f"{form} {header.fund_name} ({header.period.isoformat()})", doc_date=filed, external_id=accession,
            meta={"producer": PRODUCER, "cik": cik, "form": form, "via": via, "period": header.period.isoformat(),
                  "series_id": header.series_id, "matched_keys": sorted({k for m in found for k in m.keys}),
                  "http": http_meta},
        )
        self.stats.documents_new += int(doc.was_new)
        return doc


def due_datasets(today: date) -> list[str]:
    """The previous quarter's data set, from the 7th of the quarter's first month on."""
    if (today.month - 1) % 3 == 0 and today.day < DATASET_FROM_DAY:
        return []
    return [dataset_label(*previous_quarter(today))]


def run(
    *,
    since: date | None = None,
    until: date | None = None,
    days: int | None = None,
    datasets: list[str] | None = None,
    periods: list[date] | None = None,
    rescan: bool = False,
    today: date | None = None,
    fetcher: Fetcher | None = None,
) -> RunStats:
    """One collection run (module docstring). A failed fetch is recorded on the source and the run
    returns with ok=False; bugs propagate (and are recorded by the heartbeat)."""
    today = today or today_et()
    until = until or today - timedelta(days=1)
    since = since or today - timedelta(days=LOOKBACK_DAYS if days is None else days)
    labels = [dataset_label(*parse_label(x)) for x in datasets] if datasets is not None else due_datasets(today)
    stats = RunStats(today=today, since=since, until=until, datasets=labels,
                     periods=sorted(p.isoformat() for p in periods) if periods else None)
    sources.upsert_spec(SOURCE)
    try:
        with heartbeat.run(SOURCE_ID) as hb:
            try:
                _Run(stats, fetcher or http.default_fetcher(), rescan=rescan,
                     periods=set(periods) if periods else None).collect()
            finally:
                hb.meta.update(stats.meta())
                hb.seen(stats.filings_listed + sum(d.get("candidates", 0) for d in stats.dataset.values()))
                hb.new(stats.filings_matched)
            if stats.errors:
                raise CollectError(f"{len(stats.errors)} problem(s); first: {stats.errors[0]}")
    except (CollectError, FetchError, SourceError) as exc:
        stats.ok, stats.error = False, str(exc)
        log.warning("sec_nport run failed: %s", exc)
    return stats


# ---------------------------------------------------------------- CLI and queue
def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--since", type=date.fromisoformat, help="first daily index day (default: today - 10 days)")
    p.add_argument("--until", type=date.fromisoformat, help="last daily index day (default: yesterday)")
    p.add_argument("--days", type=int, help=f"daily index window in days (default {LOOKBACK_DAYS}; 0 = none)")
    p.add_argument("--dataset", action="append", dest="datasets",
                   help="DERA quarterly data set to backfill from, e.g. 2026q3 (repeatable)")
    p.add_argument("--period", action="append", dest="periods", type=date.fromisoformat,
                   help="with --dataset: only filings reporting as of this date (repeatable)")
    p.add_argument("--rescan", action="store_true", help="re-check indexes and data sets already done")


@cli.action("collect", SOURCE_ID, help="Form N-PORT fund holdings and marks for watched instruments", configure=_args)
def collect_command(args: argparse.Namespace) -> int:
    stats = run(since=args.since, until=args.until, days=args.days, datasets=args.datasets, periods=args.periods,
                rescan=args.rescan)
    print(stats.summary())
    return 0 if stats.ok else 1


@app.periodic(cron=CRON_UTC, periodic_id="daily_0700_et")
@app.task(name="collect.sec_nport", queue="collect", queueing_lock="collect.sec_nport", lock="collect.sec_nport")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    if not due_et(timestamp, *RUN_AT_ET):
        return {"skipped": "not 07:00 ET"}
    return run().meta()


@app.task(name="collect.sec_nport_backfill", queue="collect", queueing_lock="collect.sec_nport_backfill",
          lock="collect.sec_nport")
def backfill_task(datasets: list[str], periods: list[str] | None = None, days: int = 0) -> dict[str, Any]:
    """Deferred by hand: backfill from DERA data sets (e.g. ['2026q3']), optionally only some periods."""
    return run(datasets=datasets, periods=[date.fromisoformat(p) for p in periods or ()], days=days).meta()
