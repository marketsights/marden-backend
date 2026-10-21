"""ERCOT Public API (api.ercot.com) access shared by the ERCOT collectors (`ercot_gis`, `ercot_prices`).

The API needs a free account registered by a person on apiexplorer.ercot.com (bots may not register), its
subscription key, and a one-hour ID token from the account's sign-in (ERCOT's Azure B2C ROPC flow). Without
ERCOT_API_KEY, ERCOT_API_USERNAME and ERCOT_API_PASSWORD a collector raises NeedsKey before any request, which
`rates_common.collect` records on the source as "needs key". The password goes in the sign-in form body only;
the key and token travel as request headers and are never stored (documents keep the plain URL).

ERCOT allows 30 API requests a minute (developer.ercot.com, Known Limitations): api.ercot.com is held to 0.4
requests a second here, and `http.Fetcher` backs off on 429 (Retry-After honoured); a 429 that persists
through the retries fails the run, which is recorded on the source.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from marden.core import http
from marden.core.config import get_settings
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, FetchError

API_HOST = "api.ercot.com"
API_BASE = f"https://{API_HOST}/api/public-reports"
TOKEN_HOST = "ercotb2c.b2clogin.com"
TOKEN_URL = f"https://{TOKEN_HOST}/ercotb2c.onmicrosoft.com/B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token"
CLIENT_ID = "fec253ea-0d06-4272-a5e6-b478baeecd70"  # ERCOT's published Public API client id
TOKEN_SECONDS = 50 * 60  # ID tokens last an hour; sign in again well before that
http.set_rate_limit(API_HOST, 0.4)  # under ERCOT's 30 requests a minute
http.set_rate_limit(TOKEN_HOST, 0.2)


class NeedsKey(FetchError):
    """The ERCOT Public API credentials are not configured; no request is made."""


def credentials(url: str) -> tuple[str, str, str]:
    """(subscription key, username, password), or NeedsKey naming `url` (the request that was not made)."""
    s = get_settings()
    if not s.has_ercot_api():
        raise NeedsKey(url, None, "needs key: set ERCOT_API_KEY, ERCOT_API_USERNAME and ERCOT_API_PASSWORD "
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


class Session:
    """Signed-in API access for one run: credential headers (token renewed before it expires) and raw-first
    GETs through `http.fetch_document`. Credentials are read (or NeedsKey raised) when the session is made."""

    def __init__(self, fetcher: Fetcher, *, first_url: str):
        self.fetcher = fetcher
        self._key, self._username, self._password = credentials(first_url)
        self._token: str | None = None
        self._signed_in_at = 0.0
        self.requests = 0

    def headers(self) -> dict[str, str]:
        if self._token is None or time.monotonic() - self._signed_in_at > TOKEN_SECONDS:
            self._token = id_token(self.fetcher, self._username, self._password)
            self._signed_in_at = time.monotonic()
        return {"Ocp-Apim-Subscription-Key": self._key, "Authorization": f"Bearer {self._token}"}

    def document(self, source_id: str, url: str, **doc: Any) -> DocumentRow:
        """GET `url` with the credential headers; the bytes are stored raw (document row, idempotent)."""
        self.requests += 1
        return http.fetch_document(source_id, url, fetcher=self.fetcher, secret_headers=self.headers(), **doc)
