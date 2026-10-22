"""Polite fetching for the market-data sites Marden reads without a licence (docs/methods/licensing.md).

Used by `yahoo_equity` and `boerse_frankfurt`. The limits the business owner set are enforced here:

- Honest identity: the declared User-Agent (SEC_USER_AGENT, default "Marden research cody@marden.ai")
  and an ordinary Accept header. No browser User-Agent, no fingerprint headers, no rotating identity,
  no proxies.
- Low rate: the collector caps its host at 1 request/second (`http.set_rate_limit`) and every request
  waits a further random 0.2-0.8 s, so requests never arrive on a fixed beat.
- Bot protection is never evaded. A Cloudflare `cf-mitigated` header, a CAPTCHA or "Just a moment"
  page, a redirect to a login or consent page, HTTP 401/403, or a 429 that persists after one polite
  retry raises `Challenged`: the run is recorded as failed on the source row and stops. Nothing is
  retried with different headers.
- Once a day after the close: `closed_window` says why a run now would break that (before the source's
  window opens, or a full run already succeeded since it opened); the collectors skip such a run unless
  forced. The scheduled tasks run inside the window.
"""

from __future__ import annotations

import random
import re
import time
from datetime import datetime, tzinfo
from typing import Any
from urllib.parse import urlparse

from marden.core import db
from marden.core.config import Settings
from marden.core.http import Fetcher, FetchError, FetchResponse, Validators

JITTER_SECONDS = (0.2, 0.8)
STOP_STATUSES = frozenset({401, 403, 407, 429, 451})
ACCEPT_JSON = "application/json, text/plain, */*"
# Body markers of an interstitial (bot check, CAPTCHA, login or consent wall) instead of data.
_CHALLENGE = re.compile(
    rb"cf-chl|challenge-platform|just a moment\.\.\.|captcha|attention required|access denied"
    rb"|please enable cookies|are you a robot|unusual traffic",
    re.I,
)
CHALLENGE = _CHALLENGE  # for collectors of HTML pages, which check the body themselves (collectors/etf_oas)
_LOGIN_HOSTS = ("login.", "consent.", "guce.", "signin.", "auth.", "accounts.")


class Challenged(FetchError):
    """The site answered with bot protection, a login wall or a rate-limit storm: stop the run."""


def challenge_reason(resp: FetchResponse, *, expect_json: bool) -> str | None:
    """Why a 2xx response is not data but an interstitial, or None when it looks like data."""
    if resp.headers.get("cf-mitigated"):
        return f"cf-mitigated: {resp.headers['cf-mitigated']}"
    final_host = (urlparse(resp.final_url).hostname or "").lower()
    if final_host != (urlparse(resp.url).hostname or "").lower() and final_host.startswith(_LOGIN_HOSTS):
        return f"redirected to {final_host} (login or consent wall)"
    ctype = (resp.content_type or "").lower()
    if expect_json and "html" in ctype:
        head = resp.content[:20000]
        if _CHALLENGE.search(head):
            return "HTML challenge page instead of JSON"
        return f"HTML instead of JSON ({ctype})"
    return None


class MarketFetcher(Fetcher):
    """A Fetcher that identifies honestly, adds jitter and stops at bot protection."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        expect_json: bool = True,
        jitter: tuple[float, float] = JITTER_SECONDS,
        max_retries: int = 1,
        **kwargs: Any,
    ):
        super().__init__(settings=settings, max_retries=max_retries, **kwargs)
        self.expect_json = expect_json
        self.jitter = jitter
        self.requests = 0

    @property
    def user_agent(self) -> str:
        return self.settings.sec_user_agent

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        validators: Validators | None = None,
        use_cache: bool = True,
    ) -> FetchResponse:
        low, high = self.jitter
        if high > 0:
            time.sleep(random.uniform(low, high))
        sent = {"User-Agent": self.user_agent, "Accept": ACCEPT_JSON if self.expect_json else "*/*"}
        sent.update(headers or {})
        self.requests += 1
        try:
            resp = super().get(url, params=params, headers=sent, validators=validators, use_cache=use_cache)
        except FetchError as exc:
            if exc.status in STOP_STATUSES:
                raise Challenged(exc.url, exc.status, f"stopped: HTTP {exc.status} (no evasion attempted)") from None
            raise
        reason = challenge_reason(resp, expect_json=self.expect_json)
        if reason:
            raise Challenged(resp.url, resp.status, f"stopped: {reason} (no evasion attempted)")
        return resp


FULL_RUN = "all"  # source_run.meta['scope'] of a run over the whole watched universe


def closed_window(source_id: str, now: datetime, zone: tzinfo, opens: tuple[int, int], label: str) -> str | None:
    """Why a run at `now` would break the once-a-day-after-the-close limit, or None when it may run:
    before `opens` (local time in `zone`), or a full run (meta scope 'all') already ok since then."""
    local = now.astimezone(zone)
    since = local.replace(hour=opens[0], minute=opens[1], second=0, microsecond=0)
    if local < since:
        return f"before {opens[0]:02d}:{opens[1]:02d} {label} (once a day after the close; --force overrides)"
    with db.transaction() as conn:
        row = conn.execute(
            """SELECT 1 FROM source_run WHERE source_id = %s AND ok AND started_at >= %s
               AND meta->>'scope' = %s LIMIT 1""", (source_id, since, FULL_RUN),
        ).fetchone()
    return "already collected after today's close (--force overrides)" if row else None
