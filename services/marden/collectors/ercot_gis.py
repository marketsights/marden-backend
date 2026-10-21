"""ERCOT generation interconnection status (GIS Report, EMIL PG7-200-ER) -> queue_project facts (tag F,
source ERCOT). See iso_queue for the model and ercot_gis_parse for the workbook. Large-load requests are a
separate ERCOT report and a separate collector; this one reads generation requests only (INR numbers).

Access. ERCOT posts the GIS Report monthly on its MIS (report type 15933), but www.ercot.com/robots.txt
disallows /misapp and /misdownload for every user agent, and the product page
(https://www.ercot.com/mp/data-products/data-product-details?id=PG7-200-ER) lists and downloads documents
only through those paths. The automated route ERCOT offers is the Public API (api.ercot.com), which needs a
free account registered by a person on https://apiexplorer.ercot.com (bot-registered accounts are not
permitted), its subscription key, and a one-hour ID token from the account's sign-in. So:

- without ERCOT_API_KEY, ERCOT_API_USERNAME and ERCOT_API_PASSWORD the run records "needs key" on the
  source and makes no request (nothing is written);
- with them: sign in (ID token), list the PG7-200-ER archive (stored raw), pick the GIS_Report_* documents
  (not the Co-located Battery Identification reports filed under the same EMIL id), download each document
  once (ERCOT's API terms allow the same report at most three times in 12 months; a stored copy is reused),
  store it raw and load it. The first run loads only the newest report; later runs load the reports posted
  after the newest one loaded, oldest first. valid_from is the posting date.

Cadence: ERCOT posts the report in the first days of each month; checked weekly (Tuesday) so a new month
is picked up within a week. `python -m marden collect ercot_gis`.
"""

from __future__ import annotations

import argparse
import io
import json
import zipfile
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlparse

from marden import cli
from marden.collectors import ercot_api
from marden.collectors.ercot_api import API_HOST, TOKEN_HOST, NeedsKey, id_token  # noqa: F401  (shared auth)
from marden.collectors.ercot_gis_parse import parse_workbook, report_month
from marden.collectors.iso_queue import QueueStore
from marden.collectors.rates_common import ParseError, RunStats, collect, print_stats, today_et
from marden.core import db, documents, sources
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, default_fetcher
from marden.core.queue import app
from marden.core.sources import declare_source

SOURCE_ID = "ercot_gis"
SOURCE_NAME = "ERCOT"
PRODUCER = "collector:ercot_gis"
EMIL_ID = "pg7-200-er"
ARCHIVE_URL = f"https://{API_HOST}/api/public-reports/archive/{EMIL_ID}"
PRODUCT_URL = "https://www.ercot.com/mp/data-products/data-product-details?id=PG7-200-ER"
CRON_UTC = "17 13 * * 2"  # Tuesdays 13:17 UTC
LIST_DAYS = 120  # archive window listed each run (about four monthly reports)
MAX_NEW = 3  # reports loaded in one run (catch-up after missed months)
MIN_ENTRIES = 500  # the GIS report lists well over a thousand requests; fewer means a truncated file
TERMS_NOTE = (
    "Checked 2026-10-21. www.ercot.com/robots.txt: \"User-agent: * ... Disallow: /misapp ... Disallow: "
    "/misdownload\", the only paths that list and download the GIS Report on www.ercot.com, so it is not "
    "collected there. ERCOT Terms of Use (https://www.ercot.com/help/terms, 07/20/2023) allow reuse: \"raw data "
    "provided in public portions of this website may be used, reproduced, and redistributed in compilations, "
    "charts, and analyses without maintaining such notices.\" Terms of Use for the ERCOT API "
    "(https://www.ercot.com/help/terms/data-portal): \"Users must create an account to access the ERCOT API "
    "... Accounts registered by \"bots\" or other automated methods are not permitted\" and \"You may not "
    "download the same report from the Data Access Portal more than three times within a 12-month period\"; "
    "the API allows 30 requests a minute (developer.ercot.com, Known Limitations). Needs key: a person "
    "registers on apiexplorer.ercot.com and sets ERCOT_API_KEY, ERCOT_API_USERNAME and ERCOT_API_PASSWORD; "
    "until then each run records 'needs key' and makes no request. Each report is downloaded once."
)
SOURCE = declare_source(SOURCE_ID, "ERCOT", "api", f"https://{API_HOST}", 7 * 24 * 60, TERMS_NOTE)
http.set_rate_limit(API_HOST, 0.4)  # under ERCOT's 30 requests a minute
http.set_rate_limit(TOKEN_HOST, 0.2)


class NeedsKey(FetchError):
    """The ERCOT Public API credentials are not configured; no request is made."""


def _credentials() -> tuple[str, str, str]:
    s = get_settings()
    if not s.has_ercot_api():
        raise NeedsKey(ARCHIVE_URL, None, "needs key: set ERCOT_API_KEY, ERCOT_API_USERNAME and ERCOT_API_PASSWORD "
                       "(free ERCOT Public API account registered by a person on apiexplorer.ercot.com)")
    assert s.ercot_api_key and s.ercot_api_username and s.ercot_api_password
    return s.ercot_api_key, s.ercot_api_username, s.ercot_api_password


def id_token(fetcher: Fetcher, username: str, password: str) -> str:
    """A one-hour ID token (ERCOT's ROPC sign-in). The password goes in the form body only."""
    form = {"username": username, "password": password, "grant_type": "password",
            "scope": f"openid {CLIENT_ID} offline_access", "client_id": CLIENT_ID, "response_type": "id_token"}
    http.bucket_for(TOKEN_HOST)[1].acquire()
    try:
        resp = fetcher.client.post(TOKEN_URL, data=form, headers={"User-Agent": http.user_agent_for(TOKEN_HOST)})
    except httpx.HTTPError as exc:
        raise FetchError(TOKEN_URL, None, f"ERCOT sign-in failed: {type(exc).__name__}") from None
    if resp.status_code != 200:  # the body is not echoed: it can repeat what was sent
        raise FetchError(TOKEN_URL, resp.status_code, f"ERCOT sign-in refused (HTTP {resp.status_code})")
    try:
        token = resp.json()["id_token"]
    except (ValueError, KeyError, TypeError):
        raise FetchError(TOKEN_URL, resp.status_code, "ERCOT sign-in answered without an id_token") from None
    return str(token)


def _api_document(url: str, headers: dict[str, str], fetcher: Fetcher, *, meta: dict[str, Any] | None = None,
                  **doc: Any) -> DocumentRow:
    """GET an API URL with the credential headers and store the bytes (the headers are never stored)."""
    sources.require_collectable(SOURCE_ID)
    resp = fetcher.get(url, headers=headers, use_cache=False)
    return documents.record_document(SOURCE_ID, url, resp.content, content_type=resp.content_type,
                                     meta={**(meta or {}), "http": {"status": resp.status}}, **doc)


def gis_reports(listing: bytes) -> list[dict[str, Any]]:
    """GIS_Report_* documents in an archive listing, oldest first: {doc_id, name, posted, url}."""
    try:
        body = json.loads(listing)
        archives = body["archives"]
    except (ValueError, KeyError, TypeError):
        raise ParseError("ERCOT archive listing has no 'archives' list") from None
    out = []
    for a in archives if isinstance(archives, list) else []:
        name = str(a.get("friendlyName") or "")
        if not name.lower().startswith("gis_report") or a.get("docId") is None:
            continue
        try:
            posted = datetime.fromisoformat(str(a["postDatetime"])).date()
        except (KeyError, ValueError):
            raise ParseError(f"ERCOT archive entry {a.get('docId')} has no postDatetime") from None
        href = ((a.get("_links") or {}).get("endpoint") or {}).get("href")
        if not href or urlparse(href).hostname != API_HOST:
            href = f"{ARCHIVE_URL}?{urlencode({'download': a['docId']})}"
        out.append({"doc_id": str(a["docId"]), "name": name, "posted": posted, "url": href})
    return sorted(out, key=lambda r: (r["posted"], r["doc_id"]))


def _workbook_bytes(data: bytes) -> bytes:
    """The xlsx itself, also when the API wraps it in a zip with one .xlsx member."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            if "[Content_Types].xml" not in names:
                inner = [n for n in names if n.lower().endswith(".xlsx")]
                if len(inner) == 1:
                    return z.read(inner[0])
    except zipfile.BadZipFile:
        pass
    return data


def _loaded_through() -> date | None:
    """The posting date of the newest GIS report already loaded (facts written from it)."""
    with db.transaction() as conn:
        row = conn.execute(
            """SELECT max(d.doc_date) AS d FROM document d
                WHERE d.source_id = %s AND d.doc_type = 'gis_xlsx'
                  AND EXISTS (SELECT 1 FROM fact f WHERE f.document_id = d.id)""", (SOURCE_ID,)).fetchone()
    return row["d"] if row else None


def load(doc: DocumentRow, report_day: date, stats: RunStats, *, min_entries: int | None = None) -> None:
    entries = parse_workbook(_workbook_bytes(doc.read()))
    if len(entries) < (MIN_ENTRIES if min_entries is None else min_entries):
        raise ParseError(f"ERCOT GIS report looks truncated: {len(entries)} entries")
    with db.transaction() as conn:
        store = QueueStore(conn, "ercot", SOURCE_NAME, PRODUCER, report_day, stats)
        for e in entries:
            store.write(e, source_url=doc.url, document_id=doc.id)
        store.finish(source_url=doc.url, document_id=doc.id)


def collect_reports(stats: RunStats, fetcher: Fetcher) -> None:
    key, username, password = _credentials()
    headers = {"Ocp-Apim-Subscription-Key": key, "Authorization": f"Bearer {id_token(fetcher, username, password)}"}
    since = today_et() - timedelta(days=LIST_DAYS)
    listing_url = f"{ARCHIVE_URL}?{urlencode({'postDatetimeFrom': f'{since.isoformat()}T00:00:00', 'size': 100})}"
    listing = _api_document(listing_url, headers, fetcher, doc_type="archive_json",
                            title="ERCOT PG7-200-ER archive listing")
    stats.document(listing)
    reports = gis_reports(listing.read())
    if not reports:
        raise ParseError(f"no GIS_Report document in the PG7-200-ER archive since {since}")
    through = _loaded_through()
    todo = [r for r in reports if through is None or r["posted"] > through]
    todo = todo[-1:] if through is None else todo[:MAX_NEW]
    loaded = []
    for r in todo:
        doc = documents.latest_document(SOURCE_ID, r["url"])  # downloaded before: never fetched again
        if doc is None:
            doc = _api_document(r["url"], headers, fetcher, doc_type="gis_xlsx", title=r["name"],
                                doc_date=r["posted"], external_id=r["doc_id"],
                                meta={"report_month": (m.isoformat() if (m := report_month(r["name"])) else None)})
        stats.document(doc)
        load(doc, r["posted"], stats)
        loaded.append({"doc_id": r["doc_id"], "name": r["name"], "posted": r["posted"].isoformat()})
    stats.extra["ercot_gis"] = {"listed": len(reports), "loaded": loaded, "loaded_through_before": (
        through.isoformat() if through else None)}


def run(*, fetcher: Fetcher | None = None) -> dict[str, Any]:
    return collect(SOURCE, lambda stats: collect_reports(stats, fetcher or default_fetcher()))


@app.periodic(cron=CRON_UTC, periodic_id="weekly_tue")
@app.task(name="collect.ercot_gis", queue="collect", queueing_lock="collect.ercot_gis", lock="collect.ercot_gis")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


@cli.action("collect", "ercot_gis", help="ERCOT GIS Report: generation interconnection status (needs ERCOT API key)")
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run())
