from __future__ import annotations

import pytest

from marden.core import events
from marden.core.events import InvalidStreamEvent, Listener

SOURCED = {"value": 97.4, "tag": "M", "asOf": "2026-10-20", "methodVersion": "price v0.3"}

VALID = [
    {"type": "alert", "alert": {"id": "a1", "at": "2026-10-21T10:00:00+00:00", "headline": "Cushion under 3 months",
                                "tag": "M", "ruleLabel": "Cushion < 3m", "refUrl": "/sites/abilene", "read": False,
                                "emailed": False}},
    {"type": "status", "siteSlug": "abilene", "chain": [
        {"role": "tenant", "party": "Oracle", "status": "amber", "reason": "Rating watch", "ruleCode": "T2",
         "fact": {"value": "Watch negative", "tag": "R", "asOf": "2026-10-01", "sourceName": "Reuters"}}]},
    {"type": "cushion", "siteSlug": "abilene", "cushionMonths": {"value": None, "tag": "M", "asOf": "2026-10-20",
                                                                 "methodVersion": "cushion v0.1"}},
    {"type": "price", "instrumentId": "EX-ABILENE-000", "price": SOURCED, "change1w": {**SOURCED, "value": -0.6}},
    {"type": "mark", "instrumentId": "EX-ABILENE-000", "holder": "PGIM", "mark": {"value": 99.5, "tag": "F",
                                                                                  "asOf": "2026-06-30"}},
    {"type": "pipeline", "source": "sec_edgar", "status": "down", "at": "2026-10-21T10:00:00+00:00"},
    {"type": "campus", "campusId": "stargate-abilene"},
    {"type": "plant", "plantId": "abilene-gas"},
]


@pytest.mark.parametrize("payload", VALID, ids=[p["type"] for p in VALID])
def test_valid_payloads(payload):
    assert events.validate(payload) == payload


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"type": "weather"}, "unknown stream event type"),
        ({"campusId": "x"}, "unknown stream event type"),
        ({"type": "campus"}, "campus.campusId: missing"),
        ({"type": "campus", "campusId": "x", "extra": 1}, "unknown keys"),
        ({"type": "price", "instrumentId": "i", "price": {**SOURCED, "tag": "Q"}, "change1w": SOURCED}, "tag"),
        ({"type": "price", "instrumentId": "i", "price": {"value": 1, "tag": "M", "asOf": "2026-10-20"},
          "change1w": SOURCED}, "methodVersion"),
        ({"type": "mark", "instrumentId": "i", "holder": "h", "mark": {"value": "high", "tag": "F",
                                                                         "asOf": "2026-10-20"}}, "number"),
        ({"type": "pipeline", "source": "s", "status": "sleeping", "at": "2026-10-21T10:00:00"}, "one of"),
        ({"type": "cushion", "siteSlug": "a", "cushionMonths": {"value": 3, "tag": "F", "asOf": "Oct 2026"}},
         "ISO date"),
    ],
)
def test_invalid_payloads(payload, message):
    with pytest.raises(InvalidStreamEvent, match=message):
        events.validate(payload)


def test_emit_refuses_invalid_payload_without_writing(committed):
    with pytest.raises(InvalidStreamEvent):
        events.emit({"type": "campus"})
    assert committed.execute("SELECT count(*) AS n FROM stream_event").fetchone()["n"] == 0


def test_emit_notifies_a_listener(committed):
    with Listener() as listener:
        event_id = events.emit({"type": "campus", "campusId": "stargate-abilene"}, org_id="org_test")
        received = []
        for announced in listener.ids(timeout=10):
            received.append(announced)
            if announced == event_id:
                break
    assert event_id in received
    row = committed.execute("SELECT org_id, payload FROM stream_event WHERE id = %s", (event_id,)).fetchone()
    assert row == {"org_id": "org_test", "payload": {"type": "campus", "campusId": "stargate-abilene"}}


def test_no_notify_when_the_transaction_rolls_back(committed):
    from marden.core import db

    with Listener() as listener:
        with pytest.raises(RuntimeError), db.transaction() as conn:
            rolled_back = events.emit({"type": "plant", "plantId": "p1"}, conn=conn)
            raise RuntimeError("abort")
        kept = events.emit({"type": "plant", "plantId": "p2"})
        seen = []
        for announced in listener.ids(timeout=10):
            seen.append(announced)
            if announced == kept:
                break
    assert kept in seen and rolled_back not in seen
