"""spp_queue: the OpsPortal GI summary CSV (real excerpt), layout guards, terms, a load and a rerun, no links
without names, the schedule and a live run (network)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from marden import cli
from marden.collectors import spp_queue as spp
from marden.collectors.rates_common import ParseError, RunStats
from marden.core import documents, licensing, sources
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "spp_queue"
CSV = DATA / "GI_SummaryCSV_excerpt.csv"


def test_parses_the_summary_csv():
    as_of, entries = spp.parse_csv(CSV.read_bytes())
    assert as_of == date(2026, 10, 20) and len(entries) == 30
    rows = {e.queue_id: e.values for e in entries}
    gas = rows["GEN-2024-379"]
    assert (gas["county"], gas["state"], gas["transmission_owner"], gas["fuel"], gas["mw_requested"]) == (
        "Bossier Parish", "LA", "AEP", "Thermal: Gas", 1350)
    assert (gas["status"], gas["status_class"], gas["queue_date"], gas["proposed_cod"]) == (
        "DISIS STAGE", "active", date(2025, 3, 1), "2031-12-15")
    assert (gas["cycle"], gas["zone"], gas["poi"]) == (
        "DISIS-2024-001", "04 SOUTHEAST", "Longwood - El Dorado 345 kV Line")
    wind = rows["TI-18-0827"]
    assert wind["status_class"] == "in_service" and wind["actual_cod"] == "2022-06-24" and "proposed_cod" not in wind
    assert "name" not in wind and "developer" not in wind  # SPP publishes neither
    term = rows["GEN-2023-187"]
    assert term["status_class"] == "withdrawn" and term["withdrawn_date"] == date(2023, 12, 7)
    assert rows["GI-DISIS-2022-41"]["status_class"] == "suspended"
    assert rows["GI-TC-2024-25"]["poi"] == "Ault - Laramie River Station 345 Line"  # a cell over two lines
    assert rows["GI-TC-2024-25"]["study_phase"] == (
        "FACILITY STUDY STAGE; Cause of delay: GIA delayed due to Affected Systems Study")
    assert rows["GI-SISS-2026-04"]["county"] == "Kit Carson County"  # trailing comma dropped
    assert "status" not in rows["ASGI-2024-024"]  # published as the word 'None'


def test_refuses_error_pages_and_layout_changes():
    with pytest.raises(ParseError, match="Last Updated On"):
        spp.parse_csv(b"<html><body>404 Page Not Found</body></html>")
    changed = CSV.read_bytes().replace(b"TO at POI", b"Transmission Owner", 1)
    with pytest.raises(ParseError, match="columns changed"):
        spp.parse_csv(changed)


def test_terms_note_quotes_spp_and_flags_commercial_use():
    terms = " ".join((DATA / "terms_conditions_excerpt.txt").read_text().split())
    clause = ("Any commercial use of these materials requires prior, express written authorization from the "
              "author(s) or a duly authorized officer of SPP.")
    assert clause in terms and clause in spp.TERMS_NOTE
    aup = (DATA / "external_systems_aup_excerpt.txt").read_text()
    assert "polling of systems at a frequency higher than necessary for basic reconciliation of data" in aup
    assert "polling of systems at a frequency higher than necessary for basic reconciliation of data" in spp.TERMS_NOTE
    assert not spp.SOURCE.blocked and licensing.is_unlicensed(spp.SOURCE_NAME)


def test_load_and_rerun_without_links(committed):
    # Harrison County TX holds GEN-2024-377; with no name in the queue, nothing can link to it.
    committed.execute("INSERT INTO site (slug, name, state, county, tracking_names) VALUES (%s, %s, %s, %s, %s)",
                      ("harrison-test", "Pirkey Data Campus", "TX", "Harrison", ["Pirkey Compute LLC"]))
    sources.upsert_spec(spp.SOURCE)
    doc = documents.record_document(spp.SOURCE_ID, spp.CSV_URL, CSV.read_bytes(), content_type="text/csv")
    stats = RunStats()
    spp.load(doc, stats, min_entries=1)
    report = stats.extra["iso"]["spp"]
    assert report["entries"] == 30 and report["linked"] == {}
    n = committed.execute("SELECT count(*) AS n FROM queue_project WHERE iso = 'spp'").fetchone()["n"]
    assert n == 30
    row = committed.execute("SELECT value, tag, source_name, source_url, valid_from, producer FROM fact_current "
                            "WHERE subject_id = 'spp:GEN-2024-377' AND field = 'status'").fetchone()
    assert (row["value"], row["tag"], row["source_name"], row["source_url"], row["producer"]) == (
        "DISIS STAGE", "F", "SPP", spp.CSV_URL, "collector:spp_queue")
    assert row["valid_from"] <= date(2026, 10, 20)
    again = RunStats()
    spp.load(doc, again, min_entries=1)
    assert again.facts_new == 0 and again.facts_seen == stats.facts_seen
    with pytest.raises(ParseError, match="truncated"):
        spp.load(doc, RunStats())


def test_schedule_and_cli():
    assert app.tasks["collect.spp_queue"].queue == "collect"
    crons = {name: t.cron for (name, _), t in app.periodic_registry.periodic_tasks.items()}
    assert crons["collect.spp_queue"] == spp.CRON_UTC
    assert cli.build_parser().parse_args(["collect", "spp_queue"])._handler is spp.collect_cli
    assert spp.SOURCE.cadence_minutes == 7 * 24 * 60


@pytest.mark.network
def test_live_run_twice_writes_nothing_new(committed):
    first = spp.run()
    assert first["ok"], first
    assert first["iso"]["spp"]["entries"] > 2000 and first["facts_new"] > 0
    second = spp.run()
    assert second["ok"], second
    assert second["facts_new"] == 0
