from __future__ import annotations

from datetime import date

import pytest

from marden.core import db
from marden.core.facts import FactWriter, current_facts, sourced
from marden.readmodels import builder, get_builder, mark_dirty, rebuild_all, rebuild_dirty, upsert_doc
from marden.readmodels.registry import register, unregister

CAMPUS = "stargate-abilene"


@pytest.fixture
def dc_row_builder(committed):
    """A test builder for the real `dc_row` view (any registered one is restored afterwards)."""
    previous = get_builder("dc_row")
    unregister("dc_row")
    calls: list[str] = []

    def keys(conn):
        return [r["id"] for r in conn.execute("SELECT id FROM campus ORDER BY id")]

    @builder("dc_row", keys=keys)
    def build(conn, key):
        calls.append(key)
        if key == "boom":
            raise RuntimeError("builder bug")
        row = conn.execute("SELECT id, name FROM campus WHERE id = %s", (key,)).fetchone()
        if row is None:
            return None
        facts = current_facts(conn, "campus", key)
        return {"id": row["id"], "name": row["name"], "facilityMw": sourced(facts.get("facility_mw"))}

    committed.execute("INSERT INTO campus (id, name, state) VALUES (%s, 'Stargate Abilene', 'TX')", (CAMPUS,))
    yield calls
    unregister("dc_row")
    if previous is not None:
        register(previous)


def _publish_mw(value: float, valid_from: date) -> None:
    with db.transaction() as conn:
        FactWriter(conn).add(
            "campus", CAMPUS, "facility_mw", value, tag="F", source_name="TCEQ",
            source_url="https://www.tceq.texas.gov/permitting/air", valid_from=valid_from,
            producer="collector:tceq", status="published", unit="MW",
        )


def _campus_events(committed) -> list[dict]:
    rows = committed.execute("SELECT payload FROM stream_event ORDER BY id").fetchall()
    return [r["payload"] for r in rows if r["payload"]["type"] == "campus"]


def _dirty(committed, view: str) -> set[str]:
    return {r["key"] for r in committed.execute("SELECT key FROM read_model_dirty WHERE view = %s", (view,))}


def test_published_fact_rebuilds_view_and_emits_event(committed, dc_row_builder):
    _publish_mw(1200, date(2026, 9, 1))
    assert _dirty(committed, "dc_row") == {CAMPUS}
    stats = rebuild_dirty()
    assert dc_row_builder == [CAMPUS] and stats.changed >= 1 and not stats.errors
    doc = committed.execute(
        "SELECT doc, as_of FROM read_model WHERE view = 'dc_row' AND key = %s", (CAMPUS,)
    ).fetchone()
    assert doc["doc"]["facilityMw"] == {"value": 1200, "tag": "F", "asOf": "2026-09-01", "sourceName": "TCEQ",
                                        "sourceUrl": "https://www.tceq.texas.gov/permitting/air"}
    assert _dirty(committed, "dc_row") == set()
    assert _campus_events(committed) == [{"type": "campus", "campusId": CAMPUS}]

    rebuild_dirty()  # nothing dirty for dc_row: not rebuilt
    assert dc_row_builder == [CAMPUS]

    mark_dirty("dc_row", CAMPUS, reason="manual")  # rebuilt, unchanged: no event
    stats = rebuild_dirty()
    assert dc_row_builder == [CAMPUS, CAMPUS] and len(_campus_events(committed)) == 1

    _publish_mw(1400, date(2026, 10, 1))  # a newer filing changes the doc: one more event
    rebuild_dirty()
    assert len(_campus_events(committed)) == 2
    doc = committed.execute("SELECT doc FROM read_model WHERE view = 'dc_row' AND key = %s", (CAMPUS,)).fetchone()
    assert doc["doc"]["facilityMw"]["value"] == 1400


def test_missing_subject_and_failing_builder(committed, dc_row_builder):
    mark_dirty("dc_row", "gone")
    mark_dirty("dc_row", "boom")
    stats = rebuild_dirty()
    assert stats.removed == 1 and [e[:2] for e in stats.errors] == [("dc_row", "boom")]
    assert _dirty(committed, "dc_row") == {"boom"}  # kept for the next pass
    assert committed.execute("SELECT count(*) AS n FROM read_model").fetchone()["n"] == 0


def test_views_without_a_builder_wait(committed):
    mark_dirty("view_without_builder", "k1")
    stats = rebuild_dirty()
    assert "view_without_builder" in stats.skipped_views
    assert _dirty(committed, "view_without_builder") == {"k1"}


def test_rebuild_all_per_key(committed, dc_row_builder):
    committed.execute("INSERT INTO campus (id, name) VALUES ('second-campus', 'Second')")
    stats = rebuild_all("dc_row")
    assert stats.built == 2 and sorted(dc_row_builder) == ["second-campus", CAMPUS]
    assert committed.execute("SELECT count(*) AS n FROM read_model WHERE view = 'dc_row'").fetchone()["n"] == 2
    with pytest.raises(KeyError):
        rebuild_all("no_such_view")


def test_many_builder_and_snapshots_per_day(committed):
    view = "test_singleton_view"

    @builder(view, many=True)
    def build(conn):
        n = conn.execute("SELECT count(*) AS n FROM campus").fetchone()["n"]
        yield "all", {"campuses": n}

    try:
        mark_dirty(view, "all")
        stats = rebuild_dirty(as_of=date(2026, 10, 20))
        assert stats.built == 1 and _dirty(committed, view) == set()
        rebuild_all(view, as_of=date(2026, 10, 21))
        rows = committed.execute(
            "SELECT as_of, doc FROM read_model WHERE view = %s ORDER BY as_of", (view,)
        ).fetchall()
        assert [(r["as_of"], r["doc"]) for r in rows] == [
            (date(2026, 10, 20), {"campuses": 0}), (date(2026, 10, 21), {"campuses": 0})
        ]
        with db.transaction() as conn:
            assert upsert_doc(conn, view, "all", {"campuses": 0}, as_of=date(2026, 10, 21)) is False
            assert upsert_doc(conn, view, "all", {"campuses": 1}, as_of=date(2026, 10, 21)) is True
    finally:
        unregister(view)


def test_registry_rejects_bad_builders():
    with pytest.raises(ValueError, match="needs keys"):
        builder("test_no_keys")(lambda conn, key: {})
    assert get_builder("test_no_keys") is None
