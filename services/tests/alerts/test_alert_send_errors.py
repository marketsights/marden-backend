"""Sending when something goes wrong, on real alerts: an outbox row that raises while it is rendered or sent
backs off and fails after the retries without holding up the rows behind it, and a provider key never reaches
last_error (which Account > Alerts shows the member), whether the HTTP library or the provider quotes it.
The providers are reached through httpx.MockTransport (Marden's side of the HTTP contract only)."""

from __future__ import annotations

import json

import httpx
import pytest
from alert_rows import rows
from test_alert_email import _cfg, _next_cut, live_alerts  # noqa: F401  (fixture)

from marden.alerts import outbox
from marden.alerts.providers import REDACTED, Message, PostmarkProvider, ResendProvider

KEY = "test-provider-key"  # the key _cfg() configures


def _two_rows(alerts) -> tuple:
    """A daily digest and a weekly summary for the same alerts (two queued rows)."""
    daily_at = _next_cut(max(a["at"] for a in alerts))
    weekly_at = _next_cut(max(a["at"] for a in alerts), monday=True)
    outbox.build_digests(daily_at)
    outbox.build_digests(weekly_at)
    kinds = {r["kind"] for r in rows("SELECT kind FROM email_outbox WHERE status = 'queued'")}
    if kinds != {"daily_digest", "weekly_digest"}:
        pytest.skip(f"expected a daily and a weekly row, built {sorted(kinds)}")
    return daily_at, max(daily_at, weekly_at)


def test_a_row_that_raises_backs_off_and_does_not_block_the_rest(live_alerts, monkeypatch):  # noqa: F811
    _, now = _two_rows(live_alerts)
    real_render = outbox.render

    def render(kind, *args, **kwargs):
        if kind == "daily_digest":
            raise KeyError("ref_url")
        return real_render(kind, *args, **kwargs)

    monkeypatch.setattr(outbox, "render", render)
    sent: list[httpx.Request] = []
    ok = httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200, json={"id": "re_1"}))
    with httpx.Client(transport=ok) as client:
        counts = outbox.send_due(cfg=_cfg(), client=client, now=now)
        assert counts == {"retry": 1, "sent": 1}  # the weekly summary went out behind the broken row
        bad = rows("SELECT * FROM email_outbox WHERE kind = 'daily_digest'")[0]
        assert bad["status"] == "queued" and bad["attempts"] == 1
        assert bad["last_error"] == "KeyError while rendering or sending (worker log has the details)"
        assert bad["next_attempt_at"] == now + outbox.backoff(1)
        assert outbox.send_due(cfg=_cfg(), client=client, now=now) == {}  # not due again yet
        for _ in range(2, outbox.MAX_ATTEMPTS + 1):
            now = rows("SELECT next_attempt_at FROM email_outbox WHERE kind = 'daily_digest'")[0]["next_attempt_at"]
            outbox.send_due(cfg=_cfg(), client=client, now=now)
    bad = rows("SELECT * FROM email_outbox WHERE kind = 'daily_digest'")[0]
    assert bad["status"] == "failed" and bad["attempts"] == outbox.MAX_ATTEMPTS
    assert len(sent) == 1 and json.loads(sent[0].content)["subject"].startswith("Marden weekly summary")


def _message() -> Message:
    return Message(to="a1@example.test", sender="alerts@marden.test", subject="s", html="<p>h</p>", text="t")


@pytest.mark.parametrize("provider", [ResendProvider, PostmarkProvider])
def test_the_key_never_reaches_the_error_text(provider):
    def refused(request: httpx.Request) -> httpx.Response:  # an HTTP library quoting a header it refuses
        raise httpx.LocalProtocolError(f"Illegal header value b'Bearer {KEY}'")

    with httpx.Client(transport=httpx.MockTransport(refused)) as client:
        result = provider(KEY, client).send(_message(), "daily:o:u:2026-10-22")
    assert not result.ok and result.retryable and KEY not in result.error and REDACTED in result.error

    echoed = httpx.MockTransport(lambda r: httpx.Response(401, json={"message": f"API key {KEY} is invalid",
                                                                     "Message": f"API key {KEY} is invalid"}))
    with httpx.Client(transport=echoed) as client:
        result = provider(KEY, client).send(_message(), "daily:o:u:2026-10-22")
    assert not result.ok and not result.retryable
    assert result.error == f"HTTP 401: API key {REDACTED} is invalid"


def test_a_redacted_failure_is_what_the_outbox_keeps(live_alerts):  # noqa: F811
    now = _next_cut(max(a["at"] for a in live_alerts))
    outbox.build_digests(now)
    echoed = httpx.MockTransport(lambda r: httpx.Response(422, json={"message": f"bad key {KEY}"}))
    with httpx.Client(transport=echoed) as client:
        assert outbox.send_due(cfg=_cfg(), client=client, now=now) == {"failed": 1}
    stored = rows("SELECT last_error, html, text_body FROM email_outbox")[0]
    assert KEY not in stored["last_error"] and stored["last_error"] == f"HTTP 422: bad key {REDACTED}"


def test_redact_covers_the_quoted_form_of_a_key_with_a_stray_newline():
    from marden.alerts.providers import redact

    key = "re_pasted_key\nX"  # a key pasted with a line break: httpx refuses it and quotes b'Bearer re_...\nX'
    quoted = f"LocalProtocolError: Illegal header value {('Bearer ' + key).encode()!r}"
    assert "re_pasted_key" not in redact(quoted, key) and REDACTED in redact(quoted, key)
    assert redact("x" * 1000, key) == "x" * 300  # clipped
