"""Email provider adapters: Postmark and Resend over their HTTP APIs (httpx), chosen by EmailConfig.

    provider = make_provider(cfg)                  # None when email is not configured
    result = provider.send(message, idempotency_key)

A result says whether the provider accepted the message (with its message id) and, when it did not, whether
a retry can help (network errors, 429, 5xx) or not (4xx: a bad address, an unverified sender). Errors carry
the HTTP status and the provider's own message; keys are sent only in headers and never logged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from marden.alerts.settings import EmailConfig

TIMEOUT = httpx.Timeout(20.0, connect=10.0)
RESEND_URL = "https://api.resend.com/emails"
POSTMARK_URL = "https://api.postmarkapp.com/email"
POSTMARK_STREAM = "outbound"
MAX_ERROR = 300


@dataclass(frozen=True)
class Message:
    to: str
    sender: str
    subject: str
    html: str
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    tag: str = "alert"
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SendResult:
    ok: bool
    message_id: str | None = None
    retryable: bool = False
    error: str | None = None


class Provider(Protocol):
    name: str

    def send(self, message: Message, idempotency_key: str) -> SendResult: ...


def _error_text(resp: httpx.Response) -> str:
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text
    if isinstance(body, dict):
        body = body.get("message") or body.get("Message") or body.get("name") or body
    return f"HTTP {resp.status_code}: {str(body)[:MAX_ERROR]}"


def _failure(resp: httpx.Response) -> SendResult:
    retryable = resp.status_code == 429 or resp.status_code >= 500
    return SendResult(ok=False, retryable=retryable, error=_error_text(resp))


class _Http:
    def __init__(self, client: httpx.Client | None) -> None:
        self._client = client

    def post(self, url: str, *, headers: dict[str, str], body: dict[str, Any]) -> httpx.Response | SendResult:
        try:
            if self._client is not None:
                return self._client.post(url, headers=headers, json=body, timeout=TIMEOUT)
            with httpx.Client(timeout=TIMEOUT) as client:
                return client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:  # network: worth a retry
            return SendResult(ok=False, retryable=True, error=f"{type(exc).__name__}: {str(exc)[:MAX_ERROR]}")


class ResendProvider(_Http):
    name = "resend"

    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        super().__init__(client)
        self._key = api_key

    def send(self, message: Message, idempotency_key: str) -> SendResult:
        body = {
            "from": message.sender, "to": [message.to], "subject": message.subject,
            "html": message.html, "text": message.text, "headers": message.headers,
            "tags": [{"name": "kind", "value": message.tag}],
        }
        headers = {"Authorization": f"Bearer {self._key}", "Idempotency-Key": idempotency_key[:256]}
        resp = self.post(RESEND_URL, headers=headers, body=body)
        if isinstance(resp, SendResult):
            return resp
        if resp.status_code in (200, 201):
            data = resp.json()
            return SendResult(ok=True, message_id=str(data.get("id")) if data.get("id") else None)
        return _failure(resp)


class PostmarkProvider(_Http):
    name = "postmark"

    def __init__(self, token: str, client: httpx.Client | None = None) -> None:
        super().__init__(client)
        self._token = token

    def send(self, message: Message, idempotency_key: str) -> SendResult:
        body = {
            "From": message.sender, "To": message.to, "Subject": message.subject,
            "HtmlBody": message.html, "TextBody": message.text, "MessageStream": POSTMARK_STREAM,
            "Headers": [{"Name": k, "Value": v} for k, v in message.headers.items()],
            "Tag": message.tag, "Metadata": {**message.metadata, "idempotency_key": idempotency_key[:80]},
        }
        headers = {"X-Postmark-Server-Token": self._token, "Accept": "application/json"}
        resp = self.post(POSTMARK_URL, headers=headers, body=body)
        if isinstance(resp, SendResult):
            return resp
        if resp.status_code == 200:
            data = resp.json()
            if int(data.get("ErrorCode") or 0) == 0:
                return SendResult(ok=True, message_id=str(data.get("MessageID")) if data.get("MessageID") else None)
        return _failure(resp)


def make_provider(cfg: EmailConfig, client: httpx.Client | None = None) -> Provider | None:
    if not cfg.configured or cfg.api_key is None:
        return None
    if cfg.provider == "resend":
        return ResendProvider(cfg.api_key, client)
    if cfg.provider == "postmark":
        return PostmarkProvider(cfg.api_key, client)
    return None
