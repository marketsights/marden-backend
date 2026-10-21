"""Email delivery on real alerts: digests batch a user's alerts once per period, the Resend and Postmark adapters
send what the outbox holds (HTML + text, screen links, signed one-click unsubscribe), transient failures retry
with backoff and permanent ones fail, a user who switches an email off before it goes is not sent it, and the
unsubscribe token matches the API's (shared vector). The providers are reached through httpx.MockTransport:
these tests prove Marden's side of the HTTP contract, not Resend's or Postmark's servers."""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta

import httpx
import psycopg
import pytest
from alert_rows import copy_sites, make_firm, rows
from conftest import TEST_URL
from test_alert_engine import SITE, _status_change

from marden.alerts import engine, links, outbox
from marden.alerts.settings import EmailConfig, email_config
from marden.readmodels.context import BUSINESS_TZ

SECRET = "marden-test-secret-0123456789abcdefghij"
VECTOR = ("eyJpYXQiOjE3NjAwMDAwMDAsIm8iOiJvcmdfYSIsInMiOiJkYWlseSIsInUiOiJ1c2VyX2ExIiwidiI6MX0"
          ".tj6HMNoMWaYnmrSaUoneFglMgTfad2WRhUpLsRGFRRQ")
APP = "https://app.marden.test"


def _cfg(provider: str = "resend") -> EmailConfig:
    return EmailConfig(provider=provider, api_key="test-provider-key", from_email="alerts@marden.test",
                       link_secret=SECRET, app_url=APP)


def _next_cut(at: datetime, *, monday: bool = False) -> datetime:
    """The first 07:00 ET strictly after `at` (a Monday for the weekly summary), plus five minutes."""
    local = at.astimezone(BUSINESS_TZ)
    cut = datetime.combine(local.date(), time(7), BUSINESS_TZ)
    while cut <= local or (monday and cut.weekday() != 0):
        cut += timedelta(days=1)
    return cut + timedelta(minutes=5)


@pytest.fixture
def live_alerts():
    if SITE not in copy_sites([SITE])["sites"]:
        pytest.skip("jupiter is not in the source database")
    make_firm("org_a", [("a1", None, True), ("a2", {"dailyDigest": False, "weeklyNote": False}, True)],
              [("site", SITE)])
    before, after, _ = _status_change(SITE)
    engine.scan(since=before + timedelta(days=1), today=after)
    alerts = rows("SELECT * FROM alert WHERE user_id = 'a1' ORDER BY id")
    assert alerts, "the real status change made no alert"
    return alerts


def test_digests_batch_each_users_alerts_once(live_alerts):
    latest = max(a["at"] for a in live_alerts)
    built = outbox.build_digests(_next_cut(latest))
    daily = rows("SELECT * FROM email_outbox WHERE kind = 'daily_digest'")
    assert built["daily_digest"] == 1 and [d["user_id"] for d in daily] == ["a1"]  # a2 switched digests off
    expected = {a["id"] for a in live_alerts if a["severity"] != "red"} | {
        a["id"] for a in live_alerts if a["severity"] == "red"}  # no instant row was queued in this test
    assert set(daily[0]["alert_ids"]) == expected
    assert daily[0]["idempotency_key"].startswith("daily:org_a:a1:") and daily[0]["status"] == "queued"
    assert outbox.build_digests(_next_cut(latest)) == {"daily_digest": 0, "weekly_digest": 0}  # once per day

    weekly = outbox.build_digests(_next_cut(latest, monday=True))
    rows_w = rows("SELECT * FROM email_outbox WHERE kind = 'weekly_digest'")
    assert weekly["weekly_digest"] == 1 and set(rows_w[0]["alert_ids"]) == {a["id"] for a in live_alerts}


def test_red_alerts_in_an_instant_email_are_left_out_of_the_digest(live_alerts):
    with psycopg.connect(TEST_URL, autocommit=True) as c:  # mark one real alert red (as a T2 or M4 alert is)
        c.execute("UPDATE alert SET severity = 'red' WHERE id = %s", (live_alerts[0]["id"],))
    assert outbox.sweep_instant() == 1
    instant = rows("SELECT * FROM email_outbox WHERE kind = 'instant'")[0]
    assert instant["alert_ids"] == [live_alerts[0]["id"]] and instant["user_id"] == "a1"
    outbox.build_digests(_next_cut(max(a["at"] for a in live_alerts)))
    daily = rows("SELECT * FROM email_outbox WHERE kind = 'daily_digest'")[0]
    assert live_alerts[0]["id"] not in daily["alert_ids"]


def _due(now: datetime) -> dict:
    return rows("SELECT * FROM email_outbox WHERE status = 'queued' AND next_attempt_at <= %s", (now,))


def test_resend_sends_html_text_links_and_one_click_unsubscribe(live_alerts):
    now = _next_cut(max(a["at"] for a in live_alerts))
    outbox.build_digests(now)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "re_msg_1"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert outbox.send_due(cfg=_cfg(), client=client, now=now) == {"sent": 1}
    req = seen[0]
    row = rows("SELECT * FROM email_outbox")[0]
    assert str(req.url) == "https://api.resend.com/emails"
    assert req.headers["Authorization"] == "Bearer test-provider-key"
    assert req.headers["Idempotency-Key"] == row["idempotency_key"]
    body = json.loads(req.content)
    assert body["to"] == ["a1@example.test"] and body["from"] == "alerts@marden.test"
    assert body["subject"].startswith("Marden daily digest:")
    first = live_alerts[0]
    assert first["headline"].split(":")[0] in body["html"] and f"{APP}{first['ref_url']}" in body["text"]
    assert f"{APP}/account?tab=alerts" in body["text"]
    unsub = body["headers"]["List-Unsubscribe"].strip("<>")
    assert body["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert unsub.startswith(f"{APP}/api/email/unsubscribe?token=")
    token = httpx.URL(unsub).params["token"]
    assert links.verify_unsubscribe(SECRET, token) == {"iat": int(now.timestamp()), "o": "org_a", "s": "daily",
                                                       "u": "a1", "v": 1}
    assert row["status"] == "sent" and row["provider"] == "resend" and row["provider_message_id"] == "re_msg_1"
    assert row["html"] == body["html"] and row["text_body"] == body["text"] and row["attempts"] == 1
    emailed = rows("SELECT id FROM alert WHERE emailed_at IS NOT NULL")
    assert {r["id"] for r in emailed} == set(row["alert_ids"])
    assert rows("SELECT configured, provider FROM email_provider_state")[0] == {"configured": True,
                                                                                "provider": "resend"}


def test_transient_failures_retry_with_backoff_then_fail(live_alerts):
    now = _next_cut(max(a["at"] for a in live_alerts))
    outbox.build_digests(now)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503, json={"message": "busy"}))) as c:
        for attempt in range(1, outbox.MAX_ATTEMPTS + 1):
            assert _due(now), f"attempt {attempt} not due"
            outbox.send_due(cfg=_cfg(), client=c, now=now)
            row = rows("SELECT * FROM email_outbox")[0]
            assert row["attempts"] == attempt and "HTTP 503: busy" in row["last_error"]
            if attempt < outbox.MAX_ATTEMPTS:
                assert row["status"] == "queued" and row["next_attempt_at"] == now + outbox.backoff(attempt)
                assert not _due(now + outbox.backoff(attempt) - timedelta(seconds=1))
                now += outbox.backoff(attempt)
    assert row["status"] == "failed"
    assert not rows("SELECT 1 FROM alert WHERE emailed_at IS NOT NULL")


def test_permanent_failure_and_postmark(live_alerts):
    now = _next_cut(max(a["at"] for a in live_alerts))
    outbox.build_digests(now)
    bad = httpx.MockTransport(lambda r: httpx.Response(422, json={"ErrorCode": 300, "Message": "Invalid 'To'"}))
    with httpx.Client(transport=bad) as c:
        assert outbox.send_due(cfg=_cfg("postmark"), client=c, now=now) == {"failed": 1}
    assert "HTTP 422: Invalid 'To'" in rows("SELECT last_error FROM email_outbox")[0]["last_error"]

    weekly_now = _next_cut(max(a["at"] for a in live_alerts), monday=True)
    outbox.build_digests(weekly_now)
    seen: list[httpx.Request] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ErrorCode": 0, "Message": "OK", "MessageID": "pm-1"})

    with httpx.Client(transport=httpx.MockTransport(ok)) as c:
        assert outbox.send_due(cfg=_cfg("postmark"), client=c, now=weekly_now) == {"sent": 1}
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://api.postmarkapp.com/email"
    assert seen[0].headers["X-Postmark-Server-Token"] == "test-provider-key"
    assert body["To"] == "a1@example.test" and body["MessageStream"] == "outbound" and body["TextBody"]
    assert {"Name": "List-Unsubscribe-Post", "Value": "List-Unsubscribe=One-Click"} in body["Headers"]
    sent = rows("SELECT * FROM email_outbox WHERE kind = 'weekly_digest'")[0]
    assert sent["status"] == "sent" and sent["provider_message_id"] == "pm-1"


def test_switched_off_before_sending_is_cancelled(live_alerts):
    now = _next_cut(max(a["at"] for a in live_alerts))
    outbox.build_digests(now)
    with psycopg.connect(TEST_URL, autocommit=True) as c:
        c.execute("""INSERT INTO alert_settings (org_id, user_id, settings) VALUES ('org_a', 'a1', '{"dailyDigest": false}')""")
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("nothing may be sent"))) as c:
        assert outbox.send_due(cfg=_cfg(), client=c, now=now) == {"cancelled": 1}
    assert rows("SELECT last_error FROM email_outbox")[0]["last_error"] == "dailyDigest is switched off"


def test_unsubscribe_token_vector_and_tampering():
    token = links.sign_unsubscribe(SECRET, org_id="org_a", user_id="user_a1", scope="daily", issued_at=1760000000)
    assert token == VECTOR  # apps/api/test/alert-delivery.test.ts verifies the same token
    assert links.verify_unsubscribe(SECRET, token)["u"] == "user_a1"
    body, mac = token.split(".")
    assert links.verify_unsubscribe(SECRET, f"{body}x.{mac}") is None
    assert links.verify_unsubscribe(SECRET + "x", token) is None
    assert links.verify_unsubscribe(SECRET, "nonsense") is None
    with pytest.raises(ValueError):
        links.sign_unsubscribe(SECRET, org_id="o", user_id="u", scope="everything", issued_at=0)


def test_email_config_names_what_is_missing_never_values():
    cfg = email_config({})
    assert not cfg.configured and cfg.provider is None
    assert cfg.reason == ("email is not configured: set POSTMARK_TOKEN or RESEND_API_KEY, ALERT_FROM_EMAIL, "
                          "ALERT_LINK_SECRET (at least 32 characters), MARDEN_APP_URL")
    env = {"RESEND_API_KEY": "re_value_1", "ALERT_FROM_EMAIL": "alerts@marden.test", "ALERT_LINK_SECRET": SECRET,
           "MARDEN_APP_URL": "https://app.marden.test/"}
    ok = email_config(env)
    assert ok.configured and ok.provider == "resend" and ok.app_url == "https://app.marden.test"
    assert "re_value_1" not in repr(ok) and SECRET not in repr(ok)
    both = email_config({**env, "POSTMARK_TOKEN": "pm_value"})
    assert both.provider == "postmark"  # spec 21 names Postmark; ALERT_EMAIL_PROVIDER overrides
    assert email_config({**env, "POSTMARK_TOKEN": "pm_value", "ALERT_EMAIL_PROVIDER": "resend"}).provider == "resend"
    picked = email_config({**env, "ALERT_EMAIL_PROVIDER": "postmark"})
    assert not picked.configured and picked.missing == ("POSTMARK_TOKEN",)
