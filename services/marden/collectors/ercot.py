"""ERCOT large-load interconnection status -> `series` facts (tag F, source ERCOT). Parsers: ercot_parse.

What ERCOT publishes. ERCOT's Large Load Integration Team posts a monthly "Large Load Interconnection Status
Update" deck (pptx or pdf) on the Large Load Working Group meeting pages (https://www.ercot.com/committees/tac/llwg
-> /calendar/MMDDYYYY-LLWG-Meeting -> /files/docs/...), titled "February TAC Report", "March TAC Report
Updated_03262026", "April 24 LLWG Report_042426", "May 21 LLWG Report", "June 19 LLWG Report" and so on. The
decks are aggregates only: queue MW by status, load zone, TSP, size, type, submittal and in-service quarter,
all drawn as pictures, with categories under five customers folded into "Other ... to protect Customer
data". ERCOT publishes no per-project large-load list (no project names, counties or dates), so nothing here
can be matched to a campus or site and no per-project rows or status-change events are written; the
www.ercot.com Large Load Integration page carries forms and Batch Zero (PGRR 145) material, not a queue. The
last status update posted is June 19, 2026; since July 2026 large-load figures appear in the Board's Batch
Zero decks instead, so a weekly run may find nothing new until ERCOT posts another.

What is written, per status update (valid_from = the deck's "current as of" date, else its title-slide date;
quote = ERCOT's sentence, quote_loc = the slide or page), each a series observation (field 'value', MW):
- ercot_large_load_approved_to_energize_mw: "Of the N MW that have received Approval to Energize";
- ercot_large_load_peak_nonsimultaneous_mw: the non-simultaneous monthly peak of approved loads;
- ercot_large_load_peak_simultaneous_mw: the simultaneous monthly peak.
The month ERCOT names for a peak stays in the quote only: two decks carry a year typo ("February 2025" in the
February 25, 2026 deck). Each status deck gets extract_state 'queued' so the extraction workers can read the
chart pictures (queue MW by status, zone, TSP); nothing is read off an image here.

Pages and politeness. robots.txt is checked for every URL (/calendar, /committees and /files/docs are
allowed; /misapp, /misdownload and /meetings are not). One request per 5 s: Imperva bot protection answered
403 to bursts near 1 request a second; a challenge (403 or challenge page) stops the run and is recorded on the
source, and is never retried around. ERCOT's HTML embeds per-request tokens, so a page is stored as a new
document only when its links changed (the document table is the cache, as with a 304). The LLWG page is read
every run; a meeting page is read once when first seen and again for RECENT_DAYS after the meeting (decks are
posted up to three weeks later), and older pages are parsed from their stored copy without a request; a deck is
downloaded once (its dated /files/docs URL never changes).

Cadence: weekly (Mondays 13:41 UTC); ERCOT posts monthly. `python -m marden collect ercot`.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any
from urllib.robotparser import RobotFileParser

from marden import cli
from marden.collectors import ercot_parse as parse
from marden.collectors.rates_common import RunStats, collect, print_stats, today_et
from marden.core import db, documents, http, sources
from marden.core.documents import DocumentRow
from marden.core.facts import FactWriter
from marden.core.http import Fetcher, FetchError, default_fetcher
from marden.core.queue import app
from marden.core.sources import declare_source

SOURCE_ID = "ercot"
SOURCE_NAME = "ERCOT"
PRODUCER = f"collector:{SOURCE_ID}"
HOST = parse.HOST
BASE = f"https://{HOST}"
ROBOTS_URL = f"{BASE}/robots.txt"
LLWG_URL = f"{BASE}/committees/tac/llwg"
ROBOTS_AGENT = "MardenBot"
CRON_UTC = "41 13 * * 1"  # Mondays 13:41 UTC
RECENT_DAYS = 70  # re-read a meeting page this long after the meeting
PER_SECOND = 0.2
TERMS_NOTE = (
    "Checked 2026-10-21. ERCOT Terms of Use (https://www.ercot.com/help/terms, last updated 07/20/2023), 5: \"The "
    "publicly available contents of this website may be used, reproduced, and redistributed, provided that the "
    "contents are not modified and that you maintain all copyright and other notices contained in the contents, "
    "including this Agreement. Notwithstanding the foregoing, raw data provided in public portions of this website "
    "may be used, reproduced, and redistributed in compilations, charts, and analyses without maintaining such "
    "notices.\" 6: \"Use of this website in a manner that negatively affects the performance of this website or "
    "other ERCOT systems is prohibited.\" The API terms (https://www.ercot.com/help/terms/data-portal) apply their "
    "policy to \"Automated screen scraping programs that access ERCOT URL(s)\" to \"Prevent overloading of ERCOT "
    "computing resources\". www.ercot.com/robots.txt (\"User-agent: *\") disallows /misapp, /misdownload, "
    "/meetings and others, but not /committees, /calendar or /files/docs, the paths read here. Imperva bot "
    "protection answered 403 to bursts near 1 request/s, so requests are spaced 5 s apart and a challenge stops "
    "the run. No credential is needed. The status decks are aggregates: \"The Other category includes categories "
    "in which there are less than five customers and is aggregated to protect Customer data\"."
)
SOURCE = declare_source(SOURCE_ID, "ERCOT large-load interconnection status (LLWG)", "portal", BASE, 7 * 24 * 60,
                        TERMS_NOTE)
http.set_rate_limit(HOST, PER_SECOND)


class BotChallenge(FetchError):
    """ERCOT's bot protection answered instead of the page: the run stops, nothing is retried around it."""


def _challenge(url: str, status: int | None) -> BotChallenge:
    return BotChallenge(url, status, "ERCOT bot protection (Imperva) challenged the request; run stopped")


def _get(url: str, fetcher: Fetcher) -> http.FetchResponse:
    sources.require_collectable(SOURCE_ID)
    try:
        resp = fetcher.get(url, use_cache=False)
    except FetchError as exc:
        if exc.status in (401, 403):
            raise _challenge(url, exc.status) from None
        raise
    if parse.is_challenge(resp.content):
        raise _challenge(url, resp.status)
    return resp


def _page(url: str, doc_type: str, title: str, fetcher: Fetcher, stats: RunStats,
          content: Callable[[bytes], Any]) -> DocumentRow:
    """GET a page; store it raw unless what we read from it (`content`) is unchanged since the stored copy."""
    resp = _get(url, fetcher)
    prior = documents.latest_document(SOURCE_ID, url)
    if prior is not None and content(prior.read()) == content(resp.content):
        stats.document(prior)
        return prior
    doc = documents.record_document(SOURCE_ID, url, resp.content, content_type=resp.content_type, doc_type=doc_type,
                                    title=title, meta={"http": {"status": resp.status}})
    stats.document(doc)
    return doc


def _deck(link: parse.Link, meeting_url: str, fetcher: Fetcher, stats: RunStats) -> DocumentRow:
    """A posted deck: the stored copy when there is one, else downloaded once (raw first)."""
    doc = documents.latest_document(SOURCE_ID, link.url)
    if doc is None or parse.is_challenge(doc.read()):
        sources.require_collectable(SOURCE_ID)
        kind = link.url.rsplit(".", 1)[1].lower()
        try:
            doc = http.fetch_document(SOURCE_ID, link.url, doc_type=f"llwg_report_{kind}", title=link.text or None,
                                      doc_date=parse.posted_date(link.url), meta={"meeting": meeting_url},
                                      fetcher=fetcher)
        except FetchError as exc:
            if exc.status in (401, 403):
                raise _challenge(link.url, exc.status) from None
            raise
        if parse.is_challenge(doc.read()):
            raise _challenge(link.url, 200)
    stats.document(doc)
    return doc


def load(doc: DocumentRow, report: parse.StatusReport, stats: RunStats) -> int:
    """Write the report's stated figures as series observations. Returns facts newly written."""
    new = 0
    with db.transaction() as conn:
        facts = FactWriter(conn)
        for fig in report.figures:
            res = facts.add(
                "series", fig.series_id, "value", fig.value, tag="F", source_name=SOURCE_NAME, source_url=doc.url,
                valid_from=report.as_of, producer=PRODUCER, status="published", unit="MW", document_id=doc.id,
                quote=fig.quote, quote_loc=fig.loc,
            )
            stats.facts_seen += 1
            if res.created:
                new += 1
                stats.facts_new += 1
                stats.new_by_series[fig.series_id] = stats.new_by_series.get(fig.series_id, 0) + 1
            newest = stats.latest.get(fig.series_id)
            if newest is None or newest[0] < report.as_of.isoformat():
                stats.latest[fig.series_id] = [report.as_of.isoformat(), fig.value]
    if doc.extract_state == "none":  # the charts are pictures: queued for the extraction workers
        documents.set_extract_state(doc.id, "queued")
    return new


def _robots(fetcher: Fetcher, stats: RunStats) -> RobotFileParser:
    doc = _page(ROBOTS_URL, "robots_txt", "www.ercot.com robots.txt", fetcher, stats, lambda b: b)
    robots = RobotFileParser(ROBOTS_URL)
    robots.parse(doc.read().decode("utf-8", errors="replace").splitlines())
    return robots


def collect_reports(stats: RunStats, fetcher: Fetcher, today: date) -> None:
    info: dict[str, Any] = {"per_project_rows": 0, "matched_subjects": 0, "events": 0, "robots_disallowed": [],
                            "not_status_update": [], "reports": []}
    stats.extra["ercot"] = info
    robots = _robots(fetcher, stats)

    def allowed(url: str) -> bool:
        if robots.can_fetch(ROBOTS_AGENT, url):
            return True
        info["robots_disallowed"].append(url)
        return False

    if not allowed(LLWG_URL):
        return
    llwg = _page(LLWG_URL, "llwg_html", "ERCOT Large Load Working Group", fetcher, stats,
                 lambda b: parse.meetings(b, LLWG_URL))
    listed = parse.meetings(llwg.read(), LLWG_URL)
    held = [(d, u) for d, u in listed if d <= today]
    info.update(meetings_listed=len(listed), meeting_pages_read=0)
    candidates: dict[str, tuple[parse.Link, str]] = {}
    for day, url in held:
        stored = documents.latest_document(SOURCE_ID, url)
        if stored is None or day >= today - timedelta(days=RECENT_DAYS):
            if not allowed(url):
                continue
            stored = _page(url, "llwg_meeting_html", f"ERCOT LLWG meeting {day.isoformat()}", fetcher, stats,
                           lambda b, u=url: parse.report_links(b, u))
            info["meeting_pages_read"] += 1
        for link in parse.report_links(stored.read(), url):  # older pages: the stored copy, no request
            candidates.setdefault(link.url, (link, url))
    info["candidates"] = len(candidates)
    for link, meeting_url in sorted(candidates.values(), key=lambda c: (parse.posted_date(c[0].url), c[0].url)):
        if not allowed(link.url):
            continue
        doc = _deck(link, meeting_url, fetcher, stats)
        report = parse.parse_status_update(doc.read())
        if report is None:
            info["not_status_update"].append(link.url)
            continue
        new = load(doc, report, stats)
        info["reports"].append({
            "url": doc.url, "report_date": report.report_date.isoformat(), "as_of": report.as_of.isoformat(),
            "as_of_stated": report.as_of_stated, "figures": {f.series_id: f.value for f in report.figures},
            "facts_new": new,
        })


def run(*, fetcher: Fetcher | None = None, today: date | None = None) -> dict[str, Any]:
    return collect(SOURCE, lambda stats: collect_reports(stats, fetcher or default_fetcher(), today or today_et()))


@app.periodic(cron=CRON_UTC, periodic_id="weekly_mon")
@app.task(name="collect.ercot", queue="collect", queueing_lock="collect.ercot", lock="collect.ercot")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


@cli.action("collect", "ercot", help="ERCOT Large Load Interconnection Status Updates (LLWG decks): headline MW")
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run())
