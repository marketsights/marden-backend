"""Alert engine edge cases on real rows from the local database: a repeat of a matter that is more severe than
the alert it repeats is news of its own; an event dated ahead (a hearing, a termination announced for next
month) is alerted when Marden learns it, not when its day comes, and a backfill never dates an alert in the
future; one trigger that fails is retried on the next scan and never stops the triggers behind it; the
engine's model state never moves back to an older output."""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta

import psycopg
import pytest
from alert_rows import copy_sites, make_firm, rows
from conftest import TEST_URL
from test_alert_engine import SITE

from marden.alerts import engine
from marden.alerts import triggers as tr
from marden.core import db, events
from marden.models.materiality import is_material
from marden.readmodels.context import BUSINESS_TZ, business_today

OPPOSED = "coreweave-ddtl-3"  # carries graded and ungraded opposition events a day apart (DCD, Oct 17-18)


def _learned_now(event_id: int) -> None:
    with psycopg.connect(TEST_URL, autocommit=True) as c:
        c.execute("UPDATE event SET learned_at = clock_timestamp() WHERE id = %s", (event_id,))


def _start_engine(day: date) -> None:
    """A one-day backfill: the engine's first evaluation (the line between history and news)."""
    engine.scan(since=day, today=day, dry_run=True)
    assert rows("SELECT 1 FROM alert_trigger LIMIT 1"), "the backfill evaluated nothing"


def _holding(key: str, user: str) -> list[dict]:
    """The user's alerts that carry the trigger: its own, or the earlier alert of the matter it joined."""
    return rows("""SELECT * FROM alert WHERE user_id = %s AND (trigger_key = %s
                     OR sources @> jsonb_build_array(jsonb_build_object('key', %s::text)))""", (user, key, key))


def test_a_more_severe_repeat_is_not_folded_into_a_milder_alert():
    if OPPOSED not in copy_sites([OPPOSED])["sites"]:
        pytest.skip(f"{OPPOSED} is not in the source database")
    make_firm("org_a", [("a1", None, True)], [("site", OPPOSED)])
    opp = rows("SELECT * FROM event WHERE subject_id = %s AND kind = 'opposition' ORDER BY event_date, id", (OPPOSED,))
    pairs = [(mild, graded) for mild in opp for graded in opp
             if mild["severity"] in (None, "none") and graded["severity"] in ("red", "amber")
             and graded["event_date"] < mild["event_date"]
             and (mild["event_date"] - graded["event_date"]).days <= engine.MATTER_DAYS
             and not is_material(mild) and not is_material(graded)]
    if not pairs:
        pytest.skip("no graded opposition event dated before an ungraded one within 7 days")
    mild, graded = pairs[0]

    engine.scan(since=mild["event_date"], today=mild["event_date"])  # the ungraded article: a green OP alert
    first = rows("SELECT * FROM alert WHERE user_id = 'a1' AND trigger_key = %s", (f"event:{mild['id']}",))
    assert len(first) == 1 and first[0]["severity"] == "green" and first[0]["rule_code"] == "OP"

    engine.scan(since=graded["event_date"], today=graded["event_date"])  # the graded one, the day before
    own = rows("SELECT * FROM alert WHERE user_id = 'a1' AND trigger_key = %s", (f"event:{graded['id']}",))
    assert len(own) == 1 and own[0]["severity"] == graded["severity"]  # its own alert, not a joined source
    assert f"event:{graded['id']}" not in {s["key"] for s in first[0]["sources"]}
    after = rows("SELECT sources FROM alert WHERE id = %s", (first[0]["id"],))[0]["sources"]
    assert after == first[0]["sources"]


def _code(event: dict) -> str | None:
    """The rule an event alerts under (a material code, else OP for an opposition event)."""
    found = is_material(event)
    return found.codes[0] if found else ("OP" if event["kind"] == "opposition" else None)


def _isolated_material_event(site: str) -> dict | None:
    """An alerting event of the site with no other alerting event of its rule within 7 days (so it is not
    joined to another alert of the matter)."""
    evs = [e for e in rows("SELECT * FROM event WHERE subject_id = %s AND kind <> 'catalyst' ORDER BY event_date, id",
                           (site,)) if _code(e)]
    for e in evs:
        near = [o for o in evs if o["id"] != e["id"] and _code(o) == _code(e)
                and abs((o["event_date"] - e["event_date"]).days) <= engine.MATTER_DAYS]
        if not near:
            return e
    return None


@pytest.fixture
def watched_jupiter():
    if SITE not in copy_sites([SITE])["sites"]:
        pytest.skip("jupiter is not in the source database")
    make_firm("org_a", [("a1", None, True)], [("site", SITE)])
    first = rows("SELECT min(event_date) AS d FROM event WHERE kind <> 'catalyst'")[0]["d"]
    if first is None:
        pytest.skip("no events copied")
    return first


def test_an_event_dated_ahead_is_alerted_when_learned(watched_jupiter):
    ev = _isolated_material_event(SITE)
    if ev is None or ev["event_date"] - timedelta(days=5) <= watched_jupiter:
        pytest.skip("no material event on its own in its rule's 7 days, after the site's first event")
    key, today = f"event:{ev['id']}", ev["event_date"] - timedelta(days=5)  # the event is dated 5 days ahead
    _start_engine(watched_jupiter)

    engine.scan(today=today)  # learned before the engine started: history, even dated ahead
    assert not rows("SELECT 1 FROM alert_trigger WHERE trigger_key = %s", (key,))

    _learned_now(ev["id"])
    engine.scan(today=today)
    trig = rows("SELECT * FROM alert_trigger WHERE trigger_key = %s", (key,))
    assert trig and trig[0]["material"] and not trig[0]["dry_run"] and trig[0]["occurred_on"] == ev["event_date"]
    mine = _holding(key, "a1")
    assert len(mine) == 1 and mine[0]["trigger_key"] == key and not mine[0]["backfill"]
    assert mine[0]["occurred_on"] == ev["event_date"] and mine[0]["read_at"] is None
    streamed = {s["payload"]["alert"]["id"] for s in rows("SELECT payload FROM stream_event")}
    assert str(mine[0]["id"]) in streamed

    count = len(rows("SELECT id FROM alert"))
    engine.scan(today=ev["event_date"])  # its day arrives: nothing new
    assert len(rows("SELECT id FROM alert")) == count


def test_a_backfill_never_dates_an_alert_in_the_future(watched_jupiter):
    ev = _isolated_material_event(SITE)
    if ev is None or ev["event_date"] - timedelta(days=5) <= watched_jupiter:
        pytest.skip("no material event on its own in its rule's 7 days, after the site's first event")
    today = ev["event_date"] - timedelta(days=5)
    _start_engine(watched_jupiter)
    _learned_now(ev["id"])
    engine.scan(since=today, today=today, dry_run=True)
    mine = rows("SELECT * FROM alert WHERE trigger_key = %s", (f"event:{ev['id']}",))
    assert mine and all(a["backfill"] and a["occurred_on"] == ev["event_date"] for a in mine)
    assert all(a["at"].astimezone(BUSINESS_TZ).date() == today for a in mine)


def test_a_failing_trigger_is_retried_and_does_not_stop_the_others(caplog, monkeypatch):
    if SITE not in copy_sites([SITE])["sites"]:
        pytest.skip("jupiter is not in the source database")
    make_firm("org_a", [("a1", None, True)], [("site", SITE)])
    since = rows("SELECT min(event_date) AS d FROM event WHERE kind <> 'catalyst'")[0]["d"]
    after = business_today()  # the copied history up to today: several alerting triggers
    real_emit, calls = events.emit, []

    def emit_once_refused(payload, *args, **kwargs):  # the first live alert event is refused (bad payload)
        calls.append(payload)
        if len(calls) == 1:
            raise events.InvalidStreamEvent("alert.headline: refused by the test")
        return real_emit(payload, *args, **kwargs)

    monkeypatch.setattr(engine.events, "emit", emit_once_refused)
    with caplog.at_level(logging.ERROR, logger="marden.alerts.engine"):
        stats = engine.scan(since=since, today=after)
    if len(calls) < 2:
        pytest.skip("fewer than two alerting triggers in the copied window")
    assert stats.errors == 1  # and the triggers behind it went on: their alerts were emitted
    failed = re.search(r"trigger (\S+) failed", caplog.text).group(1)
    assert not rows("SELECT 1 FROM alert_trigger WHERE trigger_key = %s", (failed,))  # rolled back: unevaluated
    assert not rows("SELECT 1 FROM alert WHERE trigger_key = %s", (failed,))
    made = len(rows("SELECT id FROM alert"))
    assert made >= 1

    again = engine.scan(since=since, today=after)  # retried, now delivered
    assert again.errors == 0 and again.triggers == 1 and again.alerts + again.coalesced == 1
    assert rows("SELECT 1 FROM alert_trigger WHERE trigger_key = %s", (failed,))
    assert len(_holding(failed, "a1")) == 1  # its own alert, or joined to the matter's alert meanwhile
    assert engine.scan(since=since, today=after).alerts == 0  # and then never again


def test_a_failed_model_change_holds_back_that_subjects_later_changes(monkeypatch):
    def change(as_of: date) -> tr.Trigger:
        state = tr.StateUpdate("status", "site", SITE, as_of, f"h{as_of}", {"chain": []})
        return tr.Trigger(key=f"model:status:site:{SITE}:{as_of}", kind="model", subject_type="site",
                          subject_id=SITE, occurred_on=as_of, state=state)

    day = date(2026, 10, 1)
    pending = [change(day), change(day + timedelta(days=1)), tr.Trigger(
        key="event:1", kind="event", subject_type="site", subject_id="other", occurred_on=day)]
    seen: list[str] = []

    def process(trigger, *args):
        seen.append(trigger.key)
        if trigger.key == pending[0].key:
            raise ValueError("refused by the test")

    monkeypatch.setattr(tr, "event_triggers", lambda conn, start, today: iter(pending))
    monkeypatch.setattr(tr, "fact_triggers", lambda conn, start, today: iter(()))
    monkeypatch.setattr(tr, "model_triggers", lambda conn, start, today: iter(()))
    monkeypatch.setattr(engine, "_process", process)
    stats = engine.scan(today=day)
    assert stats.errors == 1 and seen == [pending[0].key, "event:1"]  # the later change waits; others go on


def test_model_state_never_moves_back_to_an_older_output():
    newer, older = date(2026, 10, 20), date(2026, 10, 19)
    with db.transaction() as conn:
        for as_of in (newer, older):
            state = tr.StateUpdate("price", "instrument", "x", as_of, f"h{as_of}", {"price": 90.0})
            engine._save_state(conn, tr.Trigger(key="k", kind="model", subject_type="instrument", subject_id="x",
                                                occurred_on=as_of, state=state))
    kept = rows("SELECT as_of, output_hash FROM alert_state")
    assert kept == [{"as_of": newer, "output_hash": f"h{newer}"}]
