"""Fetcher politeness and fetch_document, against a real local HTTP server and (marked) sec.gov."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from marden.core import http, sources
from marden.core.config import get_settings
from marden.core.http import Fetcher, FetchError, TokenBucket, fetch_document


class _Handler(BaseHTTPRequestHandler):
    counts: ClassVar[dict[str, int]] = {}

    def log_message(self, *args: object) -> None:
        pass

    def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        n = self.counts[self.path] = self.counts.get(self.path, 0) + 1
        if self.path == "/flaky":
            if n <= 2:
                self._send(503, b"busy", {"Retry-After": "0"})
            else:
                self._send(200, b"ok after retries", {"Content-Type": "text/plain"})
        elif self.path == "/etag":
            if self.headers.get("If-None-Match") == '"v1"':
                self._send(304, headers={"ETag": '"v1"'})
            else:
                self._send(200, b"versioned body", {"ETag": '"v1"', "Content-Type": "text/plain"})
        elif self.path == "/ua":
            self._send(200, (self.headers.get("User-Agent") or "").encode())
        elif self.path == "/throttled":
            self._send(429, b"slow down", {"Retry-After": "3600"})
        else:
            self._send(404, b"not here")


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    http.set_rate_limit("127.0.0.1", 200)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def fetcher() -> Iterator[Fetcher]:
    _Handler.counts.clear()
    with Fetcher(backoff_base=0.01, timeout=10) as f:
        yield f


def test_token_bucket_spaces_requests():
    bucket = TokenBucket(20.0)
    start = time.monotonic()
    for _ in range(5):
        bucket.acquire()
    assert time.monotonic() - start >= 0.19  # first is free, then 4 x 50 ms


def test_sec_hosts_share_one_capped_bucket():
    key_www, b1 = http.bucket_for("www.sec.gov")
    key_efts, b2 = http.bucket_for("efts.sec.gov")
    assert key_www == key_efts == "sec.gov" and b1 is b2 and b1.rate <= 5  # price-sources.md section 5
    assert http.bucket_for("data.sec.gov")[1] is b1
    assert http.bucket_for("api.eia.gov")[1].rate == http.DEFAULT_PER_SECOND
    with pytest.raises(ValueError):
        http.set_rate_limit("sec.gov", 10)
    with pytest.raises(ValueError):
        http.set_rate_limit("data.sec.gov", 6)


def test_user_agents():
    settings = get_settings()
    assert http.user_agent_for("www.sec.gov") == settings.sec_user_agent
    other = http.user_agent_for("www.ercot.com")
    assert other.startswith("MardenBot/") and "cody@marden.ai" in other


def test_retry_after_parsing():
    assert http.retry_after_seconds("7") == 7.0
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    later = format_datetime(now + timedelta(seconds=30), usegmt=True)
    assert http.retry_after_seconds(later, now=now) == pytest.approx(30, abs=1)
    assert http.retry_after_seconds("soon") is None


def test_retries_5xx_then_succeeds(server, fetcher):
    resp = fetcher.get(f"{server}/flaky")
    assert resp.status == 200 and resp.content == b"ok after retries"
    assert _Handler.counts["/flaky"] == 3


def test_gives_up_on_long_retry_after_and_on_404(server, fetcher):
    with pytest.raises(FetchError, match="Retry-After"):
        fetcher.get(f"{server}/throttled")
    with pytest.raises(FetchError) as info:
        fetcher.get(f"{server}/nope")
    assert info.value.status == 404


def test_conditional_get_uses_etag(server, fetcher):
    first = fetcher.get(f"{server}/etag")
    second = fetcher.get(f"{server}/etag")
    assert first.status == 200 and not first.from_cache
    assert second.not_modified and second.from_cache and second.content == b"versioned body"


def test_descriptive_user_agent_sent(server, fetcher):
    assert fetcher.get(f"{server}/ua").text().startswith("MardenBot/")


def _source(source_id: str, blocked: bool = False) -> None:
    sources.ensure_source(
        source_id, source_id.upper(), "api", "http://127.0.0.1", None, "local test server",
        blocked=blocked, blocked_reason="terms forbid automated collection" if blocked else None,
    )


def test_fetch_document_raw_first_and_304(server, fetcher):
    _source("local_etag")
    url = f"{server}/etag"
    first = fetch_document("local_etag", url, doc_type="test", fetcher=fetcher)
    assert first.was_new and first.read() == b"versioned body"
    assert first.meta["http"]["etag"] == '"v1"' and first.bytes == len(b"versioned body")
    fresh = Fetcher(backoff_base=0.01)  # no in-memory cache: validators come from the document row
    second = fetch_document("local_etag", url, doc_type="test", fetcher=fresh)
    fresh.close()
    assert not second.was_new and second.id == first.id


def test_fetch_document_refuses_blocked_source(server, fetcher):
    _source("local_blocked", blocked=True)
    with pytest.raises(sources.SourceBlocked):
        fetch_document("local_blocked", f"{server}/ua", fetcher=fetcher)
    assert "/ua" not in _Handler.counts  # no request was made


@pytest.mark.network
def test_fetch_document_idempotent_against_sec(committed):
    sources.ensure_source(
        "test_sec_robots", "SEC robots.txt (test)", "api", "https://www.sec.gov", 10,
        "SEC fair access policy: declared User-Agent with contact, under 10 requests/second",
    )
    url = "https://www.sec.gov/robots.txt"
    with Fetcher() as f:
        first = fetch_document("test_sec_robots", url, doc_type="robots", fetcher=f)
        second = fetch_document("test_sec_robots", url, doc_type="robots", fetcher=f)
    assert first.was_new and not second.was_new and first.id == second.id
    body = first.read()
    assert b"User-agent" in body or b"user-agent" in body.lower()
    n = committed.execute("SELECT count(*) AS n FROM document WHERE url = %s", (url,)).fetchone()["n"]
    assert n == 1
