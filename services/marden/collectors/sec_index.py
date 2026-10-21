"""`sec_index`: EDGAR filing discovery for watched CIKs (spec 22 "What runs when": every 10 min).

A run, for the window `[today - days, today]` (US Eastern; 7 days, or 120 in backfill mode):

1. Daily form index `form.YYYYMMDD.idx` for today and the previous business day. Today's file is
   published only at night (SEC: "Indexes incorporating the current business day's filings are
   updated nightly starting about 10:00 p.m., ET"), so a 403/404 means "not yet" and is noted.
2. data.sec.gov submissions JSON for every watched CIK (each `entity` row with a cik: issuers,
   BDCs, fund filers, SPVs). It is updated in real time, so it finds filings during the day; older
   `-submissions-NNN.json` pages are read when `recent` does not reach back to the window start.
3. Each filing of a relevant form (8-K, 10-Q, 10-K, 424B*, S-1/F-1, ABS-15G, 10-D, 13D) not yet
   recorded: its `-index.htm` page is stored, then the primary document and its EX-4 / EX-10 /
   EX-99 exhibits (doc_type = EDGAR type, doc_date = filing date, external_id = accession). 8-K,
   424B*, S-1/F-1 documents and every EX-10 get extract_state 'queued' for the extraction workers.
   One `filing` event (tag F, published) goes on each entity with that CIK, the instruments it or
   its direct subsidiaries issued, their sites and the sites it develops (sec_index_store).

Only /Archives/edgar/ and data.sec.gov/submissions/ are read (never /cgi-bin or /search/); all SEC
hosts share one 5 req/s bucket with the declared SEC_USER_AGENT. Reruns write nothing new.
"""

from __future__ import annotations

import argparse
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from marden import cli
from marden.collectors import sec_index_store as store
from marden.collectors.sec_index_parse import (
    Filing,
    IndexLine,
    cik10,
    daily_index_url,
    extract_state_for,
    filing_index_url,
    is_relevant_form,
    pages_needed,
    parse_filing_index,
    parse_form_index,
    parse_submissions,
    previous_business_day,
    relevant_documents,
    submissions_page_url,
    submissions_url,
)
from marden.core import db, heartbeat, http, sources
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, FetchError
from marden.core.queue import app
from marden.core.sources import SourceError, declare_source

log = logging.getLogger(__name__)

SOURCE_ID = "sec_index"
PRODUCER = "collector:sec_index"
SEC_RATE_PER_SECOND = 5.0  # docs/methods/price-sources.md §5: all SEC collectors share <= 5 req/s
LOOKBACK_DAYS = 7
BACKFILL_DAYS = 120
EASTERN = ZoneInfo("America/New_York")
# Every 10 min while EDGAR disseminates (06:00-22:00 ET) and while the nightly index is built.
# Cron fields are UTC: 10-23 UTC Mon-Fri plus 00-04 UTC Tue-Sat covers 06:00-23:59 ET in EST and
# EDT; `in_edgar_hours` drops the extra hour each side.
DAY_CRON = "*/10 10-23 * * 1-5"
EVENING_CRON = "*/10 0-4 * * 2-6"
MAX_LISTED = 100

TERMS_NOTE = (
    'SEC: "Information presented on sec.gov is considered public information and may be copied or further '
    "distributed by users of the web site without the SEC's permission. Please consider appropriate citation "
    'to the SEC as the source." (https://www.sec.gov/about/privacy-information, last reviewed Nov 29, 2023). '
    'Fair access: "Current max request rate: 10 requests/second." and "Please declare your user agent in '
    'request headers" (https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data); '
    '"The SEC does not allow "unclassified" bots or automated tools to crawl the site." Marden sends '
    "SEC_USER_AGENT and caps every SEC host at 5 req/s. robots.txt (https://www.sec.gov/robots.txt, read "
    "2026-10-21) allows /Archives/edgar/data and disallows /cgi-bin and /search/; data.sec.gov has none "
    "(404). This collector reads only /Archives/edgar/daily-index/, /Archives/edgar/data/ and "
    'data.sec.gov/submissions/, which "do not require any authentication or API keys to access" '
    "(https://www.sec.gov/search-filings/edgar-application-programming-interfaces)."
)

SOURCE = declare_source(
    SOURCE_ID, "SEC EDGAR filing discovery (daily index + submissions API)", "api",
    "https://www.sec.gov/Archives/edgar/", 10, TERMS_NOTE,
)
if http.bucket_for("www.sec.gov")[1].rate > SEC_RATE_PER_SECOND:  # never raise a lower shared SEC limit
    http.set_rate_limit("sec.gov", SEC_RATE_PER_SECOND)


class CollectError(RuntimeError):
    """Some fetches failed; the run is recorded as failed after everything else was collected."""


@dataclass
class RunStats:
    mode: str
    today: date
    since: date
    watched_ciks: int = 0
    watched_entities: int = 0
    bad_entity_ciks: list[str] = field(default_factory=list)
    index_days_read: list[str] = field(default_factory=list)
    index_days_pending: list[str] = field(default_factory=list)
    index_unparsed_lines: int = 0
    index_filings_watched: int = 0
    submissions_read: int = 0
    submissions_changed: int = 0
    missing_ciks: list[str] = field(default_factory=list)
    filings_found: int = 0
    filings_known: int = 0
    filings_new: int = 0
    new_accessions: list[str] = field(default_factory=list)
    documents_new: int = 0
    documents_queued: int = 0
    documents_skipped: int = 0
    documents_by_type: Counter = field(default_factory=Counter)
    events_new: int = 0
    errors: list[str] = field(default_factory=list)
    ok: bool = True
    error: str | None = None

    def meta(self) -> dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items() if k not in ("documents_by_type", "today", "since")}
        out.update(today=self.today.isoformat(), since=self.since.isoformat(),
                   documents_by_type=dict(sorted(self.documents_by_type.items())), errors=self.errors[:MAX_LISTED])
        return out

    def summary(self) -> str:
        lines = [
            f"sec_index {self.mode} {self.since}..{self.today}: {'ok' if self.ok else 'FAILED'}",
            f"  watched CIKs {self.watched_ciks} ({self.watched_entities} entities); submissions read "
            f"{self.submissions_read} ({self.submissions_changed} changed); missing CIKs {len(self.missing_ciks)}",
            f"  daily index read {self.index_days_read or '-'}; not yet published {self.index_days_pending or '-'}; "
            f"watched lines {self.index_filings_watched}",
            f"  filings found {self.filings_found}: new {self.filings_new}, already recorded {self.filings_known}",
            f"  documents new {self.documents_new} (queued {self.documents_queued}, skipped "
            f"{self.documents_skipped} irrelevant) {dict(sorted(self.documents_by_type.items()))}",
            f"  events new {self.events_new}",
        ]
        if self.error:
            lines.append(f"  error: {self.error}")
        return "\n".join(lines)


class _Run:
    def __init__(self, stats: RunStats, fetcher: Fetcher, backfill: bool):
        self.stats = stats
        self.fetcher = fetcher
        self.backfill = backfill

    def _fetch(self, url: str, **kw: Any) -> DocumentRow:
        doc = http.fetch_document(SOURCE_ID, url, fetcher=self.fetcher, **kw)
        if doc.was_new:
            self.stats.documents_new += 1
            self.stats.documents_by_type[doc.doc_type or "-"] += 1
            if doc.extract_state == "queued":
                self.stats.documents_queued += 1
        return doc

    def collect(self, hb: heartbeat.RunHandle, ciks: list[str] | None) -> None:
        st = self.stats
        with db.transaction() as conn:
            watched = store.watched_ciks(conn, ciks)
        st.watched_ciks, st.watched_entities = len(watched), sum(len(v) for v in watched.entities.values())
        st.bad_entity_ciks = watched.bad[:MAX_LISTED]
        found: dict[tuple[str, str], Filing] = {}
        days = [previous_business_day(st.today)] + ([st.today] if st.today.weekday() < 5 else [])
        for day in days:
            for line in self._daily_index(day):
                if line.cik in watched.entities and is_relevant_form(line.form) and line.filed >= st.since:
                    st.index_filings_watched += 1
                    key = (line.cik, line.accession)
                    found.setdefault(key, Filing(line.cik, line.accession, line.form, line.filed))
        for cik in sorted(watched.entities, key=int):
            for filing in self._submissions(cik):
                if is_relevant_form(filing.form) and st.since <= filing.filing_date <= st.today:
                    found[(cik, filing.accession)] = filing
        st.filings_found = len(found)
        hb.seen(len(found))
        for key in sorted(found, key=lambda k: (found[k].filing_date, k[1], int(k[0]))):
            filing = found[key]
            try:
                if self._process(filing, watched.entities[filing.cik]):
                    hb.new()
            except (FetchError, ValueError) as exc:
                st.errors.append(f"{filing.form} {filing.accession} (CIK {filing.cik}): {exc}")

    def _daily_index(self, day: date) -> list[IndexLine]:
        try:
            doc = self._fetch(
                daily_index_url(day), doc_type="edgar_daily_index", title=f"EDGAR daily form index {day}",
                doc_date=day, external_id=f"form.{day:%Y%m%d}.idx", meta={"producer": PRODUCER},
            )
        except FetchError as exc:
            if exc.status in (403, 404):  # S3 answers AccessDenied/NoSuchKey until the night build
                self.stats.index_days_pending.append(day.isoformat())
            else:
                self.stats.errors.append(f"daily index {day}: {exc}")
            return []
        lines, unparsed = parse_form_index(doc.read().decode("utf-8", errors="replace"))
        self.stats.index_days_read.append(day.isoformat())
        self.stats.index_unparsed_lines += unparsed
        return lines

    def _submissions(self, cik: str) -> list[Filing]:
        st = self.stats
        meta = {"producer": PRODUCER, "cik": cik}
        try:
            doc = self._fetch(submissions_url(cik), doc_type="sec_submissions", doc_date=st.today,
                              title=f"EDGAR submissions CIK{cik10(cik)}", external_id=f"CIK{cik10(cik)}", meta=meta)
            st.submissions_read += 1
            st.submissions_changed += int(doc.was_new)
            page = parse_submissions(doc.read(), cik)
            filings = list(page.filings)
            for name in pages_needed(page, st.since):
                older = self._fetch(submissions_page_url(name), doc_type="sec_submissions_page", title=name,
                                    doc_date=st.today, external_id=name, meta=meta)
                filings += parse_submissions(older.read(), cik).filings
        except FetchError as exc:
            if exc.status == 404:  # data.sec.gov answers NoSuchKey for a CIK that does not exist
                st.missing_ciks.append(cik)
            else:
                st.errors.append(f"submissions CIK {cik}: {exc}")
            return []
        except ValueError as exc:
            st.errors.append(f"submissions CIK {cik}: unreadable JSON: {exc}")
            return []
        return filings

    def _process(self, filing: Filing, entity_ids: list[str]) -> bool:
        """Record one filing; True when it produced new events."""
        st = self.stats
        with db.transaction() as conn:
            index_doc = store.index_document(conn, SOURCE_ID, filing.accession)
            subjects = store.event_subjects(conn, entity_ids)
            # Known filings are skipped unless a subject was linked since (or in backfill mode).
            done = index_doc is not None and not self.backfill and (
                set(subjects) <= store.subjects_with_event(conn, index_doc.id))
        if done:
            st.filings_known += 1
            return False
        if index_doc is None:
            index_doc = self._fetch(
                filing_index_url(filing.cik, filing.accession), doc_type=store.FILING_INDEX,
                title=f"{filing.form} {filing.accession}", doc_date=filing.filing_date,
                external_id=filing.accession,
                meta={"producer": PRODUCER, "cik": filing.cik, "form": filing.form, "items": list(filing.items),
                      "report_date": filing.report_date.isoformat() if filing.report_date else None,
                      "primary_document": filing.primary_document},
            )
        fi = parse_filing_index(index_doc.read(), index_doc.url)
        docs, skipped = relevant_documents(fi)
        with db.transaction() as conn:
            have = store.stored_urls(conn, SOURCE_ID, [d.url for d, _ in docs])
        for doc, primary in docs:
            if doc.url in have:
                continue
            self._fetch(
                doc.url, doc_type=doc.doc_type, title=doc.description or doc.filename, doc_date=fi.filing_date,
                external_id=fi.accession, extract_state=extract_state_for(fi.form, doc.doc_type),
                meta={"producer": PRODUCER, "cik": filing.cik, "form": fi.form, "seq": doc.seq,
                      "filename": doc.filename, "primary": primary, "index_url": index_doc.url},
            )
        if index_doc.was_new:
            st.documents_skipped += skipped
        with db.transaction() as conn:
            new = store.write_filing_events(conn, fi, filing.cik, subjects, index_doc)
        st.events_new += new
        if new:
            st.filings_new += 1
            if len(st.new_accessions) < MAX_LISTED:
                st.new_accessions.append(f"{fi.form} {fi.accession} {fi.filing_date} CIK {filing.cik}")
        else:
            st.filings_known += 1
        return new > 0


def run(
    *,
    days: int | None = None,
    backfill: bool = False,
    ciks: list[str] | None = None,
    today: date | None = None,
    fetcher: Fetcher | None = None,
) -> RunStats:
    """One collection run (see module docstring). A failed fetch is recorded on the source and the
    run returns with ok=False; bugs propagate (and are recorded by the heartbeat)."""
    today = today or datetime.now(EASTERN).date()
    window = days if days is not None else (BACKFILL_DAYS if backfill else LOOKBACK_DAYS)
    if window < 0:
        raise ValueError("days must be >= 0")
    stats = RunStats(mode="backfill" if backfill else "poll", today=today, since=today - timedelta(days=window))
    sources.upsert_spec(SOURCE)
    try:
        with heartbeat.run(SOURCE_ID) as hb:
            try:
                _Run(stats, fetcher or http.default_fetcher(), backfill).collect(hb, ciks)
            finally:
                hb.meta.update(stats.meta())
            if stats.errors:
                raise CollectError(f"{len(stats.errors)} fetch(es) failed; first: {stats.errors[0]}")
    except (CollectError, FetchError, SourceError) as exc:
        stats.ok, stats.error = False, str(exc)
        log.warning("sec_index run failed: %s", exc)
    return stats


# ---------------------------------------------------------------- CLI and queue
def in_edgar_hours(timestamp: int | None) -> bool:
    """A periodic run at `timestamp` (UTC epoch) is due on weekdays 06:00-23:59 ET: EDGAR disseminates
    06:00-22:00 ET and builds the nightly index after that. A manual defer (no timestamp) is always due."""
    if timestamp is None:
        return True
    local = datetime.fromtimestamp(timestamp, UTC).astimezone(EASTERN)
    return local.weekday() < 5 and local.hour >= 6


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--backfill", action="store_true", help=f"look back {BACKFILL_DAYS} days per watched CIK")
    p.add_argument("--days", type=int, default=None,
                   help=f"window in days (default {LOOKBACK_DAYS}, or {BACKFILL_DAYS} with --backfill)")
    p.add_argument("--cik", action="append", dest="ciks", help="only this watched CIK (repeatable)")


@cli.action("collect", SOURCE_ID, help="Discover new EDGAR filings of watched CIKs (daily index + submissions)",
            configure=_args)
def collect_command(args: argparse.Namespace) -> int:
    stats = run(days=args.days, backfill=args.backfill, ciks=args.ciks)
    print(stats.summary())
    return 0 if stats.ok else 1


@app.periodic(cron=EVENING_CRON, periodic_id="evening")
@app.periodic(cron=DAY_CRON, periodic_id="day")
@app.task(name="collect.sec_index", queue="collect", queueing_lock="collect.sec_index", lock="collect.sec_index")
def collect_sec_index_task(timestamp: int | None = None) -> dict[str, Any]:
    if not in_edgar_hours(timestamp):
        return {"skipped": "outside EDGAR hours (weekdays 06:00-24:00 ET)"}
    return run().meta()


@app.task(name="collect.sec_index_backfill", queue="collect", queueing_lock="collect.sec_index_backfill",
          lock="collect.sec_index")
def backfill_sec_index_task(days: int = BACKFILL_DAYS, ciks: list[str] | None = None) -> dict[str, Any]:
    """Deferred by hand (or when a CIK starts being watched): the last `days` of filings."""
    return run(days=days, backfill=True, ciks=ciks).meta()
