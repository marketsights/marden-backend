"""`lastChange` on the Data centers rows (dc_row, dc_detail; facilities_common.last_change) on real rows.

Copied from the local `marden` database as the loaders and collectors stored them (document and fact
references cleared, since raw documents are not copied):

- `goodnight`: Epoch AI and research-archive facts (status "Under construction", the archive rule's
  value, tag M, valid_from 2026-10-19) and the two ERCOT GIS records `ercot_gis` published on it for
  queue entry 23INR0637 (2026-01-02 and 2026-07-01). The regression: once those older records arrived,
  rebuilding its row moved lastChange back from 2026-10-19 to 2026-07-01, because the History tab's
  first values counted only while the campus had no record.
- `terawulf-lake-mariner`: Epoch value changes (newest 2026-05-31) and its NYISO large-load queue
  listing (F, the 2026-09-30 queue file), which lastChange used to ignore.
- `anthropic-amazon-new-carlisle` and `vantage-lighthouse`: changes of the same day (Epoch's monthly
  values learned in one batch, the archive's status learned after it; FERC eLibrary records learned in
  two runs) for the tie-breaks.
- Plant `n-oh-flagship` (plant_row, plant_detail; facilities_plants.plant_changes): its research-archive
  facts (status, tag M, valid_from 2026-10-20) and its FERC eLibrary records on ER27-126, some older and
  one newer (2026-10-21) than the status.

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
from marden.readmodels.views import facilities_campus, facilities_plants  # noqa: F401 (registers builders)
from marden.readmodels.views.facilities_campus_tabs import HISTORY_FIELDS, campus_changes
from marden.readmodels.views.facilities_common import (
    VALUE_CHANGE,
    event_changes,
    last_change,
    queue_changes,
    subject_events,
)

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


def current(campus_id: str, field: str) -> dict[str, Any]:
    return copied_rows(
        "SELECT * FROM fact_current WHERE subject_type = 'campus' AND subject_id = %s AND field = %s",
        (campus_id, field),
    )[0]


def events_newest_first(campus_id: str) -> list[dict[str, Any]]:
    return copied_rows(
        """SELECT * FROM event WHERE subject_type = 'campus' AND subject_id = %s AND status = 'published'
                AND event_date <= CURRENT_DATE ORDER BY event_date DESC, learned_at DESC, id DESC""",
        (campus_id,),
    )


def event_evidence(ev: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": ev["event_date"].isoformat(),
        "text": ev["title"],
        "tag": ev["tag"],
        "sourceName": ev["source_name"],
        "sourceUrl": ev["source_url"] or "",
    }


def status_evidence(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": row["valid_from"].isoformat(),
        "text": f"Status: {row['value']}",
        "tag": row["tag"],
        "sourceName": row["source_name"],
        "sourceUrl": row["source_url"] or "",
    }


def in_changes(campus_id: str, kinds: Sequence[int] = ()) -> Any:
    """last_change over the campus's History changes (only `kinds`, if given) and queue listings."""
    with building(business_today()), db.transaction() as conn:
        changes = [c for c in campus_changes(conn, campus_id) if not kinds or c.kind in kinds]
        return last_change(changes + queue_changes(conn, "campus", campus_id), {})


def test_an_older_record_never_moves_last_change_back(campuses):
    status = current(GOODNIGHT, "status")
    records = events_newest_first(GOODNIGHT)
    assert len(records) == 2 and {r["source_name"] for r in records} == {"ERCOT"}, "the two ERCOT GIS records"
    # The archive's status (2026-10-19) is newer than both records (2026-07-01, 2026-01-02).
    assert status["valid_from"] > records[0]["event_date"] > records[1]["event_date"]
    newest = status_evidence(status)
    # Before ercot_gis linked 23INR0637, after its older record, and with both records (the regression:
    # this used to be the 2026-07-01 record): the newest dated change stays the 2026-10-19 status.
    assert build("dc_row", GOODNIGHT, without_events=[r["id"] for r in records])["lastChange"] == newest
    assert build("dc_row", GOODNIGHT, without_events=[records[0]["id"]])["lastChange"] == newest
    for view in ("dc_row", "dc_detail"):
        assert build(view, GOODNIGHT)["lastChange"] == newest, view
    # It is the History tab's newest entry; the records stay on the Records tab.
    history = build("dc_tab:history", GOODNIGHT)
    assert (history[0]["date"], history[0]["field"], history[0]["evidence"]) == (newest["date"], "status", newest)
    texts = {r["text"] for r in build("dc_tab:records", GOODNIGHT)}
    assert {r["title"] for r in records} <= texts


def test_a_large_load_queue_listing_is_a_change(campuses):
    listing = current(TERAWULF, "large_load_queue_nyiso")
    newest_value = copied_rows(  # the newest value change (before -> after) of a History field
        """SELECT max(valid_from) AS d FROM (
             SELECT valid_from, value, lag(value) OVER (PARTITION BY field ORDER BY valid_from, id) AS prev
               FROM fact WHERE subject_type = 'campus' AND subject_id = %s AND status = 'published'
                AND field = ANY(%s)) x WHERE prev IS NOT NULL AND prev <> value""",
        (TERAWULF, list(HISTORY_FIELDS)),
    )[0]["d"]
    assert listing["tag"] == "F" and listing["valid_from"] > newest_value
    entry = listing["value"][0]
    expected = {
        "date": listing["valid_from"].isoformat(),
        "text": f"Large-load queue: NYISO queue {entry['queue_id']} ({entry['status']})",
        "tag": "F",
        "sourceName": listing["source_name"],
        "sourceUrl": listing["source_url"],
    }
    # The 2026-09-30 listing outranks every older value change (it used to be ignored) ...
    assert in_changes(TERAWULF, kinds=[VALUE_CHANGE]) == expected
    # ... and the row shows whichever dated change is newest overall.
    status = current(TERAWULF, "status")
    row = build("dc_row", TERAWULF)["lastChange"]
    assert row == (status_evidence(status) if status["valid_from"] > listing["valid_from"] else expected)
    assert build("dc_detail", TERAWULF)["lastChange"] == row


def test_same_day_changes_rank_by_learned_at_then_field_then_id(campuses):
    # One day, two batches: Epoch's monthly values, then the archive's status (learned later).
    status = current(NEW_CARLISLE, "status")
    epoch = copied_rows(
        """SELECT field, learned_at FROM fact WHERE subject_type = 'campus' AND subject_id = %s AND valid_from = %s
              AND source_name = 'Epoch AI' AND field IN ('operating_mw', 'operating_it_mw', 'buildings_operational')""",
        (NEW_CARLISLE, status["valid_from"]),
    )
    assert {r["field"] for r in epoch} == {"operating_mw", "operating_it_mw", "buildings_operational"}
    assert len({r["learned_at"] for r in epoch}) == 1 and status["learned_at"] > epoch[0]["learned_at"]
    assert build("dc_row", NEW_CARLISLE)["lastChange"] == status_evidence(status)  # later learned wins
    # Within Epoch's batch (same day, same instant): the most material field's change.
    first = in_changes(NEW_CARLISLE, kinds=[VALUE_CHANGE])
    assert first["date"] == status["valid_from"].isoformat() and first["text"].startswith("Operating MW: ")
    assert in_changes(NEW_CARLISLE, kinds=[VALUE_CHANGE]) == first  # deterministic

    # Two FERC records of one day learned in two runs: the later-learned one, in either order.
    by_day: dict[Any, list[dict[str, Any]]] = {}
    for r in events_newest_first(LIGHTHOUSE):
        by_day.setdefault(r["event_date"], []).append(r)
    pair = next(rs for rs in by_day.values() if len({r["learned_at"] for r in rs}) > 1)
    with building(business_today()), db.transaction() as conn:
        rows = [e for e in subject_events(conn, "campus", LIGHTHOUSE) if e["event_date"] == pair[0]["event_date"]]
    assert last_change(event_changes(rows), {}) == event_evidence(pair[0])
    assert last_change(event_changes(list(reversed(rows))), {}) == event_evidence(pair[0])
    # Two records learned at the same instant: the higher id.
    same = next(rs for rs in by_day.values() if len(rs) > 1 and len({r["learned_at"] for r in rs}) == 1)
    with building(business_today()), db.transaction() as conn:
        rows = [e for e in subject_events(conn, "campus", LIGHTHOUSE) if e["event_date"] == same[0]["event_date"]]
    assert last_change(event_changes(rows), {}) == event_evidence(max(same, key=lambda r: r["id"]))


OH_FLAGSHIP = "n-oh-flagship"


def copy_plant(plant_id: str) -> None:
    """Copy the plant, its units, the facts on both and the plant's events."""
    try:
        src = psycopg.connect(BASE_URL, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"source database not reachable: {exc}")
    with src, psycopg.connect(TEST_URL) as dst:
        if src.execute("SELECT 1 FROM power_plant WHERE id = %s", (plant_id,)).fetchone() is None:
            pytest.skip(f"not in the source database: {plant_id}")
        p: dict[str, Any] = {"id": plant_id}
        p["units"] = [r[0] for r in src.execute("SELECT id FROM power_unit WHERE plant_id = %(id)s", p)]
        _copy(src, dst, "power_plant", "id = %(id)s", p)
        _copy(src, dst, "power_unit", "plant_id = %(id)s", p)
        _copy(
            src,
            dst,
            "fact",
            """(subject_type = 'plant' AND subject_id = %(id)s)
               OR (subject_type = 'unit' AND subject_id = ANY(%(units)s))""",
            p,
            nulls=("document_id",),
        )
        _copy(src, dst, "event", "subject_type = 'plant' AND subject_id = %(id)s", p, nulls=("document_id", "fact_id"))
        for table in ("fact", "event"):
            _reset_sequence(dst, table)
        dst.commit()


def newest_fact_change(plant_id: str) -> Any:
    """The newest date of a plant History change, by SQL: a new value of a History field, or the first
    value of status / air permit (the fields whose first value is a History entry)."""
    return copied_rows(
        """SELECT max(valid_from) AS d FROM (
             SELECT field, valid_from, value,
                    lag(value) OVER (PARTITION BY field ORDER BY valid_from, id) AS prev,
                    row_number() OVER (PARTITION BY field ORDER BY valid_from, id) AS n
               FROM fact WHERE subject_type = 'plant' AND subject_id = %s AND status = 'published'
                AND valid_from <= CURRENT_DATE AND (valid_to IS NULL OR valid_to > CURRENT_DATE)
                AND field = ANY(%s)) x
            WHERE (n = 1 AND field IN ('status', 'air_permit')) OR (prev IS NOT NULL AND prev <> value)""",
        (plant_id, list(facilities_plants.HISTORY_FIELDS)),
    )[0]["d"]


@pytest.fixture
def plant() -> None:
    copy_plant(OH_FLAGSHIP)


def test_plant_last_change_ranks_records_and_history_on_one_scale(plant):
    records = copied_rows(
        """SELECT * FROM event WHERE subject_type = 'plant' AND subject_id = %s AND status = 'published'
                AND event_date <= CURRENT_DATE ORDER BY event_date DESC, learned_at DESC, id DESC""",
        (OH_FLAGSHIP,),
    )
    history_newest = newest_fact_change(OH_FLAGSHIP)
    newer = [r for r in records if r["event_date"] > history_newest]
    older = [r for r in records if r["event_date"] < history_newest]
    assert newer and older, "FERC records both after and before the plant's newest History change"
    # The newest record is the plant's lastChange, on the row and the detail page alike.
    row = build("plant_row", OH_FLAGSHIP)["lastChange"]
    assert row == event_evidence(records[0])
    assert build("plant_detail", OH_FLAGSHIP)["lastChange"] == row
    # Without the newer records the History change is the newest; the older records never pull it back,
    # with all of them present or with only the oldest one.
    without_newer = build("plant_row", OH_FLAGSHIP, without_events=[r["id"] for r in newer])["lastChange"]
    assert without_newer["date"] == history_newest.isoformat()
    only_oldest = [r["id"] for r in records if r["id"] != older[-1]["id"]]
    assert build("plant_row", OH_FLAGSHIP, without_events=only_oldest)["lastChange"] == without_newer
    assert build("plant_row", OH_FLAGSHIP, without_events=[r["id"] for r in records])["lastChange"] == without_newer
