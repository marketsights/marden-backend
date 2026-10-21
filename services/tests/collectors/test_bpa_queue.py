"""bpa_queue: the BPA interconnection request queue workbook (real excerpt), generator and line/load rows,
status classes, terms, a load with a large-load match and a rerun, the schedule and a live run (network)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from marden import cli
from marden.collectors import bpa_queue as bpa
from marden.collectors.rates_common import ParseError, RunStats
from marden.core import documents, sources
from marden.core.facts import FactWriter
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "bpa_queue"
WORKBOOK = DATA / "InterconnectionQueueOutput_excerpt.xlsx"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _entries() -> dict[str, object]:
    as_of, entries = bpa.parse_workbook(WORKBOOK.read_bytes())
    assert as_of == date(2026, 10, 21)
    return {e.queue_id: e for e in entries}


def test_finds_the_workbook_link():
    page = (DATA / "interconnection_excerpt.html").read_bytes()
    assert bpa.find_workbook(page) == (
        "https://www.bpa.gov/-/media/Aep/transmission-media-documents/InterconnectionQueueOutput.xlsx")
    with pytest.raises(ParseError, match="InterconnectionQueueOutput"):
        bpa.find_workbook(b"<html></html>")


def test_parses_generator_and_line_load_requests():
    rows = _entries()
    load = rows["L0705"].values
    assert (load["name"], load["developer"], load["fuel"], load["mw_requested"], load["county"]) == (
        "Open Range", "Big Bend Electric Cooperative, Inc.", "LOAD", 3300, "Adams")
    assert load["project_type"] == "Line and Load Interconnection" and "service" not in load
    assert (load["status"], load["status_class"], load["queue_date"], load["proposed_cod"]) == (
        "RECEIVED", "active", date(2026, 8, 6), "2030-01-31")
    assert (load["transmission_owner"], load["balancing_authority"]) == ("Bonneville Power Administration", "BPAT")
    hybrid = rows["G1097"].values
    assert hybrid["fuel"] == "Battery; Solar" and hybrid["mw_by_type"] == {"Battery": 1, "Solar": 0.4}
    assert hybrid["mw_requested"] == 1.4 and hybrid["service"] == "ERIS" and hybrid["state"] == "WA"
    gas = rows["G2"].values
    assert (gas["name"], gas["fuel"], gas["mw_requested"], gas["status_class"]) == (
        "Goldendale Energy Center", "Natural Gas", 247, "in_service")  # ENERGIZED
    assert rows["G0481"].values["status_class"] == "retired" and rows["G1"].values["status_class"] == "withdrawn"
    assert rows["L0587"].values["status"] == "CONST AGRMT EXE" and rows["L0587"].values["status_class"] == "active"


def test_refuses_a_changed_workbook():
    with pytest.raises(ParseError, match="not an xlsx"):
        bpa.parse_workbook(b"<html>maintenance</html>")


def test_terms_note_quotes_bpa():
    robots = (DATA / "robots.txt").read_text()
    privacy = (DATA / "privacy_excerpt.txt").read_text()
    assert "Disallow: /customerportal/" in robots and "Allow: /" in robots
    quote = "When you visit our website to read pages or download information, we automatically collect"
    assert quote in privacy and quote in bpa.TERMS_NOTE and "17 U.S.C. 105" in bpa.TERMS_NOTE


def test_load_links_a_large_load_and_reruns_clean(committed):
    committed.execute("INSERT INTO site (slug, name, state, county, tracking_names) VALUES "
                      "('drax-test', 'Cowlitz test campus', 'WA', 'Cowlitz', '{Drax}')")
    FactWriter(committed).add("site", "drax-test", "place", "Longview, Washington", tag="R", source_name="test",
                              valid_from=date(2026, 10, 1), producer="seed:test", status="published")
    sources.upsert_spec(bpa.SOURCE)
    doc = documents.record_document(bpa.SOURCE_ID, "https://www.bpa.gov/x/InterconnectionQueueOutput.xlsx",
                                    WORKBOOK.read_bytes(), content_type=XLSX)
    stats = RunStats()
    assert bpa.load(doc, stats, min_entries=1) <= date(2026, 10, 21)
    counts = stats.extra["iso"]["bpa"]
    assert counts["entries"] == committed.execute(
        "SELECT count(*) AS n FROM queue_project WHERE iso = 'bpa'").fetchone()["n"] == 20
    assert counts["linked"] == {"bpa:L0557": ["site:drax-test"]}
    cur = {r["field"]: r for r in committed.execute(
        "SELECT field, value, tag, source_name FROM fact_current WHERE subject_id = 'bpa:L0557'").fetchall()}
    assert "iso" not in cur and cur["fuel"]["value"] == "LOAD" and cur["fuel"]["tag"] == "F"
    assert cur["linked_site"]["value"][0]["evidence"]["phrase"] == "drax"
    loads = committed.execute("SELECT value FROM fact_current WHERE subject_type = 'site' AND subject_id = 'drax-test' "
                              "AND field = 'large_load_queue_bpa'").fetchone()["value"]
    assert [(r["queue_id"], r["mw_requested"], r["status"]) for r in loads] == [("L0557", 16.3, "ENERGIZED")]
    again = RunStats()
    bpa.load(doc, again, min_entries=1)
    assert again.facts_new == 0 and again.facts_seen == stats.facts_seen


def test_schedule_and_cli():
    assert app.tasks["collect.bpa_queue"].queue == "collect"
    crons = {name: t.cron for (name, _), t in app.periodic_registry.periodic_tasks.items()}
    assert crons["collect.bpa_queue"] == bpa.CRON_UTC
    assert cli.build_parser().parse_args(["collect", "bpa_queue"])._handler is bpa.collect_cli


@pytest.mark.network
def test_live_run_twice_writes_nothing_new(committed):
    first = bpa.run()
    assert first["ok"], first
    assert first["iso"]["bpa"]["entries"] > 1000 and first["facts_new"] > 0
    second = bpa.run()
    assert second["ok"], second
    assert second["facts_new"] == 0
