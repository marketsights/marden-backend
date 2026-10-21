"""isone_queue: the IRTT public queue table (real excerpt), unit rows on shared queue positions, terms,
a load and a rerun, the schedule and a live run (network)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from marden import cli
from marden.collectors import isone_queue as isone
from marden.collectors.rates_common import ParseError, RunStats
from marden.core import documents, sources
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "isone_queue"
PAGE = DATA / "reports_external_excerpt.html"


def test_parses_the_public_queue_table():
    as_of, entries = isone.parse_page(PAGE.read_bytes())
    assert as_of == date(2026, 10, 21)
    rows = {e.queue_id: e for e in entries}
    wind = rows["1090"]
    assert (wind.values["name"], wind.values["fuel"], wind.values["mw_requested"], wind.values["status"]) == (
        "Park City Offshore Wind CNR", "Wind", 838.2, "Active")  # Net MW 0 -> Summer MW
    assert wind.values["study_phase"] == "IA: Executed" and wind.values["cycle"] == "TCS"
    assert wind.values["proposed_cod"] == "2029-12-31" and wind.updated == date(2025, 10, 17)
    bess = rows["1238"].values
    assert (bess["county"], bess["state"], bess["zone"], bess["study_phase"]) == (
        "Norfolk", "MA", "SEMA", "IA: In Progress")
    assert rows["1601"].values["status_class"] == "withdrawn" and rows["1601"].values["withdrawn_date"] == date(
        2025, 6, 13)
    units = sorted(qid for qid in rows if qid.startswith("8/"))
    assert units == ["8/anp-bellingham-energy-project-unit-1", "8/anp-bellingham-energy-project-unit-2",
                     "8/anp-blackstone-energy-project-unit-1", "8/anp-blackstone-energy-project-unit-2"]
    commercial = rows["8/anp-bellingham-energy-project-unit-1"].values
    assert commercial["status"] == "Commercial" and commercial["status_class"] == "in_service"


def test_refuses_the_login_page_and_layout_changes():
    with pytest.raises(ParseError, match="publicqueue"):
        isone.parse_page(b"<html><body><form action='/account/login'></form></body></html>")
    changed = PAGE.read_bytes().replace(b"<th>Zone</th>", b"<th>Load Zone</th>", 1)
    with pytest.raises(ParseError, match="columns changed"):
        isone.parse_page(changed)


def test_terms_note_quotes_iso_ne():
    legal = (DATA / "legal_privacy_excerpt.txt").read_text()
    quote = ("You may not obtain or attempt to obtain any materials or information through any means not "
             "intentionally made available through this Web site.")
    assert quote in legal and quote in isone.TERMS_NOTE
    assert "overburden or impair any ISO New England server(s)" in legal


def test_load_and_rerun(committed):
    sources.upsert_spec(isone.SOURCE)
    doc = documents.record_document(isone.SOURCE_ID, isone.QUEUE_URL, PAGE.read_bytes(), content_type="text/html")
    stats = RunStats()
    isone.load(doc, stats, min_entries=1)
    n = committed.execute("SELECT count(*) AS n FROM queue_project WHERE iso = 'isone'").fetchone()["n"]
    assert n == stats.extra["iso"]["isone"]["entries"] == 20
    iso = committed.execute("SELECT value, source_name FROM fact_current WHERE subject_id = 'isone:1238' "
                            "AND field = 'iso'").fetchone()
    assert (iso["value"], iso["source_name"]) == ("ISO-NE", "ISO New England")
    again = RunStats()
    isone.load(doc, again, min_entries=1)
    assert again.facts_new == 0 and again.facts_seen == stats.facts_seen


def test_schedule_and_cli():
    assert app.tasks["collect.isone_queue"].queue == "collect"
    crons = {name: t.cron for (name, _), t in app.periodic_registry.periodic_tasks.items()}
    assert crons["collect.isone_queue"] == isone.CRON_UTC
    assert cli.build_parser().parse_args(["collect", "isone_queue"])._handler is isone.collect_cli


@pytest.mark.network
def test_live_run_twice_writes_nothing_new(committed):
    first = isone.run()
    assert first["ok"], first
    assert first["iso"]["isone"]["entries"] > 1000 and first["facts_new"] > 0
    second = isone.run()
    assert second["ok"], second
    assert second["facts_new"] == 0
