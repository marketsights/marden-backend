"""ERCOT generation interconnection status (GIS Report, EMIL PG7-200-ER, MIS report type 15933) -> queue_project
facts (tag F, source ERCOT). See iso_queue for the model and ercot_gis_parse for the workbook and the listing.
Large-load requests are not in this report: ERCOT publishes them only as aggregate status decks on ordinary
www.ercot.com pages (Nodal Protocols 3.2.7, NPRR1267: "to the ERCOT website"), read by the `ercot` collector.

Where it is. ERCOT posts the GIS Report monthly only on its MIS: the list at
https://www.ercot.com/misapp/GetReports.do?reportTypeId=15933 (GIS_Report_<Month><Year>.xlsx, each with a
doclookupId) and the file at https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId=<id>. It is not
among the 120 products of the ERCOT Public API catalog, nor on data.ercot.com. www.ercot.com/robots.txt
disallows /misapp and /misdownload for every user agent; Marden's business owner decided to collect these two
paths anyway at low volume, under the rules below (both recorded in TERMS_NOTE).

Rules. User-Agent "Marden research cody@marden.ai"; one request per 5 s to www.ercot.com (the host-wide limit the
`ercot` collector also uses: ERCOT's Imperva answered 403 to bursts near 1 a second) and no retries; per run one
listing request plus one download per report not yet in the document table (a stored report is never fetched
again); a 401/403/429 answer or a challenge/CAPTCHA page stops the run, is recorded on the source and is never
worked around.

Each run reads the listing (stored raw when its postings changed: the page embeds a session id), takes the
newest BACKFILL (12) GIS reports by report month and loads, oldest first, those posted after the newest report
already loaded: the first run backfills twelve months, later runs load each new month once. A report older than
the newest one loaded is not loaded out of order (queue history only moves forward). valid_from is the posting
date. The Co-located Battery Identification reports on the same listing are not read.

Cadence: ERCOT posts in the first days of each month; checked weekly (Tuesday 13:17 UTC).
`python -m marden collect ercot_gis`.
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from typing import Any

import httpx

from marden import cli
from marden.collectors import ercot_gis_parse as parse
from marden.collectors.ercot_gis_parse import Posting, parse_workbook
from marden.collectors.ercot_parse import is_challenge
from marden.collectors.iso_queue import QueueStore
from marden.collectors.rates_common import ParseError, RunStats, collect, print_stats
from marden.core import db, documents, http, sources
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, FetchError, FetchResponse
from marden.core.queue import app
from marden.core.sources import declare_source

SOURCE_ID = "ercot_gis"
SOURCE_NAME = "ERCOT"
PRODUCER = "collector:ercot_gis"
HOST = "www.ercot.com"
REPORT_TYPE_ID = 15933
LIST_URL = f"https://{HOST}/misapp/GetReports.do?reportTypeId={REPORT_TYPE_ID}"
DOWNLOAD_URL = f"https://{HOST}/misdownload/servlets/mirDownload?doclookupId={{}}"
PRODUCT_URL = "https://www.ercot.com/mp/data-products/data-product-details?id=PG7-200-ER"
USER_AGENT = "Marden research cody@marden.ai"
PER_SECOND = 0.2  # one request per 5 s (the owner's ceiling is 1 a second)
STOP_STATUSES = frozenset({401, 403, 429})
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CRON_UTC = "17 13 * * 2"  # Tuesdays 13:17 UTC
BACKFILL = 12  # monthly reports kept loaded: the newest twelve
MIN_ENTRIES = 500  # the GIS report lists well over a thousand requests; fewer means a truncated file
ROBOTS_LINES = "User-agent: * ... Disallow: /misapp ... Disallow: /misdownload"
TERMS_NOTE = (
    f"Checked 2026-10-21. www.ercot.com/robots.txt: \"{ROBOTS_LINES}\". The GIS Report (EMIL PG7-200-ER, MIS "
    "report type 15933) is listed only at /misapp/GetReports.do?reportTypeId=15933 and downloaded only from "
    "/misdownload/servlets/mirDownload; it is not among the 120 products of the ERCOT Public API catalog nor on "
    "data.ercot.com. Owner's decision (Marden's business owner, 2026-10-21): Marden collects these two paths "
    "anyway, at low volume, despite robots.txt, with an honest User-Agent \"Marden research cody@marden.ai\", at "
    "most 1 request a second (one per 5 s here), a weekly check, downloading only reports not already in the "
    "document table, and stopping on any challenge, 403 or CAPTCHA, which is never evaded. ERCOT Terms of Use "
    "(https://www.ercot.com/help/terms, 07/20/2023): \"raw data provided in public portions of this website may "
    "be used, reproduced, and redistributed in compilations, charts, and analyses without maintaining such "
    "notices.\" and \"Use of this website in a manner that negatively affects the performance of this website or "
    "other ERCOT systems is prohibited.\" No credential is needed."
)
SOURCE = declare_source(SOURCE_ID, "ERCOT GIS Report (MIS)", "portal", LIST_URL, 7 * 24 * 60, TERMS_NOTE)
http.set_rate_limit(HOST, PER_SECOND)
_CAPTCHA = re.compile(rb"captcha|Request unsuccessful\. Incapsula", re.I)


class Stopped(FetchError):
    """ERCOT refused or challenged a request: the run stops and nothing is retried around it."""


def challenged(body: bytes) -> bool:
    """An Imperva challenge or a CAPTCHA page instead of the content asked for."""
    return is_challenge(body) or _CAPTCHA.search(body[:65536]) is not None


def _get(url: str, fetcher: Fetcher, info: dict[str, Any]) -> FetchResponse:
    sources.require_collectable(SOURCE_ID)
    info["requests"] += 1
    try:
        resp = fetcher.get(url, headers={"User-Agent": USER_AGENT}, use_cache=False)
    except FetchError as exc:
        if exc.status in STOP_STATUSES:
            raise Stopped(url, exc.status, f"ERCOT answered HTTP {exc.status}; run stopped, not retried") from None
        raise
    return resp


def read_listing(fetcher: Fetcher, stats: RunStats, info: dict[str, Any]) -> list[Posting]:
    """The MIS listing's postings; the page is stored raw unless its postings are unchanged since the last copy."""
    resp = _get(LIST_URL, fetcher, info)
    if challenged(resp.content):
        raise Stopped(LIST_URL, resp.status, "ERCOT answered the MIS listing with a challenge page; run stopped")
    postings = parse.mis_listing(resp.content)
    if not any(p.is_gis for p in postings):
        raise ParseError(f"no GIS_Report posting on the MIS listing ({len(postings)} postings)")
    prior = documents.latest_document(SOURCE_ID, LIST_URL)
    if prior is not None and parse.mis_listing(prior.read()) == postings:
        stats.document(prior)
        return postings
    doc = documents.record_document(SOURCE_ID, LIST_URL, resp.content, content_type=resp.content_type,
                                    doc_type="mis_listing_html", title="ERCOT MIS GIS Report listing (15933)",
                                    meta={"http": {"status": resp.status}, "postings": len(postings)})
    stats.document(doc)
    return postings


def download(p: Posting, fetcher: Fetcher, info: dict[str, Any]) -> DocumentRow:
    """The stored copy of a report, else the report downloaded once and stored raw (only a real xlsx is kept)."""
    url = DOWNLOAD_URL.format(p.doc_id)
    doc = documents.latest_document(SOURCE_ID, url)
    if doc is not None:
        return doc
    resp = _get(url, fetcher, info)
    if not resp.content.startswith(b"PK\x03\x04"):
        if challenged(resp.content):
            raise Stopped(url, resp.status, "ERCOT answered a report download with a challenge page; run stopped")
        raise ParseError(f"ERCOT MIS answered {url} with {resp.content_type or 'untyped'} content, not an xlsx")
    info["downloaded"].append(p.name)
    month = p.month
    return documents.record_document(
        SOURCE_ID, url, resp.content, content_type=resp.content_type or XLSX, doc_type="gis_xlsx", title=p.file,
        doc_date=p.posted, external_id=p.doc_id,
        meta={"report_month": month.isoformat() if month else None, "posted_at": p.posted_at.isoformat(),
              "report_type_id": p.report_type, "http": {"status": resp.status}},
    )


def loaded_reports() -> dict[str, datetime]:
    """doclookupId -> posting time of every GIS report facts were written from."""
    with db.transaction() as conn:
        rows = conn.execute(
            """SELECT d.external_id, d.meta->>'posted_at' AS posted_at, d.doc_date FROM document d
                WHERE d.source_id = %s AND d.doc_type = 'gis_xlsx' AND d.external_id IS NOT NULL
                  AND EXISTS (SELECT 1 FROM fact f WHERE f.document_id = d.id)""", (SOURCE_ID,)).fetchall()
    return {r["external_id"]: (datetime.fromisoformat(r["posted_at"]) if r["posted_at"]
                               else datetime.combine(r["doc_date"], datetime.min.time())) for r in rows}


def plan(postings: list[Posting], loaded: dict[str, datetime]) -> tuple[list[Posting], list[Posting]]:
    """(reports to load, oldest first; window reports skipped because a newer one is already loaded)."""
    gis = sorted((p for p in postings if p.is_gis), key=lambda p: (p.month or p.posted, p.posted_at, p.doc_id))
    window = [p for p in gis[-BACKFILL:] if p.doc_id not in loaded]
    through = max(loaded.values(), default=None)
    todo = [p for p in window if through is None or p.posted_at > through]
    return todo, [p for p in window if p not in todo]


def load(doc: DocumentRow, posting: Posting, stats: RunStats, *, min_entries: int | None = None) -> dict[str, Any]:
    entries = parse_workbook(doc.read())
    if len(entries) < (MIN_ENTRIES if min_entries is None else min_entries):
        raise ParseError(f"ERCOT GIS report looks truncated: {len(entries)} entries")
    facts_before = stats.facts_new
    with db.transaction() as conn:
        store = QueueStore(conn, "ercot", SOURCE_NAME, PRODUCER, posting.posted, stats)
        for e in entries:
            store.write(e, source_url=doc.url, document_id=doc.id)
        store.finish(source_url=doc.url, document_id=doc.id)
    counts = stats.extra.get("iso", {}).get("ercot", {})
    return {"doc_id": posting.doc_id, "name": posting.name, "posted": posting.posted.isoformat(),
            "entries": len(entries), "entries_new": counts.get("entries_new", 0),
            "facts_new": stats.facts_new - facts_before, "events_new": counts.get("events_new", 0),
            "linked": counts.get("linked", {})}


def collect_reports(stats: RunStats, fetcher: Fetcher) -> None:
    info: dict[str, Any] = {"requests": 0, "downloaded": [], "loaded": [], "skipped_older": []}
    stats.extra["ercot_gis"] = info  # kept on the run even when it stops part way
    postings = read_listing(fetcher, stats, info)
    loaded = loaded_reports()
    todo, skipped = plan(postings, loaded)
    info.update(listed=len(postings), gis_listed=sum(p.is_gis for p in postings), already_loaded=len(loaded),
                skipped_older=[p.name for p in skipped])
    for p in todo:
        doc = download(p, fetcher, info)
        stats.document(doc)
        info["loaded"].append(load(doc, p, stats))


def make_fetcher(client: httpx.Client | None = None) -> Fetcher:
    """The fetcher a run uses: no retries, so a 429 or a server error ends the run like a refusal."""
    return Fetcher(client=client, max_retries=0)


def run(*, fetcher: Fetcher | None = None) -> dict[str, Any]:
    own = fetcher is None
    fetcher = fetcher or make_fetcher()
    try:
        return collect(SOURCE, lambda stats: collect_reports(stats, fetcher))
    finally:
        if own:
            fetcher.close()


@app.periodic(cron=CRON_UTC, periodic_id="weekly_tue")
@app.task(name="collect.ercot_gis", queue="collect", queueing_lock="collect.ercot_gis", lock="collect.ercot_gis")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


@cli.action("collect", "ercot_gis", help="ERCOT GIS Report (MIS): generation interconnection status, monthly")
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run())
