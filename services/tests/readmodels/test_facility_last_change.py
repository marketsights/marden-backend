"""`lastChange` on the Data centers rows (dc_row, dc_detail; facilities_common.last_change) on real rows.

Copied from the local `marden` database as the loaders and collectors stored them (document and fact
references cleared, since raw documents are not copied):

- `goodnight`: Epoch AI and research-archive facts (its status is the archive rule's first value, tag M,
  valid_from 2026-10-19) and the two ERCOT GIS records `ercot_gis` published on it for queue entry
  23INR0637 (2026-01-02, 2026-07-01). Rebuilding its row after those records arrived moved lastChange
  from 2026-10-19 to 2026-07-01: the 2026-10-19 value was the first-value fallback, not a change.
- `terawulf-lake-mariner`: Epoch value changes (newest 2026-05-31) and its NYISO large-load queue
  listing (F, the 2026-09-30 queue file), which lastChange used to ignore.
- `anthropic-amazon-new-carlisle` and `vantage-lighthouse`: changes of the same day (Epoch's monthly
  values learned in one batch; FERC eLibrary records learned in two runs) for the tie-breaks.

Expectations are read from the copied rows with SQL, independent of the builders. Rows are only
removed (inside a rolled-back transaction) to rebuild the state before a record arrived; nothing is
made up.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg
import pytest
from conftest import BASE_URL, TEST_URL
from real_rows import _copy, _reset_sequence, copied_rows

from marden.core import db
from marden.readmodels import business_today
from marden.readmodels.context import building
from marden.readmodels.registry import get_builder
from marden.readmodels.views import facilities_campus  # noqa: F401 (registers builders)
from marden.readmodels.views.facilities_campus_tabs import HISTORY_FIELDS
from marden.readmodels.views.facilities_common import event_changes, last_change, subject_events

GOODNIGHT, TERAWULF, NEW_CARLISLE, LIGHTHOUSE = (
    "goodnight",
    "terawulf-lake-mariner",
    "anthropic-amazon-new-carlisle",
    "vantage-lighthouse",
)
CAMPUSES = (GOODNIGHT, TERAWULF, NEW_CARLISLE, LIGHTHOUSE)


def copy_campuses(ids: Sequence[str]) -> None:
    """Copy the campuses, their buildings, the facts on both and the campus events."""
    try:
        src = psycopg.connect(BASE_URL, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"source database not reachable: {exc}")
    with src, psycopg.connect(TEST_URL) as dst:
        found = [r[0] for r in src.execute("SELECT id FROM campus WHERE id = ANY(%s)", (list(ids),))]
        if sorted(found) != sorted(ids):
            pytest.skip(f"not in the source database: {sorted(set(ids) - set(found))}")
        p: dict[str, Any] = {"ids": list(ids)}
        p["buildings"] = [r[0] for r in src.execute("SELECT id FROM building WHERE campus_id = ANY(%(ids)s)", p)]
        _copy(src, dst, "campus", "id = ANY(%(ids)s)", p)
        _copy(src, dst, "building", "campus_id = ANY(%(ids)s)", p)
        _copy(
            src,
            dst,
            "fact",
            """(subject_type = 'campus' AND subject_id = ANY(%(ids)s))
               OR (subject_type = 'building' AND subject_id = ANY(%(buildings)s))""",
            p,
            nulls=("document_id",),
        )
        _copy(
            src,
            dst,
            "event",
            "subject_type = 'campus' AND subject_id = ANY(%(ids)s)",
            p,
            nulls=("document_id", "fact_id"),
        )
        for table in ("fact", "event"):
            _reset_sequence(dst, table)
        dst.commit()


@pytest.fixture
def campuses() -> None:
    copy_campuses(CAMPUSES)


def build(view: str, key: str, *, without_events: Sequence[int] = ()) -> Any:
    """The builder's document for `key` today; `without_events` are removed first (rolled back)."""
    b = get_builder(view)
    assert b is not None and b.build_one is not None, view
    doc: Any = None
    with building(business_today()), db.transaction() as conn:
        if without_events:
            conn.execute("DELETE FROM event WHERE id = ANY(%s)", (list(without_events),))
        doc = b.build_one(conn, key)
        raise psycopg.Rollback()
    return doc


def events_newest_first(campus_id: str) -> list[dict[str, Any]]:
    return copied_rows(
        """SELECT * FROM event WHERE subject_type = 'campus' AND subject_id = %s AND status = 'published'
                AND event_date <= CURRENT_DATE ORDER BY event_date DESC, learned_at DESC, id DESC""",
        (campus_id,),
    )


def evidence(ev: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": ev["event_date"].isoformat(),
        "text": ev["title"],
        "tag": ev["tag"],
        "sourceName": ev["source_name"],
        "sourceUrl": ev["source_url"] or "",
    }


def test_goodnight_shows_its_newest_change_not_a_newer_first_value(campuses):
    status = copied_rows(
        "SELECT * FROM fact_current WHERE subject_type = 'campus' AND subject_id = %s AND field = 'status'",
        (GOODNIGHT,),
    )[0]
    records = events_newest_first(GOODNIGHT)
    assert len(records) >= 2 and all(r["source_name"] == "ERCOT" for r in records), "the two ERCOT GIS records"
    newest, older = records[0], records[1]
    # The status is the archive rule's first value: newer than both records, but not a change.
    assert status["tag"] == "M" and status["valid_from"] > newest["event_date"]
    history = build("dc_tab:history", GOODNIGHT)
    assert (history[0]["field"], history[0]["before"], history[0]["date"]) == (
        "status",
        None,
        status["valid_from"].isoformat(),
    )
    assert not [h for h in history if h["before"] is not None], "goodnight has no value change"

    for view in ("dc_row", "dc_detail"):
        assert build(view, GOODNIGHT)["lastChange"] == evidence(newest), view
    # Before the newer record: the older record still wins over the newer first value.
    assert build("dc_row", GOODNIGHT, without_events=[newest["id"]])["lastChange"] == evidence(older)
    # Before ercot_gis linked 23INR0637: no change at all, so the newest first value shows.
    fallback = build("dc_row", GOODNIGHT, without_events=[r["id"] for r in records])["lastChange"]
    assert fallback == {
        "date": status["valid_from"].isoformat(),
        "text": f"Status: {status['value']}",
        "tag": "M",
        "sourceName": status["source_name"],
        "sourceUrl": status["source_url"] or "",
    }


def test_a_large_load_queue_listing_is_a_change(campuses):
    listing = copied_rows(
        """SELECT * FROM fact_current WHERE subject_type = 'campus' AND subject_id = %s
              AND field = 'large_load_queue_nyiso'""",
        (TERAWULF,),
    )[0]
    newest_value = copied_rows(  # the newest value change (before -> after) of a History field
        """SELECT max(valid_from) AS d FROM (
             SELECT valid_from, value, lag(value) OVER (PARTITION BY field ORDER BY valid_from, id) AS prev
               FROM fact WHERE subject_type = 'campus' AND subject_id = %s AND status = 'published'
                AND field = ANY(%s)) x WHERE prev IS NOT NULL AND prev <> value""",
        (TERAWULF, list(HISTORY_FIELDS)),
    )[0]["d"]
    assert listing["tag"] == "F" and listing["valid_from"] > newest_value
    entry = listing["value"][0]
    row = build("dc_row", TERAWULF)
    assert row["lastChange"] == {
        "date": listing["valid_from"].isoformat(),
        "text": f"Large-load queue: NYISO queue {entry['queue_id']} ({entry['status']})",
        "tag": "F",
        "sourceName": listing["source_name"],
        "sourceUrl": listing["source_url"],
    }
    assert build("dc_detail", TERAWULF)["lastChange"] == row["lastChange"]


def test_same_day_changes_rank_by_learned_at_then_field_then_id(campuses):
    # Epoch's values of one month, learned in one batch: the most material field's change shows.
    day = copied_rows(
        """SELECT valid_from, learned_at FROM fact WHERE subject_type = 'campus' AND subject_id = %s
              AND source_name = 'Epoch AI' AND field = 'operating_mw' AND status = 'published'
            ORDER BY valid_from DESC LIMIT 1""",
        (NEW_CARLISLE,),
    )[0]
    same = copied_rows(
        """SELECT DISTINCT field FROM fact WHERE subject_type = 'campus' AND subject_id = %s AND valid_from = %s
              AND learned_at = %s AND field IN ('operating_mw', 'operating_it_mw', 'buildings_operational')""",
        (NEW_CARLISLE, day["valid_from"], day["learned_at"]),
    )
    assert len(same) == 3, "operating MW, operating IT MW and buildings changed in the same Epoch batch"
    first = build("dc_row", NEW_CARLISLE)["lastChange"]
    assert first["date"] == day["valid_from"].isoformat() and first["text"].startswith("Operating MW: ")
    assert build("dc_row", NEW_CARLISLE)["lastChange"] == first  # deterministic

    # Two FERC records of one day learned in two runs: the later-learned one ranks first.
    records = events_newest_first(LIGHTHOUSE)
    by_day: dict[Any, list[dict[str, Any]]] = {}
    for r in records:
        by_day.setdefault(r["event_date"], []).append(r)
    pair = next(rs for rs in by_day.values() if len({r["learned_at"] for r in rs}) > 1)
    with building(business_today()), db.transaction() as conn:
        rows = [e for e in subject_events(conn, "campus", LIGHTHOUSE) if e["event_date"] == pair[0]["event_date"]]
    assert last_change(event_changes(rows), {}) == evidence(pair[0])
    assert last_change(event_changes(list(reversed(rows))), {}) == evidence(pair[0])
    # The row: the newest records day; two records learned at the same instant -> the higher id.
    top = [r for r in records if r["event_date"] == records[0]["event_date"]]
    top = [r for r in top if r["learned_at"] == top[0]["learned_at"]]
    assert build("dc_row", LIGHTHOUSE)["lastChange"]["date"] >= records[0]["event_date"].isoformat()
    if build("dc_row", LIGHTHOUSE)["lastChange"]["date"] == records[0]["event_date"].isoformat():
        assert build("dc_row", LIGHTHOUSE)["lastChange"] == evidence(max(top, key=lambda r: r["id"]))
