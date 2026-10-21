"""Links in alert emails: the screen, Account > Alerts, and a signed one-click unsubscribe.

Token = base64url(canonical JSON payload) + "." + base64url(HMAC-SHA256(ALERT_LINK_SECRET, first part)).
Payload: {"v": 1, "o": org id, "u": user id, "s": scope, "iat": unix seconds}. Scope is what the link
switches off: 'all' (every alert email), 'instant', 'daily' or 'weekly'. The API verifies the same bytes
(apps/api/src/lib/unsubscribe-token.ts; the shared test vector is in both test suites).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any
from urllib.parse import quote

SCOPES = ("all", "instant", "daily", "weekly")
UNSUBSCRIBE_PATH = "/api/email/unsubscribe"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(secret: str, body: str) -> str:
    return _b64(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())


def sign_unsubscribe(secret: str, *, org_id: str, user_id: str, scope: str, issued_at: int) -> str:
    if scope not in SCOPES:
        raise ValueError(f"unknown unsubscribe scope {scope!r}")
    payload = {"iat": int(issued_at), "o": org_id, "s": scope, "u": user_id, "v": 1}
    body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    return f"{body}.{_mac(secret, body)}"


def verify_unsubscribe(secret: str, token: str) -> dict[str, Any] | None:
    """The payload when the signature matches and the fields are well formed, else None."""
    body, _, mac = token.partition(".")
    if not body or not mac or not hmac.compare_digest(_mac(secret, body), mac):
        return None
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, UnicodeDecodeError):
        return None
    ok = (
        isinstance(payload, dict) and payload.get("v") == 1 and payload.get("s") in SCOPES
        and isinstance(payload.get("o"), str) and isinstance(payload.get("u"), str)
    )
    return payload if ok else None


def unsubscribe_url(app_url: str, token: str) -> str:
    return f"{app_url}{UNSUBSCRIBE_PATH}?token={quote(token, safe='')}"


def manage_url(app_url: str) -> str:
    return f"{app_url}/account?tab=alerts"


def screen_url(app_url: str, ref_url: str) -> str:
    return f"{app_url}{ref_url}" if ref_url.startswith("/") else ref_url
