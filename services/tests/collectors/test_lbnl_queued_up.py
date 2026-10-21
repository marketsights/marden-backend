"""lbnl_queued_up: edition discovery, the CC BY 4.0 check, the data file (real excerpt), ids for repeated
queue numbers, tag R, the cross-check link to an operator's own entry, a rerun, the schedule and a live run."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from marden import cli
from marden.collectors import bpa_queue as bpa
from marden.collectors import lbnl_queued_up as lbnl
from marden.collectors.rates_common import ParseError, RunStats
from marden.core import documents, licensing, sources
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "lbnl_queued_up"
WORKBOOK = DATA / "lbnl_ix_queue_data_file_thru2025_excerpt.xlsx"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DATA_URL = "https://eta-publications.lbl.gov/sites/default/files/2026-05/lbnl_ix_queue_data_file_thru2025.xlsx"


def test_finds_the_newest_edition_and_checks_its_licence():
    series = (DATA / "series_excerpt.html").read_bytes()
    assert lbnl.find_data_file(series) == (DATA_URL, 2025)
    assert lbnl.find_edition_page(series, 2025) == (
        "https://eta-publications.lbl.gov/publications/queued-2026-edition-characteristics")
    edition = (DATA / "queued-2026-edition_excerpt.html").read_bytes()
    lbnl.check_licence(edition, DATA_URL)
    with pytest.raises(ParseError, match="CC BY 4.0"):
        lbnl.check_licence(edition.replace(b"licensed", b"available"), DATA_URL)
    with pytest.raises(ParseError, match="does not link"):
        lbnl.check_licence(edition, DATA_URL.replace("thru2025", "thru2026"))


def test_parses_rows_with_unique_ids():
    rows = {e.queue_id: (entity, qid, e) for entity, qid, e in lbnl.parse_workbook(WORKBOOK.read_bytes())}
    entity, qid, e = rows["epe:LG-19-9 -1"]
    assert (entity, qid) == ("EPE", "LG-19-9 -1")
    v = e.values
    assert (v["name"], v["county"], v["state"], v["transmission_owner"], v["status_class"]) == (
        "VE100S Project", "Dona Ana", "NM", "El Paso Electric Company", "active")
    assert "iso" not in v and v["queue_date"] == date(2019, 9, 13)
    assert rows["pjm:AE1-070"][2].values["iso"] == "PJM"
    assert {"nyiso:2/1998-04-27", "nyiso:2/1998-04-27-2", "duke:2/1998-01-20", "duke:2/1998-01-20-2"} <= set(rows)
    assert "clpt:2011-03-01" in rows  # an id Excel had turned into a date
    hybrid = rows["avista:111"][2].values
    assert hybrid["fuel"] == "Solar+Battery" and hybrid["mw_requested"] == 375
    assert hybrid["mw_by_type"] == {"Solar": 375, "Battery": 185}
    assert rows["ip:BPA"][2].values.get("status") is None  # q_status 'unknown'
    assert rows["bpa:G2"][2].values["status_class"] == "in_service"  # 'operational'


def test_refuses_a_changed_file():
    with pytest.raises(ParseError, match="not an xlsx"):
        lbnl.parse_workbook(b"PK not really")


def test_terms_and_licence_entry():
    assert "CC BY 4.0" in lbnl.TERMS_NOTE and "GridTracker" in lbnl.ATTRIBUTION
    assert "attribute it to Lawrence Berkeley National Laboratory and GridTracker" in (
        DATA / "queued-2026-edition_excerpt.html").read_text()
    assert licensing.licence_for(lbnl.SOURCE_NAME)["status"] == "public"
    robots = (DATA / "robots.txt").read_text()
    rules = [line.split(":", 1)[1].strip() for line in robots.splitlines() if line.startswith("Disallow:")]
    for path in ("/research-areas/x", "/publications/x", "/sites/default/files/2026-05/x.xlsx"):
        assert not any(path.startswith(r) for r in rules), path


def test_load_is_tag_r_cross_links_operator_entries_and_reruns_clean(committed):
    sources.upsert_spec(bpa.SOURCE)
    bdoc = documents.record_document(bpa.SOURCE_ID, "https://www.bpa.gov/x.xlsx",
                                     (Path(__file__).parent / "data" / "bpa_queue" /
                                      "InterconnectionQueueOutput_excerpt.xlsx").read_bytes(), content_type=XLSX)
    bpa.load(bdoc, RunStats(), min_entries=1)
    committed.execute("INSERT INTO queue_project (id, iso, queue_id) VALUES ('pjm:AE1-070', 'pjm', 'AE1-070')")
    sources.upsert_spec(lbnl.SOURCE)
    doc = documents.record_document(lbnl.SOURCE_ID, DATA_URL, WORKBOOK.read_bytes(), content_type=XLSX)
    stats = RunStats()
    lbnl.load(doc, 2025, stats, min_entries=1)
    assert stats.extra["lbnl"]["operator_entry_matches"] == {"BPA": 2, "PJM": 1}
    assert stats.extra["iso"]["lbnl"]["entries"] == stats.extra["lbnl"]["rows"] == 47
    cur = {r["field"]: r for r in committed.execute(
        "SELECT field, value, tag, source_name, valid_from FROM fact_current WHERE subject_type = 'queue_project' "
        "AND subject_id = 'lbnl:bpa:G2'").fetchall()}
    assert cur["operator_entry"]["value"] == "bpa:G2"
    assert {(r["tag"], r["source_name"], r["valid_from"]) for r in cur.values()} == {
        ("R", "LBNL Queued Up", date(2025, 12, 31))}
    # the operator's own entry keeps its F values: LBNL rows are separate subjects
    own = committed.execute("SELECT tag, source_name FROM fact_current WHERE subject_id = 'bpa:G2' "
                            "AND field = 'status'").fetchone()
    assert (own["tag"], own["source_name"]) == ("F", "Bonneville Power Administration")
    assert committed.execute("SELECT queue_id FROM queue_project WHERE id = 'lbnl:epe:LG-19-9 -1'").fetchone()[
        "queue_id"] == "epe:LG-19-9 -1"
    again = RunStats()
    lbnl.load(doc, 2025, again, min_entries=1)
    assert again.facts_new == 0 and again.facts_seen == stats.facts_seen


def test_schedule_and_cli():
    assert app.tasks["collect.lbnl_queued_up"].queue == "collect"
    crons = {name: t.cron for (name, _), t in app.periodic_registry.periodic_tasks.items()}
    assert crons["collect.lbnl_queued_up"] == lbnl.CRON_UTC
    assert cli.build_parser().parse_args(["collect", "lbnl_queued_up"])._handler is lbnl.collect_cli


@pytest.mark.network
def test_live_run_twice_writes_nothing_new(committed):
    first = lbnl.run()
    assert first["ok"], first
    assert first["lbnl"]["rows"] > 30_000 and first["facts_new"] > 0
    second = lbnl.run()
    assert second["ok"], second
    assert second["facts_new"] == 0
