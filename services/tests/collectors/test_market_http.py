"""The owner's limits for the unlicensed market sources, checked on the code paths the collectors use:
honest User-Agent, at most 1 request/second per host plus jitter, and a full stop (no retry with other
headers) at bot protection: Cloudflare challenges (the real page Cloudflare served, market_http/), 401/403,
consent or login redirects and a 429 that survives one polite retry."""

from __future__ import annotations

import time
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from marden.collectors import boerse_frankfurt as FWB
from marden.collectors import finra_trace_client, market_http
from marden.collectors import yahoo_equity as Y
from marden.collectors.market_http import Challenged, MarketFetcher
from marden.core import http

CHALLENGE = (Path(__file__).parent / "data" / "market_http" / "cloudflare_challenge.html").read_bytes()
UA = "Marden research cody@marden.ai"
BROWSER_HINTS = ("mozilla", "chrome", "safari", "applewebkit", "gecko")


def _fetcher(handler, **kw) -> MarketFetcher:
    return MarketFetcher(client=httpx.Client(transport=httpx.MockTransport(handler)), jitter=(0, 0),
                         backoff_base=0.01, **kw)


def test_each_source_host_is_capped_at_one_request_per_second():
    assert finra_trace_client.RATE_PER_SECOND == 1.0
    for host in (Y.HOST, FWB.HOST, "www.finra.org", "services-dynarep.ddwa.finra.org"):
        _, bucket = http.bucket_for(host)
        assert bucket.rate == 1.0 and bucket.capacity == 1.0, host
    assert http.bucket_for("www.finra.org")[1] is http.bucket_for("services-dynarep.ddwa.finra.org")[1]


def test_requests_identify_honestly_and_are_spaced(monkeypatch):
    seen: list[tuple[float, httpx.Headers]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((time.monotonic(), request.headers))
        return httpx.Response(200, json={"ok": True})

    sleeps: list[float] = []
    monkeypatch.setattr(market_http, "time", SimpleNamespace(sleep=sleeps.append))  # jitter only, not the bucket
    fetcher = MarketFetcher(client=httpx.Client(transport=httpx.MockTransport(handler)), backoff_base=0.01)
    for _ in range(3):
        fetcher.get(f"https://{FWB.HOST}/v1/data/price_information/single?isin=US68389XBX21&mic=XFRA")
    assert len(sleeps) == 3 and all(0.2 <= s <= 0.8 for s in sleeps)  # random jitter before every request
    gaps = [b[0] - a[0] for a, b in pairwise(seen)]
    assert all(g >= 0.95 for g in gaps), gaps  # never faster than 1 request/second on the host
    for _, headers in seen:
        assert headers["user-agent"] == UA
        assert not any(h in headers["user-agent"].lower() for h in BROWSER_HINTS)
        assert not {"sec-ch-ua", "sec-fetch-mode", "cookie"} & set(headers)


@pytest.mark.parametrize(
    ("status", "headers", "body", "requests"),
    [
        (403, {"cf-mitigated": "challenge", "content-type": "text/html"}, CHALLENGE, 1),
        (200, {"cf-mitigated": "challenge", "content-type": "text/html"}, CHALLENGE, 1),
        (200, {"content-type": "text/html; charset=UTF-8"}, CHALLENGE, 1),
        (401, {"content-type": "application/json"}, b'{"error": "login required"}', 1),
        (403, {"content-type": "text/html"}, b"<html>Access denied</html>", 1),
        (429, {"content-type": "text/html"}, b"Too Many Requests", 2),  # one polite retry, then stop
    ],
)
def test_bot_protection_stops_without_a_workaround(status, headers, body, requests):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, content=body, headers=headers)

    with pytest.raises(Challenged, match="no evasion attempted"):
        _fetcher(handler).get(Y.chart_url("ORCL", "1mo"))
    assert len(calls) == requests
    assert {c.headers["user-agent"] for c in calls} == {UA}  # the retry is the same request, same identity


def test_a_consent_or_login_redirect_stops():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == Y.HOST:
            return httpx.Response(302, headers={"location": "https://consent.yahoo.com/v2/collectConsent?x=1"})
        return httpx.Response(200, content=b"<html>consent</html>", headers={"content-type": "text/html"})

    fetcher = MarketFetcher(client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
                            jitter=(0, 0))  # the production client follows redirects, as a browser does
    with pytest.raises(Challenged, match=r"consent\.yahoo\.com"):
        fetcher.get(Y.chart_url("ORCL", "1mo"))
    assert len(calls) == 2  # the request and the redirect a client follows; nothing is submitted there
