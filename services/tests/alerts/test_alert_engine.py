"""The alert engine on real rows from the local database: a real chain status change on a watched site gives
exactly one alert per watcher (per changed link), a live 'alert' stream event per alert scoped to its user,
and an instant email row for red; a rerun adds nothing; firms that do not watch the site, firms without a
live product and members without a product role get nothing; a backfill writes alerts only; repeats of one
matter join the first alert."""

from __future__ import annotations

import itertools
from datetime import timedelta

import psycopg
import pytest
from alert_rows import copy_sites, make_firm, rows
from conftest import TEST_URL

from marden.alerts import engine, outbox
from marden.alerts.triggers import extract
from marden.readmodels.context import BUSINESS_TZ

SITE, OTHER = "jupiter", "abilene"
EMAIL_VARS = ("RESEND_API_KEY", "POSTMARK_TOKEN", "ALERT_EMAIL_PROVIDER", "ALERT_FROM_EMAIL", "ALERT_LINK_SECRET",
              "MARDEN_APP_URL")


def _status_change(site: str, *, to: str | None = None):
    """The first pair of consecutive status outputs of the site where a chain link's status changed (to `to`)."""
    outs = rows("""SELECT DISTINCT ON (as_of) * FROM model_output WHERE model = 'status' AND subject_id = %s
                   ORDER BY as_of, created_at DESC, id DESC""", (site,))
    for before, after in itertools.pairwise(outs):
        old = {link["role"]: link["status"] for link in extract(before)["chain"]}
        changed = [link["role"] for link in extract(after)["chain"]
                   if link["role"] in old and old[link["role"]] != link["status"]
                   and {old[link["role"]], link["status"]} - {"none", "green"} and to in (None, link["status"])]
        if changed:
            return before["as_of"], after["as_of"], changed
    pytest.skip(f"no chain status change{f' to {to}' if to else ''} in the copied status outputs of {site}")


@pytest.fixture
def firms():
    copied = copy_sites([SITE, OTHER])
    if SITE not in copied["sites"] or not copied["instruments"]:
        pytest.skip("jupiter or its instruments are not in the source database")
    on_site = rows("SELECT id FROM instrument WHERE site_slug = %s ORDER BY id", (SITE,))
    instrument = on_site[0]["id"]
    make_firm("org_a", [("a1", None, True), ("a2", {"instantRed": False}, True), ("a3", None, False)],
              [("site", SITE)])
    make_firm("org_b", [("b1", None, True)], [("instrument", instrument)])  # watches the site through its debt
    make_firm("org_c", [("c1", None, True)], [("site", OTHER)])
    make_firm("org_d", [("d1", None, True)], [("site", SITE)], product=False)
    return {"instrument": instrument}


def _status_alerts(site: str):
    return rows("""SELECT * FROM alert WHERE trigger_key LIKE %s ORDER BY user_id, dedupe_key""",
                (f"model:status:site:{site}:%",))


def test_status_change_reaches_each_watcher_once(firms, monkeypatch):
    for name in EMAIL_VARS:
        monkeypatch.delenv(name, raising=False)
    before, after, changed = _status_change(SITE)
    stats = engine.scan(since=before + timedelta(days=1), today=after)
    alerts = _status_alerts(SITE)
    by_user: dict[str, list[str]] = {}
    for a in alerts:
        by_user.setdefault(a["user_id"], []).append(a["dedupe_key"].rsplit(":", 1)[-1])
    assert set(by_user) == {"a1", "a2", "b1"}  # not c1 (other site), d1 (no product), a3 (no product role)
    for roles in by_user.values():
        assert sorted(roles) == sorted(changed)  # exactly one alert per watcher per changed link
    assert not rows("SELECT 1 FROM alert WHERE user_id IN ('d1', 'a3')")
    jupiter_subjects = [SITE, firms["instrument"], *[r["id"] for r in rows(
        "SELECT id FROM instrument WHERE site_slug = %s", (SITE,))]]
    assert not rows("SELECT 1 FROM alert WHERE user_id = 'c1' AND subject_id = ANY(%s)", (jupiter_subjects,))
    a = next(x for x in alerts if x["user_id"] == "b1")
    assert a["org_id"] == "org_b" and a["ref_url"] == f"/sites/{SITE}" and a["tag"] == "M" and not a["backfill"]
    assert "\u2192" in a["headline"] and a["body"] and a["sources"] and a["occurred_on"] == after
    assert "On your watchlist:" in a["body"]

    # one live 'alert' event per new alert, scoped to its org and user, carrying the Alert
    streamed = rows("SELECT org_id, user_id, payload FROM stream_event WHERE payload->>'type' = 'alert'")
    assert len(streamed) == stats.alerts == len(rows("SELECT id FROM alert"))
    ev = next(s for s in streamed if s["payload"]["alert"]["id"] == str(a["id"]))
    assert ev["org_id"] == "org_b" and ev["user_id"] == "b1"
    assert ev["payload"]["alert"] == {"id": str(a["id"]), "at": a["at"].isoformat(), "headline": a["headline"],
                                      "tag": "M", "ruleLabel": a["rule_label"], "refUrl": a["ref_url"],
                                      "read": False, "emailed": False}

    # red alerts -> one instant email row per user who takes instant red (a2 switched it off)
    reds = {x["user_id"] for x in rows("SELECT user_id FROM alert WHERE severity = 'red'")}
    queued = outbox.sweep_instant()
    instant = rows("SELECT * FROM email_outbox WHERE kind = 'instant' ORDER BY user_id")
    assert {o["user_id"] for o in instant} == reds - {"a2"} and queued == len(instant)

    # a rerun evaluates nothing again: no alert, no event, no email row
    again = engine.scan(since=before + timedelta(days=1), today=after)
    assert again.alerts == 0 and again.triggers == 0 and again.coalesced == 0
    assert len(rows("SELECT id FROM stream_event")) == len(streamed)
    assert outbox.sweep_instant() == 0

    # no provider configured: every due row says so; nothing is marked emailed
    if instant:
        sent = outbox.send_due()
        assert sent == {"not_configured": len(instant)}
        assert {o["status"] for o in rows("SELECT status FROM email_outbox")} == {"not_configured"}
        error = rows("SELECT last_error FROM email_outbox LIMIT 1")[0]["last_error"]
        assert "POSTMARK_TOKEN or RESEND_API_KEY" in error
        assert not rows("SELECT 1 FROM alert WHERE emailed_at IS NOT NULL")
        state = rows("SELECT * FROM email_provider_state")[0]
        assert state["configured"] is False and "ALERT_FROM_EMAIL" in state["reason"]


def test_type_switch_and_entitlement(firms):
    before, after, _ = _status_change(SITE)
    off = {"types": {"cushion": False, "milestone": False, "power": False, "opposition": False, "tenant": False,
                     "price_or_mark": False, "construction_stall": False}}
    make_firm("org_e", [("e1", off, True)], [("site", SITE)])
    with psycopg.connect(TEST_URL, autocommit=True) as c:
        c.execute("UPDATE org_product SET ends_on = current_date - 1 WHERE org_id = 'org_c'")
        c.execute("INSERT INTO watchlist_item (org_id, kind, ref_id) VALUES ('org_c', 'site', %s)", (SITE,))
    engine.scan(since=before + timedelta(days=1), today=after)
    users = {a["user_id"] for a in _status_alerts(SITE)}
    assert "e1" not in users  # every alert type switched off
    assert "c1" not in users  # the firm's product ended yesterday
    assert {"a1", "a2", "b1"} <= users


def test_backfill_writes_alerts_only(firms):
    before, after, _ = _status_change(SITE)
    stats = engine.scan(since=before + timedelta(days=1), today=after, dry_run=True)
    alerts = rows("SELECT * FROM alert")
    assert alerts and stats.alerts == len(alerts) and stats.stream_events == 0
    assert all(a["backfill"] for a in alerts)
    assert all(a["read_at"] is not None for a in alerts)  # history: no "new" badge for it
    assert all(a["at"].astimezone(BUSINESS_TZ).date() == a["occurred_on"] for a in alerts)
    assert not rows("SELECT 1 FROM stream_event")
    assert outbox.sweep_instant() == 0
    assert outbox.build_digests() == {"daily_digest": 0, "weekly_digest": 0}
    # the live scan after a backfill does not alert the same changes again
    assert engine.scan(since=before + timedelta(days=1), today=after).alerts == 0


def test_repeats_of_one_matter_join_the_first_alert(firms):
    from marden.models.materiality import is_material

    events = rows("SELECT * FROM event WHERE subject_id = %s ORDER BY event_date, id", (SITE,))
    hits = [(e, is_material(e)) for e in events if e["kind"] != "catalyst"]
    pairs = [(x, y) for i, (x, mx) in enumerate(hits) if mx for (y, my) in hits[i + 1:]
             if my and my.codes[0] == mx.codes[0] and (y["event_date"] - x["event_date"]).days <= engine.MATTER_DAYS]
    if not pairs:
        pytest.skip("no two material events of one rule within 7 days on the copied site")
    first, second = pairs[0]
    engine.scan(since=first["event_date"], today=second["event_date"])
    code = is_material(first).codes[0]
    mine = rows("SELECT * FROM alert WHERE user_id = 'a1' AND trigger_key LIKE 'event:%%' AND rule_code = %s",
                (code,))
    joined = [a for a in mine if f"event:{second['id']}" in {s["key"] for s in a["sources"]}]
    assert len(joined) == 1 and joined[0]["trigger_key"] != f"event:{second['id']}"
    assert not [a for a in mine if a["trigger_key"] == f"event:{second['id']}"]
    trig = rows("SELECT * FROM alert_trigger WHERE trigger_key = %s", (f"event:{second['id']}",))
    assert trig and trig[0]["material"] and trig[0]["alerts"] == 0  # evaluated, joined, no new alert
