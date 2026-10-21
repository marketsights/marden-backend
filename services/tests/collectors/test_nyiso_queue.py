"""nyiso_queue: the workbook link, every sheet of a real workbook excerpt, terms, a load and a rerun, the
schedule and a live run (network)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from marden import cli
from marden.collectors import nyiso_queue as nyiso
from marden.collectors.rates_common import ParseError, RunStats
from marden.core import documents, sources
from marden.core.facts import FactWriter
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "nyiso_queue"
WORKBOOK = DATA / "NYISO-Interconnection-Queue-09302026_excerpt.xlsx"
URL = ("https://www.nyiso.com/documents/20142/1407078/NYISO-Interconnection-Queue-09302026.xlsx/"
       "ff0e2005-e8d3-e75d-3e81-fa7027a52685?t=1791917810242")


def test_finds_the_current_workbook_on_the_page():
    url, as_of = nyiso.find_workbook((DATA / "interconnections_excerpt.html").read_bytes())
    assert url == URL and as_of == date(2026, 9, 30)
    with pytest.raises(ParseError):
        nyiso.find_workbook(b"<html>no workbook here</html>")


def test_parses_every_sheet():
    entries = {e.queue_id: e for e in nyiso.parse_workbook(WORKBOOK.read_bytes())}
    assert "A01-703" not in entries  # Affected System Studies: another ISO's project
    homer = entries["0276"].values
    assert (homer["name"], homer["fuel"], homer["mw_requested"], homer["status"]) == (
        "Homer Solar Energy Center", "Solar", 90, "IA Completed")
    assert homer["proposed_cod"] == "2028-06" and homer["queue_date"] == date(2008, 1, 30) and homer["zone"] == "C"
    load = entries["1670"].values
    assert (load["fuel"], load["end_use"], load["developer"], load["poi"]) == (
        "LOAD", "DAT-AI (AI Data center)", "Lake Mariner Data LLC", "Kintigh 345kV Substation")
    assert entries["0849"].values["status"] == "In Service Commercial"  # int queue numbers are zero-padded
    assert entries["C24-003"].values["status"] == "Final Decision Period"  # cluster status key
    assert entries["C24-052-001"].values["status_class"] == "active"
    assert entries["0001"].values["status"] == "In Service Commercial"  # two-row header sheet
    assert entries["0005"].values["status"] == "Withdrawn" and entries["C24-001"].values["status"] == "Withdrawn"
    assert entries["1670"].updated == date(2026, 7, 31) and entries["1465"].updated is None  # 1900-01-14 dropped


def test_refuses_a_file_that_is_not_the_workbook():
    with pytest.raises(ParseError):
        nyiso.parse_workbook(b"<html>error</html>")


def test_terms_note_quotes_the_legal_notice():
    legal = " ".join((DATA / "legal_notice_excerpt.txt").read_text().split())
    for part in ("Access to this Web site does not confer any license or ownership interest in either the form or "
                 "content of the Web site", "Downloading, republishing, retransmitting, reproducing, or other use of "
                 "any image or video on this website as a stand-alone file is strictly prohibited"):
        assert part in legal and part in nyiso.TERMS_NOTE
    robots = (DATA / "robots.txt").read_text()
    assert "Disallow: /public/" in robots and "/documents" not in robots


def test_load_writes_queue_facts_links_and_reruns_clean(committed):
    committed.execute("INSERT INTO site (slug, name, state, county, tracking_names) VALUES (%s, %s, %s, %s, %s)",
                      ("lake-mariner", "Lake Mariner", "NY", "Niagara", ["TeraWulf Brookings", "WULF Compute LLC"]))
    FactWriter(committed).add("site", "lake-mariner", "place", "Barker, New York", tag="R", source_name="test",
                              valid_from=date(2026, 10, 1), producer="seed:test", status="published")
    sources.upsert_spec(nyiso.SOURCE)
    index = documents.record_document(nyiso.SOURCE_ID, nyiso.INDEX_URL, (DATA / "interconnections_excerpt.html")
                                      .read_bytes(), content_type="text/html")
    doc = documents.record_document(nyiso.SOURCE_ID, URL, WORKBOOK.read_bytes(), content_type="application/xlsx")
    stats = RunStats()
    nyiso.load(index, doc, date(2026, 9, 30), stats, min_entries=1)
    assert stats.facts_new > 0
    report = stats.extra["iso"]["nyiso"]
    assert report["linked"] == {"nyiso:1670": ["site:lake-mariner"], "nyiso:1732": ["site:lake-mariner"]}
    row = committed.execute("SELECT * FROM fact_current WHERE subject_id = 'nyiso:1732' AND field = 'status'"
                            ).fetchone()
    assert (row["value"], row["tag"], row["source_name"], row["source_url"], row["document_id"]) == (
        "SRIS/SIS Approved", "F", "NYISO", URL, doc.id)
    assert row["producer"] == "collector:nyiso_queue"
    again = RunStats()
    nyiso.load(index, doc, date(2026, 9, 30), again, min_entries=1)
    assert again.facts_new == 0 and again.facts_seen == stats.facts_seen
    with pytest.raises(ParseError, match="truncated"):
        nyiso.load(index, doc, date(2026, 9, 30), RunStats())


def test_schedule_and_cli():
    task = app.tasks["collect.nyiso_queue"]
    assert task.queue == "collect"
    crons = {name: t.cron for (name, _), t in app.periodic_registry.periodic_tasks.items()}
    assert crons["collect.nyiso_queue"] == nyiso.CRON_UTC
    assert cli.build_parser().parse_args(["collect", "nyiso_queue"])._handler is nyiso.collect_cli
    assert nyiso.SOURCE.cadence_minutes == 7 * 24 * 60 and not nyiso.SOURCE.blocked


@pytest.mark.network
def test_live_run_twice_writes_nothing_new(committed):
    first = nyiso.run()
    assert first["ok"], first
    assert first["iso"]["nyiso"]["entries"] > 1000 and first["facts_new"] > 0
    second = nyiso.run()
    assert second["ok"], second
    assert second["facts_new"] == 0 and second["facts_seen"] == first["facts_seen"]
