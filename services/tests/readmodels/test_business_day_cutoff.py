"""Dated reads count a row from the US Eastern business day it was learned (or created) on, not the UTC one.

Snapshots and as-of days are dated in US Eastern (`business_today`); the database session is UTC. A row learned
at 22:00 Eastern on 1 October (02:00 UTC on the 2nd) was known on 1 October, and one learned at 00:01 Eastern on
the 2nd was not. Comparing with `day + 1` as a date (midnight UTC, 20:00 EDT) hid the evening's rows until the
next day: facts (`known_facts` through `fact_asof`, `known_fact_rows`, the licence switch's own query, the
models' `pit` reads), events, campus rows. The rows are real ones copied from the local database (Abilene's
withdrawn-permit event and an SEC risk statement, Southgate's listing-deadline catalyst, the Stargate Abilene
campus); only their learned / created instant is set.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import psycopg
import pytest
from conftest import BASE_URL
from psycopg.rows import dict_row

from marden.core.jsonutil import jsonb
from marden.models import pit
from marden.readmodels import context
from marden.readmodels.context import building, day_end, known_fact_rows, known_facts
from marden.readmodels.views.facilities_common import campus_row, subject_events
from marden.readmodels.views.instrument_inputs import DAY_END_SQL
from marden.readmodels.views.sites_common import site_events
from marden.readmodels.views.sites_evidence import linked_events

DAY = date(2026, 10, 1)  # a past business day (a backfill), Eastern daylight time
EVENING = "2026-10-02 02:00+00"  # 22:00 EDT on 1 Oct: known on 1 Oct
AFTER_MIDNIGHT = "2026-10-02 04:01+00"  # 00:01 EDT on 2 Oct: not yet on 1 Oct
CASES = [(EVENING, True), (AFTER_MIDNIGHT, False)]
FACT_ID = 1080685  # site abilene, risk_evidence, SEC EDGAR, valid_from 2026-09-11
EVENT_ID = 54595  # plant n-abilene-onsite, 'TCEQ air permit 182267: withdrawn (project 401169)', 2026-04-20
CATALYST_ID = 35590  # site southgate, a listing deadline dated 2026-11-30 (after DAY: public once learned)


def _real(table: str, row_id: int | str) -> dict:
    try:
        with psycopg.connect(BASE_URL, connect_timeout=5, row_factory=dict_row) as src:
            row = src.execute(f"SELECT * FROM {table} WHERE id = %s", (row_id,)).fetchone()
    except psycopg.OperationalError as exc:
        pytest.skip(f"source database not reachable: {exc}")
    if row is None:
        pytest.skip(f"{table} {row_id} is not in the source database")
    return row


def _insert(conn, table: str, row: dict, **override) -> None:
    row = {**row, **override}
    cols = list(row)
    values = [jsonb(v) if isinstance(v, dict | list) and table == "fact" and c == "value" else v
              for c, v in row.items()]
    conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                 values)


def test_day_end_is_midnight_eastern():
    assert day_end(DAY) == datetime(2026, 10, 2, 4, 0, tzinfo=UTC)  # EDT
    assert day_end(date(2026, 12, 1)) == datetime(2026, 12, 2, 5, 0, tzinfo=UTC)  # EST
    with building(DAY):
        assert day_end() == day_end(DAY)


@pytest.mark.parametrize(("learned_at", "seen"), CASES)
def test_an_event_learned_in_the_eastern_evening_is_seen_that_day(conn, learned_at, seen):
    conn.execute("SET TIME ZONE 'UTC'")
    event = _real("event", EVENT_ID)
    _insert(conn, "event", event, learned_at=learned_at, document_id=None, fact_id=None)
    _insert(conn, "event", event, id=EVENT_ID + 10**9, subject_type="site", subject_id="abilene",
            dedupe_key=event["dedupe_key"] + ":site", learned_at=learned_at, document_id=None, fact_id=None)
    with building(DAY):
        assert any(r["id"] == EVENT_ID for r in subject_events(conn, "plant", "n-abilene-onsite")) is seen
        assert any(r["id"] == EVENT_ID for r in linked_events(conn, "plant", ["n-abilene-onsite"])) is seen
        assert any(r["title"] == event["title"] for r in site_events(conn, "abilene")) is seen
    # a dated-ahead event (a deadline) is public once learned: the models' point-in-time read
    catalyst = _real("event", CATALYST_ID)
    _insert(conn, "event", catalyst, learned_at=learned_at, document_id=None, fact_id=None)
    rows = pit.event_rows(conn, DAY, "site", ["southgate"])
    assert any(r["id"] == CATALYST_ID for r in rows) is seen


@pytest.mark.parametrize(("learned_at", "seen"), CASES)
def test_a_fact_learned_in_the_eastern_evening_is_known_that_day(conn, monkeypatch, learned_at, seen):
    conn.execute("SET TIME ZONE 'UTC'")
    fact = _real("fact", FACT_ID)
    _insert(conn, "fact", fact, learned_at=learned_at, document_id=None)
    field = fact["field"]
    with building(DAY):
        assert (field in known_facts(conn, "site", "abilene", [field])) is seen  # fact_asof(day)
        assert bool(known_fact_rows(conn, "site", "abilene", [field])[field]) is seen
        monkeypatch.setattr(context, "hidden_source_names", lambda: ["a source the licence switch hides"])
        assert (field in known_facts(conn, "site", "abilene", [field])) is seen  # the licence switch's query
    rows = pit.fact_rows(conn, DAY, "site", ["abilene"], [field], mode="known")
    assert any(r["id"] == FACT_ID for r in rows) is seen


@pytest.mark.parametrize(("created_at", "seen"), CASES)
def test_a_campus_created_in_the_eastern_evening_exists_that_day(conn, created_at, seen):
    conn.execute("SET TIME ZONE 'UTC'")
    campus = _real("campus", "stargate-abilene")
    conn.execute("INSERT INTO campus (id, name, created_at) VALUES (%s, %s, %s)",
                 (campus["id"], campus["name"], created_at))
    with building(DAY):
        assert (campus_row(conn, "stargate-abilene") is not None) is seen


@pytest.mark.parametrize(("learned_at", "seen"), CASES)
def test_the_instrument_views_known_clause_ends_the_day_in_eastern_time(conn, learned_at, seen):
    conn.execute("SET TIME ZONE 'UTC'")
    row = conn.execute(f"SELECT %(at)s::timestamptz < {DAY_END_SQL} AS known", {"at": learned_at, "day": DAY})
    assert row.fetchone()["known"] is seen
