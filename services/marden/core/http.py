"""Polite HTTP: declared User-Agent, per-host rate limits, retries, conditional GETs.

- sec.gov hosts get SEC_USER_AGENT and share one bucket capped at 8 req/s (SEC allows 10).
- Other hosts get a descriptive Marden User-Agent and 2 req/s unless a collector sets a limit.
- 429/5xx/timeouts are retried with exponential backoff, honoring Retry-After.
- ETag / Last-Modified are replayed as If-None-Match / If-Modified-Since.

`fetch_document` is the raw-first entry point for collectors: fetch -> rawstore -> `document` row.
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from marden.core import documents, sources
from marden.core.config import Settings, get_settings
from marden.core.db import Conn
from marden.core.documents import DocumentRow

SEC_SUFFIX = "sec.gov"
SEC_MAX_PER_SECOND = 8.0
DEFAULT_PER_SECOND = 2.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_BACKOFF_SECONDS = 60.0
MAX_RETRY_AFTER_SECONDS = 600.0
CACHE_ENTRIES = 256
CACHE_MAX_BODY = 10 * 1024 * 1024


class FetchError(RuntimeError):
    def __init__(self, url: str, status: int | None, message: str):
        super().__init__(f"{message} ({url}, status={status})")
        self.url = url
        self.status = status


# ---------------------------------------------------------------- rate limits
class TokenBucket:
    """`rate` tokens per second, at most `capacity` banked. capacity=1 means strict spacing."""

    def __init__(self, rate: float, capacity: float = 1.0):
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.capacity = max(1.0, capacity)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Take one token, sleeping as needed. Returns seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return waited
                delay = (1.0 - self.tokens) / self.rate
            time.sleep(delay)
            waited += delay


_rates: dict[str, float] = {SEC_SUFFIX: SEC_MAX_PER_SECOND}
_buckets: dict[str, TokenBucket] = {}
_buckets_lock = threading.Lock()


def _matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def is_sec_host(host: str) -> bool:
    return _matches(host.lower(), SEC_SUFFIX)


def set_rate_limit(host_suffix: str, per_second: float) -> None:
    """Declare a per-host limit (applies to the host and its subdomains, process-wide)."""
    host_suffix = host_suffix.lower().strip(".")
    if per_second <= 0:
        raise ValueError("per_second must be positive")
    if _matches(host_suffix, SEC_SUFFIX) and per_second > SEC_MAX_PER_SECOND:
        raise ValueError(f"sec.gov is capped at {SEC_MAX_PER_SECOND} req/s")
    with _buckets_lock:
        _rates[host_suffix] = per_second
        _buckets.pop(host_suffix, None)


def bucket_for(host: str) -> tuple[str, TokenBucket]:
    host = host.lower()
    matches = [s for s in _rates if _matches(host, s)]
    key = max(matches, key=len) if matches else host
    # A subdomain listed separately never escapes the sec.gov cap: all SEC hosts share one bucket.
    if is_sec_host(host):
        key = SEC_SUFFIX
    with _buckets_lock:
        bucket = _buckets.get(key)
        if bucket is None:
            bucket = _buckets[key] = TokenBucket(_rates.get(key, DEFAULT_PER_SECOND))
        return key, bucket


# ---------------------------------------------------------------- user agents
def marden_user_agent(settings: Settings) -> str:
    contact = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", settings.sec_user_agent)
    tail = f"; {contact.group(0)}" if contact else ""
    return f"MardenBot/0.1 (research on AI infrastructure debt; +https://marden.ai{tail})"


def user_agent_for(host: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return settings.sec_user_agent if is_sec_host(host) else marden_user_agent(settings)


# ---------------------------------------------------------------- responses and cache
@dataclass(frozen=True)
class FetchResponse:
    url: str
    final_url: str
    status: int
    content: bytes
    headers: dict[str, str] = field(default_factory=dict)
    from_cache: bool = False

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    @property
    def content_type(self) -> str | None:
        return self.headers.get("content-type")

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")

    @property
    def last_modified(self) -> str | None:
        return self.headers.get("last-modified")

    def text(self, encoding: str = "utf-8") -> str:
        return self.content.decode(encoding, errors="replace")

    def json(self) -> Any:
        return json.loads(self.content)


@dataclass(frozen=True)
class Validators:
    etag: str | None = None
    last_modified: str | None = None

    def headers(self) -> dict[str, str]:
        out = {}
        if self.etag:
            out["If-None-Match"] = self.etag
        if self.last_modified:
            out["If-Modified-Since"] = self.last_modified
        return out


def retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    """Parse Retry-After (delta seconds or HTTP date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


class Fetcher:
    """HTTP GET with the politeness rules above. Thread-safe; share one per process."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        max_retries: int = 4,
        backoff_base: float = 1.0,
    ):
        self.settings = settings or get_settings()
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 15.0)), follow_redirects=True
        )
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._cache: OrderedDict[str, FetchResponse] = OrderedDict()
        self._cache_lock = threading.Lock()

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _backoff(self, attempt: int) -> float:
        return min(MAX_BACKOFF_SECONDS, self.backoff_base * 2**attempt) * (0.5 + random.random() / 2)

    def _cached(self, key: str) -> FetchResponse | None:
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
            return hit

    def _store(self, key: str, resp: FetchResponse) -> None:
        if not (resp.etag or resp.last_modified) or len(resp.content) > CACHE_MAX_BODY:
            return
        with self._cache_lock:
            self._cache[key] = resp
            self._cache.move_to_end(key)
            while len(self._cache) > CACHE_ENTRIES:
                self._cache.popitem(last=False)

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        validators: Validators | None = None,
        use_cache: bool = True,
    ) -> FetchResponse:
        """GET a URL. Raises FetchError for non-2xx/304 after retries.

        A 304 returns the cached body (from_cache=True) when this Fetcher has one, else an empty
        body with `not_modified` set (the caller supplied the validators and holds the content).
        """
        target = httpx.URL(url, params=params) if params else httpx.URL(url)
        key = str(target)
        cached = self._cached(key) if use_cache else None
        if validators is None and cached is not None:
            validators = Validators(cached.etag, cached.last_modified)
        req_headers = {"User-Agent": user_agent_for(target.host, self.settings)}
        req_headers.update(headers or {})
        if validators is not None:
            req_headers.update(validators.headers())
        _, bucket = bucket_for(target.host)

        attempt = 0
        while True:
            bucket.acquire()
            try:
                raw = self.client.get(target, headers=req_headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self.max_retries:
                    raise FetchError(key, None, f"{type(exc).__name__}: {exc}") from exc
                time.sleep(self._backoff(attempt))
                attempt += 1
                continue
            if raw.status_code in RETRY_STATUSES and attempt < self.max_retries:
                wait = retry_after_seconds(raw.headers.get("retry-after"))
                if wait is not None and wait > MAX_RETRY_AFTER_SECONDS:
                    raise FetchError(key, raw.status_code, f"Retry-After {wait:.0f}s exceeds limit")
                time.sleep(wait if wait is not None else self._backoff(attempt))
                attempt += 1
                continue
            break

        resp = FetchResponse(
            url=key,
            final_url=str(raw.url),
            status=raw.status_code,
            content=raw.content,
            headers={k.lower(): v for k, v in raw.headers.items()},
        )
        if resp.status == 304:
            if cached is not None:
                return FetchResponse(key, cached.final_url, 304, cached.content, cached.headers, True)
            return resp
        if not 200 <= resp.status < 300:
            raise FetchError(key, resp.status, f"HTTP {resp.status}: {resp.content[:200]!r}")
        if use_cache:
            self._store(key, resp)
        return resp


_default: Fetcher | None = None
_default_lock = threading.Lock()


def default_fetcher() -> Fetcher:
    global _default
    with _default_lock:
        if _default is None:
            _default = Fetcher()
        return _default


def fetch_document(
    source_id: str,
    url: str,
    *,
    doc_type: str | None = None,
    title: str | None = None,
    doc_date: date | str | None = None,
    external_id: str | None = None,
    meta: dict[str, Any] | None = None,
    extract_state: str = "none",
    fetcher: Fetcher | None = None,
    conn: Conn | None = None,
) -> DocumentRow:
    """Fetch a URL for a source, store the raw bytes, insert `document` (idempotent).

    Returns the new row (was_new=True) or the existing row for identical bytes or a 304
    (was_new=False). Raises SourceBlocked/UnknownSource before any request if the source
    is blocked or undeclared in the DB.
    """
    sources.require_collectable(source_id, conn=conn)
    fetcher = fetcher or default_fetcher()
    prior = documents.latest_document(source_id, url, conn=conn)
    validators = None
    if prior is not None:
        prior_http = prior.meta.get("http") or {}
        if prior_http.get("etag") or prior_http.get("last_modified"):
            validators = Validators(prior_http.get("etag"), prior_http.get("last_modified"))

    # The document table is the conditional-GET cache here (it survives restarts), so the
    # Fetcher's in-memory cache is bypassed: a 304 can only answer validators of `prior`.
    resp = fetcher.get(url, validators=validators, use_cache=False)
    if resp.not_modified:
        if prior is None:
            raise FetchError(url, 304, "304 Not Modified to an unconditional request")
        return prior

    http_meta: dict[str, Any] = {"status": resp.status}
    if resp.etag:
        http_meta["etag"] = resp.etag
    if resp.last_modified:
        http_meta["last_modified"] = resp.last_modified
    if resp.final_url != url:
        http_meta["final_url"] = resp.final_url
    return documents.record_document(
        source_id,
        url,
        resp.content,
        content_type=resp.content_type,
        doc_type=doc_type,
        title=title,
        doc_date=doc_date,
        external_id=external_id,
        meta={**(meta or {}), "http": http_meta},
        extract_state=extract_state,
        conn=conn,
    )
