"""pjm_queue: both real PJM exports (excerpts), cycle rows superseding moved serial rows, a plant link,
reruns, terms, the schedule and a live POST of the cycle export (network)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from marden import cli
from marden.collectors import pjm_queue as pjm
from marden.collectors.rates_common import ParseError, RunStats
from marden.core import documents, sources
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "pjm_queue"
SERIAL = DATA / "PlanningQueues_excerpt.xml"
CYCLE = DATA / "CycleProjects-All_excerpt.xml"


def test_parses_the_serial_export():
    rows = {e.queue_id: e for e in pjm.parse_serial(SERIAL.read_bytes())}
    assert "AE1-070" in rows and not any("moved" in qid for qid in rows)  # "AE1-070 - moved to TC1"
    a01 = rows["A01"].values
    assert (a01["name"], a01["poi"], a01["state"], a01["county"], a01["status"], a01["mw_requested"]) == (
        "Ironwood", "South Lebanon 230 KV", "PA", "Lebanon", "In Service", 720)
    assert a01["queue_date"] == date(1997, 4, 1) and a01["status_class"] == "in_service"
    assert a01["study_phase"].startswith("Interconnection Service Agreement")
    assert rows["U1-004 865838"].values["status_class"] == "withdrawn"  # Retracted
    assert rows["B14"].values["status_class"] == "retired"  # Deactivated
    assert rows["994856"].values["project_type"] == "Long-Term Firm Transmission"


def test_parses_the_cycle_export():
    rows = {e.queue_id: e for e in pjm.parse_cycle(CYCLE.read_bytes())}
    juliet = rows["AF2-126"]
    assert juliet.values["developer"] == "Juliet Energy Project, LLC" and juliet.values["cycle"] == "TC1"
    assert juliet.values["status"] == "UC-ISP" and juliet.values["status_class"] == "under_construction"
    assert rows["AH1-563"].values["withdrawn_date"] == date(2026, 9, 30)
    assert rows["AH1-563"].updated == date(2026, 6, 2) and rows["AH1-563"].values["proposed_cod"] == "2029-06-01"
    assert "county" not in rows["AH1-570"].values  # "Not Specified"


def test_refuses_other_documents():
    with pytest.raises(ParseError):
        pjm.parse_serial(b"<html>maintenance</html>")
    with pytest.raises(ParseError, match="root"):
        pjm.parse_cycle(b"<?xml version='1.0'?><Error>down</Error>")


def test_terms_note_quotes_pjm():
    legal = (DATA / "legal_excerpt.txt").read_text()
    for part in ("Transmitting information to PJM member companies, partners, the electric industry, and the public "
                 "is an important function of PJM.", "Access to this website does not confer any license or ownership "
                 "interest in either the form or content of the website"):
        assert part in legal and part in pjm.TERMS_NOTE
    robots = (DATA / "robots.txt").read_text()
    assert "Disallow: /xsl/" in robots and "pjmfiles" not in robots and "Disallow: /m/" not in robots


def _store(committed) -> tuple[documents.DocumentRow, documents.DocumentRow]:
    sources.upsert_spec(pjm.SOURCE)
    serial = documents.record_document(pjm.SOURCE_ID, pjm.SERIAL_URL, SERIAL.read_bytes(), content_type="text/xml",
                                       meta={"http": {"last_modified": "Wed, 21 Oct 2026 12:51:38 GMT"}})
    cycle = documents.record_document(pjm.SOURCE_ID, pjm.CYCLE_URL, CYCLE.read_bytes(), content_type="text/xml")
    return serial, cycle


def test_load_prefers_cycle_rows_links_a_plant_and_reruns_clean(committed):
    committed.execute("INSERT INTO power_plant (id, name, county, state) VALUES "
                      "('juliet-energy', 'Juliet Energy', 'Wood', 'OH')")
    serial, cycle = _store(committed)
    stats = RunStats()
    pjm.load(serial, cycle, stats, min_rows=(1, 1))
    assert stats.extra["pjm_rows"]["serial_superseded_by_cycle"] == 3  # AE1-070, AH1-127, AG1-374
    status = committed.execute("SELECT * FROM fact_current WHERE subject_id = 'pjm:AE1-070' AND field = 'status'"
                               ).fetchone()
    assert status["value"] == "Withdrawn" and status["source_url"] == pjm.CYCLE_PAGE  # the cycle row, not serial
    assert status["document_id"] == cycle.id and status["valid_from"] == date(2025, 1, 27)  # its LastUpdated
    a01 = committed.execute("SELECT valid_from, source_url FROM fact_current WHERE subject_id = 'pjm:A01' "
                            "AND field = 'status'").fetchone()
    assert a01["valid_from"] == date(2026, 10, 21) and a01["source_url"] == pjm.SERIAL_URL  # Last-Modified day
    link = committed.execute("SELECT * FROM fact_current WHERE subject_id = 'pjm:AF2-126' AND field = 'linked_plant'"
                             ).fetchone()
    assert link["tag"] == "M" and [x["id"] for x in link["value"]] == ["juliet-energy"]
    assert link["value"][0]["evidence"]["phrase"] == "juliet"
    n = committed.execute("SELECT count(*) AS n FROM queue_project WHERE iso = 'pjm'").fetchone()["n"]
    assert n == 19 + 11 - 3
    again = RunStats()
    pjm.load(serial, cycle, again, min_rows=(1, 1))
    assert again.facts_new == 0 and again.facts_seen == stats.facts_seen
    with pytest.raises(ParseError, match="truncated"):
        pjm.load(serial, cycle, RunStats())


def test_schedule_and_cli():
    assert app.tasks["collect.pjm_queue"].queue == "collect"
    crons = {name: t.cron for (name, _), t in app.periodic_registry.periodic_tasks.items()}
    assert crons["collect.pjm_queue"] == pjm.CRON_UTC
    assert cli.build_parser().parse_args(["collect", "pjm_queue"])._handler is pjm.collect_cli


@pytest.mark.network
def test_live_cycle_export_is_public_xml(committed):
    sources.upsert_spec(pjm.SOURCE)
    doc = pjm.fetch_cycle()
    assert doc.meta["http"]["method"] == "POST"
    rows = pjm.parse_cycle(doc.read())
    assert len(rows) > 1000 and {e.values.get("cycle") for e in rows} >= {"TC1", "TC2"}
