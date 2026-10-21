"""ust_par_curve: the parser on real Treasury CSVs, facts and reruns on the test DB, schedule, live run."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from marden import cli
from marden.collectors import ust_par_curve as ust
from marden.collectors.rates_common import ParseError, RunStats, due_et
from marden.core import documents, sources
from marden.core.http import Fetcher
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "ust_par_curve"


def _stored(name: str, year: int) -> documents.DocumentRow:
    sources.upsert_spec(ust.SOURCE)
    return documents.record_document(
        ust.SOURCE_ID, ust.year_url(year), (DATA / name).read_bytes(), content_type="text/csv",
        doc_type="par_yield_curve_csv", external_id=str(year),
    )


def test_parses_the_current_year_file():
    parsed = ust.parse_csv((DATA / "par_curve_2026_head.csv").read_bytes())
    assert parsed.unknown_columns == [] and parsed.blank_cells == 0
    assert len(parsed.rows) == 10
    day, values = parsed.rows[0]
    assert day == date(2026, 10, 20)
    assert set(values) == set(ust.TENOR_YEARS)  # all 14 tenors
    assert values["ust_par_10y"] == Decimal("5.12") and values["ust_par_30y"] == Decimal("5.42")
    assert values["ust_par_6w"] == Decimal("4.13")  # Treasury's "1.5 Month" column


def test_blank_tenors_are_skipped_not_filled():
    parsed = ust.parse_csv((DATA / "par_curve_2025_excerpt.csv").read_bytes())
    by_day = dict(parsed.rows)
    assert "ust_par_6w" not in by_day[date(2025, 2, 14)]  # the 6-week series starts 2025-02-18
    assert by_day[date(2025, 2, 18)]["ust_par_6w"] == Decimal("4.41")
    assert parsed.blank_cells == 3
    assert len(by_day[date(2025, 1, 2)]) == 13


def test_changed_file_shapes_are_refused():
    with pytest.raises(ParseError):
        ust.parse_csv(b"<html>Site maintenance</html>")
    with pytest.raises(ParseError, match="outside"):
        ust.parse_csv(b'Date,"10 Yr"\n10/20/2026,512\n')  # basis points, not percent
    with pytest.raises(ParseError, match="bad date"):
        ust.parse_csv(b'Date,"10 Yr"\n2026-10-20,5.12\n')
    with pytest.raises(ParseError, match="not a number"):
        ust.parse_csv(b'Date,"10 Yr"\n10/20/2026,five\n')
    parsed = ust.parse_csv(b'Date,"10 Yr","15 Yr"\n10/20/2026,5.12,5.30\n')
    assert parsed.unknown_columns == ["15 Yr"]  # reported in run meta, never written under a guessed id
    assert parsed.rows == [(date(2026, 10, 20), {"ust_par_10y": Decimal("5.12")})]


def test_load_writes_one_fact_per_tenor_and_date_once(committed):
    doc = _stored("par_curve_2025_excerpt.csv", 2025)
    first = RunStats()
    ust.load_document(doc, first)
    assert first.facts_new == 7 * 14 - 3
    again = RunStats()
    ust.load_document(doc, again)
    assert again.facts_new == 0 and again.facts_seen == first.facts_seen

    row = committed.execute(
        "SELECT * FROM fact_current WHERE subject_type = 'series' AND subject_id = 'ust_par_10y' AND field = 'value'"
    ).fetchone()
    assert row["value"] == 4.18 and row["valid_from"] == date(2025, 12, 31)
    assert (row["tag"], row["source_name"], row["unit"], row["status"]) == ("F", "U.S. Treasury", "pct", "published")
    assert row["source_url"] == ust.year_url(2025) and row["document_id"] == doc.id
    assert row["producer"] == "collector:ust_par_curve"
    asof = committed.execute(
        "SELECT value FROM fact_asof(current_date) WHERE subject_type = 'series' AND subject_id = 'ust_par_10y'"
    ).fetchone()
    assert asof["value"] == 4.18
    dirty = committed.execute("SELECT key FROM read_model_dirty WHERE view = 'curve'").fetchall()
    assert [r["key"] for r in dirty] == ["all"]


def test_loaded_backfill_years_are_not_fetched_again(committed):
    assert ust.years_due(date(2026, 10, 21)) == [2025, 2026]
    ust.load_document(_stored("par_curve_2025_excerpt.csv", 2025), RunStats())
    assert ust.years_due(date(2026, 10, 21)) == [2026]
    assert ust.years_due(date(2026, 1, 3)) == [2025, 2026]  # last year stays live in early January


def test_a_failed_fetch_is_recorded_and_nothing_written(committed, monkeypatch):
    monkeypatch.setattr(ust, "year_url", lambda year: "http://127.0.0.1:9/unreachable.csv")
    out = ust.run(years=[2026], fetcher=Fetcher(max_retries=0, timeout=2))
    assert out["ok"] is False and out["error"].startswith("FetchError")
    src = committed.execute("SELECT last_error, last_ok_at FROM source WHERE id = 'ust_par_curve'").fetchone()
    assert "FetchError" in src["last_error"] and src["last_ok_at"] is None
    assert committed.execute("SELECT ok FROM source_run WHERE source_id = 'ust_par_curve'").fetchone()["ok"] is False
    assert committed.execute("SELECT count(*) AS n FROM fact").fetchone()["n"] == 0


def test_scheduled_daily_at_1830_eastern():
    task = app.tasks["collect.ust_par_curve"]
    assert task.queue == "collect"
    crons = {pid: pt.cron for (name, pid), pt in app.periodic_registry.periodic_tasks.items()
             if name == "collect.ust_par_curve"}
    assert crons == {"daily_1830_et": "30 22,23 * * *"}
    for day in (date(2026, 7, 1), date(2026, 12, 1)):  # EDT and EST: exactly one UTC slot is due
        slots = [int(datetime(day.year, day.month, day.day, h, 30, tzinfo=UTC).timestamp()) for h in (22, 23)]
        assert [due_et(ts, 18, 30) for ts in slots].count(True) == 1
    off_slot = int(datetime(2026, 7, 1, 23, 30, tzinfo=UTC).timestamp())  # 19:30 EDT
    assert ust.collect_task(timestamp=off_slot) == {"skipped": "not 18:30 ET"}
    assert "ust_par_curve" in cli._groups["collect"]


@pytest.mark.network
def test_live_run_twice_writes_nothing_new(committed):
    first = ust.run(years=[2026])
    assert first["ok"], first
    assert first["documents_new"] == 1 and first["facts_new"] > 0
    assert "ust_par_10y" in first["latest"]
    second = ust.run(years=[2026])
    assert second["ok"], second
    assert second["documents_new"] == 0 and second["facts_new"] == 0
    assert second["facts_seen"] == first["facts_seen"]
