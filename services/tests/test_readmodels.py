from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from marden.core import db
from marden.core.facts import FactWriter, sourced
from marden.readmodels import (
    VIEWS,
    build_date,
    builder,
    business_today,
    depends,
    get_builder,
    known_facts,
    mark_dirty,
    rebuild_all,
    rebuild_dirty,
    upsert_doc,
)
from marden.readmodels.deps import dependencies
from marden.readmodels.views import SCHEDULED_VIEWS, UnknownView

CAMPUS = "stargate-abilene"
ARCHITECTURE = Path(__file__).resolve().parents[2] / "docs" / "ARCHITECTURE.md"


@pytest.fixture
def dc_row_builder(committed, take_view):
    """A test builder for the real `dc_row` view (any registered one is restored afterwards)."""
    take_view("dc_row")
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
        facts = known_facts(conn, "campus", key)
        return {"id": row["id"], "name": row["name"], "facilityMw": sourced(facts.get("facility_mw")),
                "builtFor": build_date().isoformat()}

    committed.execute("INSERT INTO campus (id, name, state) VALUES (%s, 'Stargate Abilene', 'TX')", (CAMPUS,))
    yield calls


def _publish_mw(value: float, valid_from: date) -> int:
    with db.transaction() as conn:
        return FactWriter(conn).add(
            "campus", CAMPUS, "facility_mw", value, tag="F", source_name="TCEQ",
            source_url="https://www.tceq.texas.gov/permitting/air", valid_from=valid_from,
            producer="collector:tceq", status="published", unit="MW",
        ).id


def _campus_events(committed) -> list[dict]:
    rows = committed.execute("SELECT payload FROM stream_event ORDER BY id").fetchall()
    return [r["payload"] for r in rows if r["payload"]["type"] == "campus"]


def _dirty(committed, view: str) -> set[str]:
    return {r["key"] for r in committed.execute("SELECT key FROM read_model_dirty WHERE view = %s", (view,))}


def _doc(committed, view: str, key: str, as_of: date) -> dict | None:
    row = committed.execute(
        "SELECT doc FROM read_model WHERE view = %s AND key = %s AND as_of = %s", (view, key, as_of)
    ).fetchone()
    return row["doc"] if row else None


def test_published_fact_rebuilds_view_and_emits_event(committed, dc_row_builder):
    _publish_mw(1200, date(2026, 9, 1))
    assert _dirty(committed, "dc_row") == {CAMPUS}
    stats = rebuild_dirty()
    assert dc_row_builder == [CAMPUS] and stats.changed >= 1 and not stats.errors
    doc = committed.execute(
        "SELECT doc, as_of FROM read_model WHERE view = 'dc_row' AND key = %s", (CAMPUS,)
    ).fetchone()
    assert doc["as_of"] == business_today()
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


def test_backfilled_snapshot_shows_what_was_known_that_day(committed, dc_row_builder):
    today = business_today()
    yesterday = today - timedelta(days=1)
    early = _publish_mw(1200, date(2026, 9, 1))
    committed.execute("UPDATE fact SET learned_at = now() - interval '3 days' WHERE id = %s", (early,))
    _publish_mw(1400, date(2026, 10, 1))  # learned today: not known yesterday
    rebuild_dirty()
    events_before = len(_campus_events(committed))

    mark_dirty("dc_row", CAMPUS, reason="still dirty for today")
    stats = rebuild_all("dc_row", as_of=yesterday)
    assert stats.built == 1 and not stats.errors
    past = _doc(committed, "dc_row", CAMPUS, yesterday)
    assert past is not None
    assert past["facilityMw"]["value"] == 1200 and past["builtFor"] == yesterday.isoformat()
    assert _doc(committed, "dc_row", CAMPUS, today)["facilityMw"]["value"] == 1400
    assert len(_campus_events(committed)) == events_before  # no live event for a past day
    assert _dirty(committed, "dc_row") == {CAMPUS}  # today's snapshot still owes a rebuild

    rebuild_all("dc_row")
    assert _dirty(committed, "dc_row") == set()


def test_snapshots_cannot_be_dated_after_today_and_dirty_builds_today(committed, dc_row_builder):
    tomorrow = business_today() + timedelta(days=1)
    with pytest.raises(ValueError, match="after today"):
        rebuild_all("dc_row", as_of=tomorrow)
    with pytest.raises(ValueError, match="after today"), db.transaction() as conn:
        upsert_doc(conn, "dc_row", CAMPUS, {"x": 1}, as_of=tomorrow)
    with pytest.raises(ValueError, match="today's snapshot"):
        rebuild_dirty(as_of=business_today() - timedelta(days=1))
    assert committed.execute("SELECT count(*) AS n FROM read_model").fetchone()["n"] == 0


def test_missing_subject_and_failing_builder(committed, dc_row_builder):
    mark_dirty("dc_row", "gone")
    mark_dirty("dc_row", "boom")
    stats = rebuild_dirty()
    assert stats.removed == 1 and [e[:2] for e in stats.errors] == [("dc_row", "boom")]
    assert _dirty(committed, "dc_row") == {"boom"}  # kept for the next pass
    assert committed.execute("SELECT count(*) AS n FROM read_model").fetchone()["n"] == 0


def test_views_without_a_builder_wait(committed, take_view):
    view = take_view("gpu_model")
    mark_dirty(view, "k1")
    stats = rebuild_dirty()
    assert view in stats.skipped_views
    assert _dirty(committed, view) == {"k1"}


def test_rebuild_all_per_key(committed, dc_row_builder):
    committed.execute("INSERT INTO campus (id, name) VALUES ('second-campus', 'Second')")
    stats = rebuild_all("dc_row")
    assert stats.built == 2 and sorted(dc_row_builder) == ["second-campus", CAMPUS]
    assert committed.execute("SELECT count(*) AS n FROM read_model WHERE view = 'dc_row'").fetchone()["n"] == 2
    with pytest.raises(KeyError):
        rebuild_all("curve" if get_builder("curve") is None else "no_such_view")


def test_many_builder_and_snapshots_per_day(committed, take_view):
    view = take_view("curve")
    today = business_today()
    yesterday = today - timedelta(days=1)

    @builder(view, many=True)
    def build(conn):
        n = conn.execute("SELECT count(*) AS n FROM campus").fetchone()["n"]
        yield "all", {"campuses": n}

    rebuild_all(view, as_of=yesterday)
    mark_dirty(view, "all")
    stats = rebuild_dirty()
    assert stats.built == 1 and _dirty(committed, view) == set()
    rows = committed.execute("SELECT as_of, doc FROM read_model WHERE view = %s ORDER BY as_of", (view,)).fetchall()
    assert [(r["as_of"], r["doc"]) for r in rows] == [(yesterday, {"campuses": 0}), (today, {"campuses": 0})]
    with db.transaction() as conn:
        assert upsert_doc(conn, view, "all", {"campuses": 0}) is False
        assert upsert_doc(conn, view, "all", {"campuses": 1}) is True
        # A backfill compares against the snapshot in force on its own day, not the newest one.
        assert upsert_doc(conn, view, "all", {"campuses": 0}, as_of=yesterday) is False


def test_view_names_are_checked_everywhere():
    with pytest.raises(UnknownView):
        builder("dc_rows", keys=("all",))(lambda conn, key: {})
    with pytest.raises(UnknownView):
        depends("campus", "", "dc_rows")
    with pytest.raises(UnknownView):
        mark_dirty("site_tabs:debt", "abilene")
    assert get_builder("dc_rows") is None


def test_registry_rejects_bad_builders(take_view):
    view = take_view("leases")
    with pytest.raises(ValueError, match="needs keys"):
        builder(view)(lambda conn, key: {})
    assert get_builder(view) is None


def test_every_fact_driven_view_has_a_dependency():
    targeted = {dep.view for dep in dependencies()}
    assert targeted <= set(VIEWS)
    assert set(VIEWS) - SCHEDULED_VIEWS == targeted


def test_architecture_view_table_matches_views():
    """docs/ARCHITECTURE.md's view/key table is the contract the API and builders share."""
    text = ARCHITECTURE.read_text()
    section = text.split("### View and key table", 1)[1].split("\n## ", 1)[0]
    documented: dict[str, str] = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not cells[0].startswith("`"):
            continue
        key = cells[1].strip("`")
        names = re.findall(r"`([^`]+)`", cells[0])
        if "<tab>" in names[0]:  # `site_tab:<tab>` for `evidence`, `power`, ...
            documented.update({names[0].replace("<tab>", tab): key for tab in names[1:]})
        else:
            documented.update({name: key for name in names})
    assert documented == VIEWS
