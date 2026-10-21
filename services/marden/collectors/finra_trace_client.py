"""The finra.org bond page's own request flow, made politely and stopped at the first refusal.

What a browser does on https://www.finra.org/finra-data/fixed-income/bond?cusip=<CUSIP>&bondType=CORP
(read from the page's app bundle, ddwa-cdn-us-east-1.ddwa.finra.org/app-dynamic-reporting/app-dynamic-reporting.js):

1. GET the page (an HTML shell);
2. GET the bond template  .../public/reporting/v2/template/template-e07aeeca-.../composite -> dataset + fields;
3. POST .../public/reporting/v2/data/group/<group>/name/<dataset>: the security master and last sale;
4. GET the price/yield template .../template-b3232ls-.../composite, then POST its dataset: the
   end-of-day price/yield history (the page asks for five years; later runs ask for the last 14 days).

This client sends exactly those requests with the cookies and the X-XSRF-TOKEN header the site hands out,
the Origin/Referer of the page, and Marden's own User-Agent (`Marden research cody@marden.ai`), at most one
request per second per host plus 0.2-0.8 s of jitter. Templates are fetched once per run, not per bond.

Every JSON answer is stored raw (`document`) before it is parsed. A Cloudflare challenge, a 401/403/407/429,
a login redirect or an HTML page where the bond page gets JSON is a refusal: the response is stored as a
`refusal` document and AccessRefused is raised, which ends the run (recorded on the source). There is no
retry around a refusal: no proxies, no headless browser, no other User-Agent, no rotated identity.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx

from marden.collectors.finra_trace_parse import bond_page_url, refusal_reason
from marden.core import documents, http
from marden.core.config import get_settings
from marden.core.documents import DocumentRow
from marden.core.http import FetchError

API = "https://services-dynarep.ddwa.finra.org/"
BOND_TEMPLATE = API + "public/reporting/v2/template/template-e07aeeca-d6b8-4356-bd2a-0ca58e1e5bea/composite"
PRICE_YIELD_TEMPLATE = API + "public/reporting/v2/template/template-b3232ls-8h326-4355-8240-2ks8ka01kz0/composite"
ORIGIN = "https://www.finra.org"
RATE_PER_SECOND = 1.0
JITTER_SECONDS = (0.2, 0.8)
SERVER_RETRIES = 2  # 500/502/503/504 and timeouts only; never a refusal
TRANSIENT = frozenset({500, 502, 503, 504})

http.set_rate_limit("finra.org", RATE_PER_SECOND)  # www, services-dynarep.ddwa and the CDN share it


class AccessRefused(FetchError):
    """finra.org did not serve the request (challenge, 403/429, login wall, HTML for JSON)."""


def data_url(group: str, name: str) -> str:
    return f"{API}public/reporting/v2/data/group/{group}/name/{name}"


def cusip_filter(cusip: str) -> list[dict[str, Any]]:
    return [{"compareFilters": [{"fieldName": "cusip", "fieldValue": cusip, "compareType": "EQUAL"},
                                {"fieldName": "issueSymbolIdentifier", "fieldValue": cusip, "compareType": "EQUAL"}]}]


def bond_body(cusip: str, fields: list[str]) -> dict[str, Any]:
    return {"fields": fields, "orFilters": cusip_filter(cusip), "offset": 0, "limit": 1}


def price_yield_body(cusip: str, start: str, end: str) -> dict[str, Any]:
    """The page's history request; start/end are 'YYYY-MM-DD HH:MM:SS.000 ' (UTC) as the page sends them."""
    return {"fields": ["cusip", "productType", "lastSalePrice", "lastSaleYield", "tradeDate"],
            "orFilters": cusip_filter(cusip),
            "dateRangeFilters": [{"startDate": start, "endDate": end, "fieldName": "tradeDate"}],
            "sortFields": ["tradeDate"], "offset": 0, "limit": 5000}


class BondPageClient:
    def __init__(self, source_id: str, *, client: httpx.Client | None = None, user_agent: str | None = None,
                 sleep: Callable[[float], None] = time.sleep, jitter: tuple[float, float] = JITTER_SECONDS):
        self.source_id = source_id
        self.user_agent = user_agent or get_settings().sec_user_agent  # "Marden research cody@marden.ai"
        self.client = client or httpx.Client(timeout=httpx.Timeout(45.0, connect=15.0), follow_redirects=True)
        self.sleep = sleep
        self.jitter = jitter
        self.referer = ORIGIN + "/"
        self.requests = 0

    def close(self) -> None:
        self.client.close()

    # ------------------------------------------------------------ requests
    def _xsrf(self) -> str | None:
        for cookie in self.client.cookies.jar:
            if cookie.name == "XSRF-TOKEN" and cookie.domain.lstrip(".").endswith("finra.org"):
                return cookie.value
        return None

    def _send(self, method: str, url: str, *, body: dict[str, Any] | None, api: bool) -> httpx.Response:
        headers = {"User-Agent": self.user_agent}
        if api:
            headers.update({"Accept": "application/json, text/plain, */*", "Origin": ORIGIN, "Referer": self.referer})
            if body is not None:
                headers["Content-Type"] = "application/json"
            token = self._xsrf()
            if token:
                headers["X-XSRF-TOKEN"] = token
        else:
            headers["Accept"] = "text/html,application/xhtml+xml"
        _, bucket = http.bucket_for(urlparse(url).hostname or "")
        attempt = 0
        while True:
            bucket.acquire()
            self.sleep(random.uniform(*self.jitter))
            self.requests += 1
            try:
                resp = self.client.request(method, url, headers=headers,
                                           content=json.dumps(body).encode() if body is not None else None)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= SERVER_RETRIES:
                    raise FetchError(url, None, f"{type(exc).__name__}: {exc}") from exc
                attempt += 1
                self.sleep(5.0 * attempt)
                continue
            if resp.status_code in TRANSIENT and attempt < SERVER_RETRIES:
                attempt += 1
                self.sleep(5.0 * attempt)
                continue
            return resp

    def _check(self, resp: httpx.Response, doc_url: str, *, expect_json: bool, meta: dict[str, Any]) -> None:
        headers = {k.lower(): v for k, v in resp.headers.items()}
        reason = refusal_reason(resp.status_code, headers, resp.content, expect_json=expect_json,
                                final_url=str(resp.url))
        if reason is not None:
            kept = {k: headers[k] for k in ("content-type", "cf-mitigated", "cf-ray", "retry-after", "server")
                    if k in headers}
            documents.record_document(
                self.source_id, doc_url, resp.content, content_type=headers.get("content-type"),
                doc_type="refusal", title=f"refused: {reason}"[:200],
                meta={**meta, "http": {"status": resp.status_code, "headers": kept, "final_url": str(resp.url)}},
            )
            raise AccessRefused(doc_url, resp.status_code, reason)
        if not 200 <= resp.status_code < 300:
            raise FetchError(doc_url, resp.status_code, f"HTTP {resp.status_code}: {resp.content[:200]!r}")

    def page(self, cusip: str) -> int:
        """GET the bond page itself (as a browser does first). Returns its size; nothing is parsed from it."""
        url = bond_page_url(cusip)
        resp = self._send("GET", url, body=None, api=False)
        self._check(resp, url, expect_json=False, meta={"step": "bond page"})
        self.referer = url
        return len(resp.content)

    def get_json(self, url: str, *, doc_type: str, title: str) -> DocumentRow:
        resp = self._send("GET", url, body=None, api=True)
        self._check(resp, url, expect_json=True, meta={"step": doc_type})
        return self._store(url, resp, doc_type=doc_type, title=title, meta={"method": "GET"})

    def post_json(self, url: str, body: dict[str, Any], *, cusip: str, doc_type: str, title: str) -> DocumentRow:
        """POST a dataset query. The document URL is the endpoint plus '#cusip=<CUSIP>' (a fragment is never
        sent; it keeps each bond's answers apart), and meta holds the exact request body."""
        doc_url = f"{url}#cusip={cusip}"
        self.referer = bond_page_url(cusip)
        resp = self._send("POST", url, body=body, api=True)
        meta = {"method": "POST", "endpoint": url, "body": body, "cusip": cusip}
        self._check(resp, doc_url, expect_json=True, meta={"step": doc_type, **meta})
        return self._store(doc_url, resp, doc_type=doc_type, title=title, meta=meta, external_id=cusip)

    def _store(self, url: str, resp: httpx.Response, *, doc_type: str, title: str, meta: dict[str, Any],
               external_id: str | None = None) -> DocumentRow:
        http_meta: dict[str, Any] = {"status": resp.status_code}
        if resp.headers.get("record-total"):
            http_meta["record_total"] = resp.headers["record-total"]
        return documents.record_document(
            self.source_id, url, resp.content, content_type=resp.headers.get("content-type"), doc_type=doc_type,
            title=title, external_id=external_id, meta={**meta, "http": http_meta},
        )
