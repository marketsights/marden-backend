"""`sec_xbrl`: company financials from SEC XBRL companyfacts (spec 24 "Data and where it comes from").

For every target CIK (sec_xbrl_store.targets: the V1 payers, neoclouds, developers, chipmakers and
power companies in the entity table, plus Broadcom), one run:

1. Fetches https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json through `fetch_document`
   (stored raw; identical bytes are not stored twice). A 404 means the CIK files no XBRL and is noted.
2. Checks the filer name against the entity (a wrong CIK is reported, never written).
3. Writes the periodic-report facts (sec_xbrl_parse.FIELDS) as published tag F entity facts, valid_from
   = period end, `public_date` = the filing date that first made a period public, the XBRL concept and
   filing in quote_loc. Structured filed data: nothing is queued for extraction.

Cadence: weekly (Sunday 09:23 UTC, early morning ET), plus `collect.sec_xbrl_on_filing` every 15 minutes, which re-runs
the CIKs whose new 10-Q/10-K sec_index has announced as a `filing` event (sec_xbrl_store.due_filings).
Only data.sec.gov/api/xbrl/ is read, through the shared SEC bucket (<= 5 req/s, SEC_USER_AGENT).
Reruns write nothing new.
"""

from __future__ import annotations

import argparse
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from marden import cli
from marden.collectors import sec_xbrl_store as store
from marden.collectors.sec_index_parse import cik10
from marden.collectors.sec_xbrl_parse import companyfacts_url, parse_companyfacts, select
from marden.core import db, heartbeat, http, sources
from marden.core.http import Fetcher, FetchError
from marden.core.queue import app
from marden.core.sources import SourceError, declare_source

log = logging.getLogger(__name__)

SOURCE_ID = store.SOURCE_ID
PRODUCER = store.PRODUCER
SEC_RATE_PER_SECOND = 5.0  # docs/methods/price-sources.md §5: all SEC collectors share <= 5 req/s
DEFAULT_SINCE = date(2019, 1, 1)  # ASC 842 (leases on the balance sheet) took effect for 2019
EASTERN = ZoneInfo("America/New_York")
WEEKLY_CRON = "23 9 * * 0"  # Sunday 09:23 UTC (05:23 EDT / 04:23 EST), before the Sunday-night risk ranking
ON_FILING_CRON = "2-59/15 * * * *"  # after sec_index's 10-minute polls; a database check unless a CIK is due
MAX_LISTED = 100

TERMS_NOTE = (
    'SEC: "Information presented on sec.gov is considered public information and may be copied or further '
    "distributed by users of the web site without the SEC's permission. Please consider appropriate citation "
    'to the SEC as the source." (https://www.sec.gov/about/privacy-information, last reviewed Nov. 29, 2023). '
    '"data.sec.gov" was created to host RESTful data APIs ... These APIs do not require any authentication or '
    'API keys to access. Currently included in the APIs are the submissions history by filer and the XBRL data '
    'from financial statements (forms 10-Q, 10-K,8-K, 20-F, 40-F, 6-K, and their variants)." '
    "(https://www.sec.gov/search-filings/edgar-application-programming-interfaces, last reviewed Apr. 8, 2025). "
    'Fair access: "Current guidelines limit users to a total of no more than 10 requests per second" and "The '
    'SEC does not allow "unclassified" bots or automated tools to crawl the site." (privacy page); "Please '
    'declare your user agent in request headers" (https://www.sec.gov/search-filings/edgar-search-assistance/'
    "accessing-edgar-data). data.sec.gov has no robots.txt (404, read 2026-10-21); www.sec.gov/robots.txt "
    "disallows /cgi-bin and /search/, which this collector never calls. Marden sends SEC_USER_AGENT, shares one "
    "5 req/s SEC bucket, and reads only data.sec.gov/api/xbrl/companyfacts/."
)

SOURCE = declare_source(
    SOURCE_ID, "SEC EDGAR XBRL company facts (data.sec.gov)", "api", "https://data.sec.gov/api/xbrl/",
    7 * 24 * 60, TERMS_NOTE,
)
if http.bucket_for("data.sec.gov")[1].rate > SEC_RATE_PER_SECOND:  # never raise a lower shared SEC limit
    http.set_rate_limit("sec.gov", SEC_RATE_PER_SECOND)


class CollectError(RuntimeError):
    """Some fetches failed; the run is recorded as failed after everything else was collected."""


@dataclass
class RunStats:
    mode: str
    today: date
    since: date
    targets: int = 0
    documents_new: int = 0
    documents_unchanged: int = 0
    facts_new: int = 0
    values_seen: int = 0
    missing_ciks: list[str] = field(default_factory=list)  # 404: the CIK has no XBRL company facts
    empty_ciks: list[str] = field(default_factory=list)  # a companyfacts file without any fact
    name_mismatch: list[str] = field(default_factory=list)
    fetched: dict[str, dict[str, Any]] = field(default_factory=dict)  # CIK -> what the due check needs
    facts_by_cik: dict[str, int] = field(default_factory=dict)
    skipped: Counter = field(default_factory=Counter)
    concepts: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)
    ok: bool = True
    error: str | None = None

    def meta(self) -> dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items() if k not in ("today", "since", "skipped", "concepts")}
        out.update(today=self.today.isoformat(), since=self.since.isoformat(),
                   skipped=dict(sorted(self.skipped.items())), concepts=dict(self.concepts.most_common()),
                   errors=self.errors[:MAX_LISTED])
        return out

    def summary(self) -> str:
        lines = [
            f"sec_xbrl {self.mode} (periods since {self.since}): {'ok' if self.ok else 'FAILED'}",
            f"  targets {self.targets}; documents new {self.documents_new}, unchanged {self.documents_unchanged}; "
            f"no XBRL (404) {self.missing_ciks or '-'}; empty {self.empty_ciks or '-'}",
            f"  values {self.values_seen}; facts new {self.facts_new} {self.facts_by_cik}",
            f"  filer name mismatch (not written): {self.name_mismatch or '-'}",
            f"  skipped records {dict(sorted(self.skipped.items()))}",
        ]
        if self.error:
            lines.append(f"  error: {self.error}")
        return "\n".join(lines)


def _collect_one(stats: RunStats, target: store.Target, fetcher: Fetcher) -> int:
    """Fetch, check and write one CIK. Returns new facts; fetch failures go to stats.errors."""
    cik = target.cik
    try:
        doc = http.fetch_document(
            SOURCE_ID, companyfacts_url(cik), doc_type="sec_companyfacts", title=f"XBRL company facts CIK{cik10(cik)}",
            doc_date=stats.today, external_id=f"CIK{cik10(cik)}", meta={"producer": PRODUCER, "cik": cik},
            fetcher=fetcher,
        )
    except FetchError as exc:
        if exc.status == 404:  # data.sec.gov answers NoSuchKey for a CIK without XBRL financial data
            stats.missing_ciks.append(cik)
        else:
            stats.errors.append(f"companyfacts CIK {cik}: {exc}")
        return 0
    if doc.was_new:
        stats.documents_new += 1
    else:
        stats.documents_unchanged += 1
    try:
        cf = parse_companyfacts(doc.read())
    except ValueError as exc:
        stats.errors.append(f"companyfacts CIK {cik}: unreadable: {exc}")
        return 0
    latest = cf.latest_filed()
    stats.fetched[cik] = {
        "filer": cf.name, "latest_filed": latest.isoformat() if latest else None,
        "recent_accessions": cf.accessions_filed_since(stats.today - timedelta(days=store.RECENT_ACCESSION_DAYS)),
    }
    if not cf.concepts:
        stats.empty_ciks.append(cik)
        return 0
    if not store.name_matches(cf.name, target.names):
        stats.name_mismatch.append(f"CIK {cik} files as {cf.name!r}; entity {', '.join(target.entity_ids)}")
        return 0
    sel = select(cf, since=stats.since, today=stats.today)
    stats.values_seen += sel.values
    stats.skipped.update(sel.skipped)
    stats.concepts.update(sel.concepts)
    with db.transaction() as conn:
        new = store.write_observations(conn, cik, target.entity_ids, sel.observations, filer_name=cf.name, doc=doc)
    stats.facts_by_cik[cik] = new
    return new


def run(
    *,
    ciks: list[str] | None = None,
    since: date | None = None,
    today: date | None = None,
    fetcher: Fetcher | None = None,
    mode: str = "weekly",
) -> RunStats:
    """One collection run (see module docstring). A failed fetch is recorded on the source and the run
    returns with ok=False; bugs propagate (and are recorded by the heartbeat)."""
    today = today or datetime.now(EASTERN).date()
    stats = RunStats(mode=mode, today=today, since=since or DEFAULT_SINCE)
    sources.upsert_spec(SOURCE)
    fetcher = fetcher or http.default_fetcher()
    try:
        with heartbeat.run(SOURCE_ID) as hb:
            try:
                with db.transaction() as conn:
                    store.ensure_issuers(conn)
                    targets = store.targets(conn, ciks)
                stats.targets = len(targets)
                for target in targets:
                    hb.seen()
                    new = _collect_one(stats, target, fetcher)
                    stats.facts_new += new
                    if new:
                        hb.new()
            finally:
                hb.meta.update(stats.meta())
            if stats.errors:
                raise CollectError(f"{len(stats.errors)} fetch(es) failed; first: {stats.errors[0]}")
    except (CollectError, FetchError, SourceError) as exc:
        stats.ok, stats.error = False, str(exc)
        log.warning("sec_xbrl run failed: %s", exc)
    return stats


def due_ciks(today: date | None = None) -> list[str]:
    """CIKs with a new 10-Q/10-K (sec_index filing event) whose XBRL facts no run has collected yet."""
    today = today or datetime.now(EASTERN).date()
    with db.transaction() as conn:
        return sorted({d.cik for d in store.due_filings(conn, today=today)}, key=int)


def run_due(today: date | None = None, fetcher: Fetcher | None = None) -> RunStats | None:
    ciks = due_ciks(today)
    if not ciks:
        return None
    return run(ciks=ciks, today=today, fetcher=fetcher, mode="on_filing")


# ---------------------------------------------------------------- CLI and queue
def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--cik", action="append", dest="ciks", help="only this target CIK (repeatable)")
    p.add_argument("--since", type=date.fromisoformat, default=None,
                   help=f"earliest period end to write (default {DEFAULT_SINCE})")
    p.add_argument("--due", action="store_true", help="only CIKs with a new 10-Q/10-K not collected yet")


@cli.action("collect", SOURCE_ID, help="Company financials from SEC XBRL companyfacts (payers, neoclouds, ...)",
            configure=_args)
def collect_command(args: argparse.Namespace) -> int:
    if args.due:
        due = run_due()
        if due is None:
            print("sec_xbrl: no CIK has a new 10-Q/10-K to collect")
            return 0
        stats = due
    else:
        stats = run(ciks=args.ciks, since=args.since)
    print(stats.summary())
    return 0 if stats.ok else 1


@app.periodic(cron=WEEKLY_CRON, periodic_id="weekly")
@app.task(name="collect.sec_xbrl", queue="collect", queueing_lock="collect.sec_xbrl", lock="collect.sec_xbrl")
def collect_sec_xbrl_task(timestamp: int | None = None) -> dict[str, Any]:
    return run().meta()


@app.periodic(cron=ON_FILING_CRON, periodic_id="on_filing")
@app.task(name="collect.sec_xbrl_on_filing", queue="collect", queueing_lock="collect.sec_xbrl_on_filing",
          lock="collect.sec_xbrl")
def collect_sec_xbrl_on_filing_task(timestamp: int | None = None) -> dict[str, Any]:
    stats = run_due()
    return {"due": 0} if stats is None else stats.meta()
