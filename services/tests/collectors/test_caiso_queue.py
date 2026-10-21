"""caiso_queue: the public queue report (real workbook excerpt: active, completed and withdrawn sheets),
layout guards, terms, a load with a name match and a rerun, the schedule and a live run (network)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from marden import cli
from marden.collectors import caiso_queue as caiso
from marden.collectors.rates_common import ParseError, RunStats
from marden.core import documents, licensing, sources
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "caiso_queue"
WORKBOOK = DATA / "publicqueuereport_excerpt.xlsx"


def test_parses_every_sheet():
    run_date, entries = caiso.parse_workbook(WORKBOOK.read_bytes())
    assert run_date == date(2026, 10, 21)
    rows = {e.queue_id: e.values for e in entries}
    wind = rows["22"]
    assert (wind["name"], wind["county"], wind["state"], wind["transmission_owner"]) == (
        "MONTEZUMA (HIGH WINDS III)", "SOLANO", "CA", "PGAE")
    assert (wind["fuel"], wind["project_type"], wind["mw_requested"]) == (
        "Wind Turbine + Battery", "Wind Turbine + Storage (Partial Capacity)", 38)
    assert (wind["status"], wind["status_class"], wind["queue_date"], wind["proposed_cod"]) == (
        "ACTIVE", "active", date(2003, 11, 18), "2024-04-01")
    assert wind["study_phase"] == "SIS/Phase I: Complete; FAS/Phase II: Complete; IA: Executed"
    assert (wind["cycle"], wind["zone"], wind["poi"]) == ("AMEND 39", "Northern", "Birds Landing 230 kV")
    assert rows["643R"]["name"] == "NORTH ROSAMOND SOLAR"  # queue positions with letters
    done = rows["1A"]
    assert (done["status"], done["status_class"], done["actual_cod"]) == ("COMPLETED", "in_service", date(2009, 10, 2))
    assert "proposed_cod" not in done
    gone = rows["6"]
    assert (gone["status_class"], gone["withdrawn_date"]) == ("withdrawn", date(2011, 6, 16))
    suspended = [v for v in rows.values() if v.get("status_class") == "suspended"]
    assert suspended and all(v["status"] == "ACTIVE (Suspended)" for v in suspended)
    reasons = [v["study_phase"] for v in rows.values() if "Reason for withdrawal" in (v.get("study_phase") or "")]
    assert any("Failure to Cure Deficiency" in r for r in reasons)
    assert all(k[0].isdigit() for k in rows)  # legend and footnote rows are not entries


def test_refuses_files_that_are_not_the_report():
    with pytest.raises(ParseError, match="not an xlsx"):
        caiso.parse_workbook(b"<html>Access denied</html>")


def test_terms_note_quotes_caiso():
    terms = " ".join((DATA / "terms_of_use_excerpt.txt").read_text().split())
    clause = ("may be used by you provided that you keep intact all copyright, trademark and other proprietary "
              "notices and that you credit the California ISO when using such materials and/or information.")
    assert clause in terms and clause in caiso.TERMS_NOTE
    robots = (DATA / "robots.txt").read_text()
    assert "Disallow: /resources/*" in robots and "/documents" not in robots
    assert licensing.licence_for(caiso.SOURCE_NAME)["status"] == "public" and not caiso.SOURCE.blocked


def test_load_links_by_name_and_reruns_clean(committed):
    committed.execute("INSERT INTO site (slug, name, state, county, tracking_names) VALUES (%s, %s, %s, %s, %s)",
                      ("montezuma-test", "Montezuma", "CA", "Solano", []))
    sources.upsert_spec(caiso.SOURCE)
    doc = documents.record_document(caiso.SOURCE_ID, caiso.XLSX_URL, WORKBOOK.read_bytes(),
                                    content_type="application/xlsx")
    stats = RunStats()
    caiso.load(doc, stats, min_entries=1)
    report = stats.extra["iso"]["caiso"]
    assert report["linked"] == {"caiso:22": ["site:montezuma-test"]}
    link = committed.execute("SELECT value, tag, method_version FROM fact_current WHERE subject_id = 'caiso:22' "
                             "AND field = 'linked_site'").fetchone()
    assert link["tag"] == "M" and link["value"][0]["evidence"]["phrase"] == "montezuma"
    iso = committed.execute("SELECT value, source_name FROM fact_current WHERE subject_id = 'caiso:22' "
                            "AND field = 'iso'").fetchone()
    assert (iso["value"], iso["source_name"]) == ("CAISO", "CAISO")
    again = RunStats()
    caiso.load(doc, again, min_entries=1)
    assert again.facts_new == 0 and again.facts_seen == stats.facts_seen
    with pytest.raises(ParseError, match="truncated"):
        caiso.load(doc, RunStats())


def test_schedule_and_cli():
    assert app.tasks["collect.caiso_queue"].queue == "collect"
    crons = {name: t.cron for (name, _), t in app.periodic_registry.periodic_tasks.items()}
    assert crons["collect.caiso_queue"] == caiso.CRON_UTC
    assert cli.build_parser().parse_args(["collect", "caiso_queue"])._handler is caiso.collect_cli


@pytest.mark.network
def test_live_run_twice_writes_nothing_new(committed):
    first = caiso.run()
    assert first["ok"], first
    assert first["iso"]["caiso"]["entries"] > 1500 and first["facts_new"] > 0
    second = caiso.run()
    assert second["ok"], second
    assert second["facts_new"] == 0
