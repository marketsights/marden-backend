"""FERC eLibrary's JSON API: the endpoints the eLibrary web app (elibrary.ferc.gov/eLibrary) calls.

- Search: POST https://elibrary.ferc.gov/eLibraryWebAPI/api/Search/AdvancedSearch (the web app's
  Advanced Search; a docket filter with every sub-docket and a posted-date window).
- File: POST https://elibrary.ferc.gov/eLibraryWebAPI/api/File/DownloadP8File with the file id (what
  the public link https://elibrary.ferc.gov/eLibrary/filedownload?fileid=<id> does in the browser).

Both are POST-only (the GET docket-sheet routes answer 404), so this module does what
`http.fetch_document` does for GETs: shared per-host rate limit, the declared Marden User-Agent,
retries on 5xx and timeouts, then the bytes go raw to `documents.record_document` before any parse,
with the request body in the document's meta. Bot protection is never evaded: a Cloudflare challenge,
401/403/407/451 or a 429 that persists after one polite wait raises `Challenged` and the run stops.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx

from marden.collectors.ferc_parse import FileRef, Filing
from marden.collectors.market_http import Challenged
from marden.core import documents, http, sources
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, FetchError, FetchResponse, TokenBucket

HOST = "elibrary.ferc.gov"
API = f"https://{HOST}/eLibraryWebAPI/api"
SEARCH_URL = f"{API}/Search/AdvancedSearch"
DOWNLOAD_URL = f"{API}/File/DownloadP8File"
PER_SECOND = 0.5  # one request every 2 s
PAGE_SIZE = 100
ATTEMPTS = 3
MAX_RETRY_AFTER = 120.0
STOP_STATUSES = frozenset({401, 403, 407, 451})
ACCEPT_JSON = "application/json, text/plain, */*"
_INTERSTITIAL = re.compile(
    rb"<title>\s*(?:just a moment|attention required|access denied)|cf-chl-|cf_chl_opt|captcha", re.I
)

http.set_rate_limit(HOST, PER_SECOND)


def interstitial(resp: FetchResponse) -> bool:
    """A Cloudflare challenge or block page instead of the answer (not the Cloudflare beacon script that
    eLibrary's own error pages carry)."""
    if resp.headers.get("cf-mitigated"):
        return True
    if "html" not in (resp.content_type or "").lower():
        return False
    return bool(_INTERSTITIAL.search(resp.content[:20000]))


def search_body(docket: str, since: date, until: date, page: int) -> dict[str, Any]:
    """The web app's AdvancedSearch request (its own defaults), for one docket and posted-date window."""
    return {
        "searchText": "*", "searchFullText": True, "searchDescription": True,
        "dateSearches": [{"dateType": "posted_date", "startDate": since.isoformat(), "endDate": until.isoformat()}],
        "availability": [], "affiliations": [], "categories": [], "libraries": [], "accessionNumber": None,
        "eFiling": False, "opinion": None, "fedRegisterCite": None, "fedCourtCaseNumber": None, "fercCite": None,
        "parentAccessionNumber": None, "docketSearches": [{"docketNumber": docket, "subDocketNumbers": []}],
        "resultsPerPage": PAGE_SIZE, "curPage": page, "classTypes": [], "orderNumber": None, "sortBy": "",
        "groupBy": "NONE", "idolResultID": "", "allDates": False,
    }


def download_body(file_id: str) -> dict[str, Any]:
    return {"FileType": "", "accession": "", "fileid": 0, "FileIDAll": "", "fileidLst": [file_id], "Islegacy": False}


def search_url(docket: str, since: date, until: date, page: int) -> str:
    """The document identity of one search page: the endpoint plus a fragment naming the request
    (fragments are never sent; the exact POST body is in the document's meta)."""
    return f"{SEARCH_URL}#docket={docket}&posted={since.isoformat()}..{until.isoformat()}&page={page}"


class ElibraryClient:
    """POSTs to the eLibrary API for one source, storing every response raw. Counts requests."""

    def __init__(
        self,
        source_id: str,
        *,
        fetcher: Fetcher | None = None,
        bucket: TokenBucket | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.source_id = source_id
        self.fetcher = fetcher or http.default_fetcher()
        self.bucket = bucket or http.bucket_for(HOST)[1]
        self.sleep = sleep
        self.requests = 0

    def _headers(self, accept: str) -> dict[str, str]:
        return {"User-Agent": http.user_agent_for(HOST, self.fetcher.settings), "Accept": accept,
                "Content-Type": "application/json"}

    def post(self, url: str, body: dict[str, Any], *, expect_json: bool) -> FetchResponse:
        """POST with retries; returns a 2xx response that is data, or raises FetchError / Challenged."""
        headers = self._headers(ACCEPT_JSON if expect_json else "*/*")
        waited_429 = False
        attempt = 0
        while True:
            self.bucket.acquire()
            self.requests += 1
            try:
                raw = self.fetcher.client.post(url, json=body, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt + 1 >= ATTEMPTS:
                    raise FetchError(url, None, f"{type(exc).__name__}: {exc}") from exc
                self.sleep(2.0 * 2**attempt)
                attempt += 1
                continue
            resp = FetchResponse(url=url, final_url=str(raw.url), status=raw.status_code, content=raw.content,
                                 headers={k.lower(): v for k, v in raw.headers.items()})
            if resp.status in STOP_STATUSES:
                raise Challenged(url, resp.status, f"stopped: HTTP {resp.status} (no evasion attempted)")
            if resp.status == 429:
                wait = http.retry_after_seconds(resp.headers.get("retry-after"))
                if waited_429 or (wait is not None and wait > MAX_RETRY_AFTER):
                    raise Challenged(url, 429, "stopped: HTTP 429 persists (no evasion attempted)")
                waited_429 = True
                self.sleep(wait if wait is not None else 30.0)
                continue
            if resp.status in http.RETRY_STATUSES and attempt + 1 < ATTEMPTS:
                self.sleep(2.0 * 2**attempt)
                attempt += 1
                continue
            if not 200 <= resp.status < 300:
                if interstitial(resp):
                    raise Challenged(url, resp.status, "stopped: bot-check page (no evasion attempted)")
                raise FetchError(url, resp.status, f"HTTP {resp.status}: {resp.content[:200]!r}")
            if interstitial(resp):
                raise Challenged(url, resp.status, "stopped: bot-check page (no evasion attempted)")
            ctype = (resp.content_type or "").lower()
            if expect_json and ("json" not in ctype or not resp.content.strip()):
                # HTML or an empty body where JSON belongs: retried, then an error (never parsed as data)
                if attempt + 1 < ATTEMPTS:
                    self.sleep(2.0 * 2**attempt)
                    attempt += 1
                    continue
                raise FetchError(url, resp.status, f"not JSON ({ctype or 'no type'}, {len(resp.content)} bytes)")
            return resp

    def search(self, docket: str, since: date, until: date, page: int) -> DocumentRow:
        """One AdvancedSearch page for a docket, stored raw (doc_type 'ferc_search')."""
        sources.require_collectable(self.source_id)
        body = search_body(docket, since, until, page)
        resp = self.post(SEARCH_URL, body, expect_json=True)
        return documents.record_document(
            self.source_id, search_url(docket, since, until, page), resp.content, content_type=resp.content_type,
            doc_type="ferc_search", title=f"eLibrary filings in {docket} posted {since}..{until} (page {page + 1})",
            doc_date=until, external_id=docket,
            meta={"docket": docket, "posted_from": since.isoformat(), "posted_to": until.isoformat(), "page": page,
                  "http": {"status": resp.status, "method": "POST", "endpoint": SEARCH_URL, "body": body}},
        )

    def download(self, filing: Filing, file: FileRef, *, meta: dict[str, Any], extract_state: str) -> DocumentRow:
        """A filing's main file, stored raw under its public eLibrary link (doc_type ferc_issuance/ferc_submittal)."""
        sources.require_collectable(self.source_id)
        body = download_body(file.file_id)
        resp = self.post(DOWNLOAD_URL, body, expect_json=False)
        if file.size is not None and len(resp.content) != file.size:
            raise FetchError(file.url, resp.status, f"got {len(resp.content)} bytes, eLibrary lists {file.size}")
        http_meta = {"status": resp.status, "method": "POST", "endpoint": DOWNLOAD_URL, "body": body}
        if resp.headers.get("content-disposition"):
            http_meta["content_disposition"] = resp.headers["content-disposition"]
        return documents.record_document(
            self.source_id, file.url, resp.content, content_type=_content_type(file, resp),
            doc_type=filing.doc_type, title=filing.title, doc_date=filing.day, external_id=filing.accession,
            meta={**meta, "main_file": file.as_meta(), "http": http_meta}, extract_state=extract_state,
        )


_TYPES = {"PDF": "application/pdf", "TXT": "text/plain",
          "DOCX": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          "DOC": "application/msword", "XLSX": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          "XLS": "application/vnd.ms-excel", "HTML": "text/html", "XML": "application/xml"}


def _content_type(file: FileRef, resp: FetchResponse) -> str | None:
    """eLibrary sends every file as application/octet-stream; the listed file type names it."""
    sent = (resp.content_type or "").split(";")[0].strip().lower()
    if sent and sent != "application/octet-stream":
        return resp.content_type
    return _TYPES.get(file.file_type.upper(), resp.content_type)
