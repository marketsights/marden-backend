"""nyfed_sofr: the parser on real NY Fed API responses, the notice, facts and reruns, schedule, live run."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from marden import cli
from marden.collectors import nyfed_sofr as sofr
from marden.collectors.rates_common import ParseError, RunStats, due_et
from marden.core import documents, sources
from marden.core.http import Fetcher
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "nyfed_sofr"


def _stored(name: str, year: int) -> documents.DocumentRow:
    sources.upsert_spec(sofr.SOURCE)
    return documents.record_document(
        sofr.SOURCE_ID, sofr.year_url(year), (DATA / name).read_bytes(), content_type="application/json",
        doc_type="sofr_json", external_id=str(year),
    )


def test_parses_a_year_response():
    records = sofr.parse_json((DATA / "sofr_2026_excerpt.json").read_bytes())
    assert len(records) == 9
    day, rate, extra = records[0]
    assert (day, rate) == (date(2026, 10, 20), Decimal("3.88"))
    assert extra["volumeInBillions"] == 2925 and extra["revisionIndicator"] == ""
    assert records[-1][0] == date(2026, 1, 2)


def test_parses_the_latest_endpoint():
    assert [(d, r) for d, r, _ in sofr.parse_json((DATA / "sofr_last_1.json").read_bytes())] == [
        (date(2026, 10, 20), Decimal("3.88"))
    ]


def test_changed_responses_are_refused():
    with pytest.raises(ParseError):
        sofr.parse_json(b"<html>error</html>")
    with pytest.raises(ParseError, match="refRates"):
        sofr.parse_json(b'{"error": "bad request"}')
    with pytest.raises(ParseError, match="unexpected record"):
        sofr.parse_json(b'{"refRates": [{"effectiveDate": "2026-10-20", "type": "EFFR", "percentRate": 3.86}]}')
    with pytest.raises(ParseError, match="outside"):
        sofr.parse_json(b'{"refRates": [{"effectiveDate": "2026-10-20", "type": "SOFR", "percentRate": 388}]}')
    assert sofr.parse_json(b'{"refRates": [{"effectiveDate": "2026-10-20", "type": "SOFR"}]}') == []


def test_notice_follows_the_terms_of_use():
    terms = (DATA / "terms_of_use_excerpt.txt").read_text()
    for part in sofr.NOTICE_QUOTE.split(" ... "):
        assert part in terms  # quoted verbatim from the Terms of Use
    assert "Terms of Use Last Updated: 6/9/2023" in terms and sofr.TERMS_UPDATED == date(2023, 6, 9)
    assert "[" not in sofr.NOTICE and "by Marden" in sofr.NOTICE  # brackets completed
    assert sofr.NOTICE in sofr.SOURCE.terms_note
    assert "manually or through an automated process or device" in terms


def test_load_writes_rates_and_notice_once(committed):
    doc = _stored("sofr_2026_excerpt.json", 2026)
    first = RunStats()
    sofr.load_document(doc, first)
    assert first.facts_new == 9 + 1
    again = RunStats()
    sofr.load_document(doc, again)
    assert again.facts_new == 0

    cur = {r["field"]: r for r in committed.execute(
        "SELECT * FROM fact_current WHERE subject_type = 'series' AND subject_id = 'sofr'").fetchall()}
    assert cur["value"]["value"] == 3.88 and cur["value"]["valid_from"] == date(2026, 10, 20)
    assert (cur["value"]["tag"], cur["value"]["source_name"]) == ("F", "Federal Reserve Bank of New York")
    assert cur["value"]["document_id"] == doc.id
    assert cur["notice"]["value"] == sofr.NOTICE and cur["notice"]["source_url"] == sofr.TERMS_URL
    assert cur["notice"]["valid_from"] == date(2023, 6, 9) and cur["notice"]["quote"] == sofr.NOTICE_QUOTE
    src = committed.execute("SELECT terms_note, blocked FROM source WHERE id = 'nyfed_sofr'").fetchone()
    assert sofr.NOTICE in src["terms_note"] and src["blocked"] is False


def test_a_revised_rate_is_a_new_fact_for_the_same_date(committed):
    sources.upsert_spec(sofr.SOURCE)
    url = sofr.year_url(2026)
    body = b'{"refRates": [{"effectiveDate": "2026-10-20", "type": "SOFR", "percentRate": %s, "revisionIndicator": "%s"}]}'
    for rate, flag in ((b"3.88", b""), (b"3.89", b"Y")):
        doc = documents.record_document(sofr.SOURCE_ID, url, body % (rate, flag), content_type="application/json")
        sofr.load_document(doc, RunStats())
    rows = committed.execute(
        "SELECT value FROM fact WHERE subject_id = 'sofr' AND field = 'value' ORDER BY id").fetchall()
    assert [r["value"] for r in rows] == [3.88, 3.89]  # history kept
    cur = committed.execute(
        "SELECT value FROM fact_current WHERE subject_id = 'sofr' AND field = 'value'").fetchone()
    assert cur["value"] == 3.89


def test_backfill_covers_two_calendar_years_and_is_not_refetched(committed):
    assert sofr.years_due(date(2026, 10, 21)) == [2024, 2025, 2026]
    sofr.load_document(_stored("sofr_2026_excerpt.json", 2024), RunStats())  # any loaded file for 2024
    assert sofr.years_due(date(2026, 10, 21)) == [2025, 2026]


def test_a_failed_fetch_is_recorded_and_nothing_written(committed, monkeypatch):
    monkeypatch.setattr(sofr, "year_url", lambda year: "http://127.0.0.1:9/unreachable.json")
    out = sofr.run(years=[2026], fetcher=Fetcher(max_retries=0, timeout=2))
    assert out["ok"] is False and out["error"].startswith("FetchError")
    assert "FetchError" in committed.execute("SELECT last_error FROM source WHERE id = 'nyfed_sofr'").fetchone()[
        "last_error"]
    assert committed.execute("SELECT count(*) AS n FROM fact").fetchone()["n"] == 0


def test_scheduled_daily_at_0830_eastern():
    assert app.tasks["collect.nyfed_sofr"].queue == "collect"
    crons = {pid: pt.cron for (name, pid), pt in app.periodic_registry.periodic_tasks.items()
             if name == "collect.nyfed_sofr"}
    assert crons == {"daily_0830_et": "30 12,13 * * *"}
    for day in (date(2026, 7, 1), date(2026, 12, 1)):
        slots = [int(datetime(day.year, day.month, day.day, h, 30, tzinfo=UTC).timestamp()) for h in (12, 13)]
        assert [due_et(ts, 8, 30) for ts in slots].count(True) == 1
    off_slot = int(datetime(2026, 12, 1, 12, 30, tzinfo=UTC).timestamp())  # 07:30 EST
    assert sofr.collect_task(timestamp=off_slot) == {"skipped": "not 08:30 ET"}
    assert "nyfed_sofr" in cli._groups["collect"]


@pytest.mark.network
def test_live_run_twice_writes_nothing_new(committed):
    first = sofr.run(years=[2026])
    assert first["ok"], first
    assert first["documents_new"] == 1 and first["facts_new"] > 100
    second = sofr.run(years=[2026])
    assert second["ok"], second
    assert second["documents_new"] == 0 and second["facts_new"] == 0
    notice = committed.execute(
        "SELECT value FROM fact_current WHERE subject_id = 'sofr' AND field = 'notice'").fetchone()
    assert notice["value"] == sofr.NOTICE
