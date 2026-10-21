"""epe_queue: the El Paso Electric active queue PDF (real, gzipped for the repo), the link on EPE's page, the
terms that block it, a load (as it will run once unblocked), the schedule and the blocked live run (network)."""

from __future__ import annotations

import gzip
from datetime import date
from pathlib import Path

import pytest

from marden import cli
from marden.collectors import epe_queue as epe
from marden.collectors.rates_common import ParseError, RunStats
from marden.core import documents, sources
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "epe_queue"


def _pdf() -> bytes:
    return gzip.decompress((DATA / "epe-interconnection-queue_active.pdf.gz").read_bytes())


def test_finds_the_pdf_link():
    page = (DATA / "generator_interconnection_excerpt.html").read_bytes()
    assert epe.find_pdf(page) == "https://www.epelectric.com/el-paso-electric/uploads/epe-interconnection-queue_active.pdf"


def test_parses_the_queue_table():
    as_of, entries = epe.parse_pdf(_pdf())
    assert as_of == date(2026, 7, 7) and len(entries) == 50
    rows = {e.queue_id: e.values for e in entries}
    first = rows["LG-19-3-4"]
    assert (first["name"], first["mw_requested"], first["county"], first["state"], first["service"]) == (
        "CO100SB Project", 100, "Otero County", "NM", "NRIS")
    assert (first["status"], first["status_class"], first["proposed_cod"], first["queue_date"]) == (
        "LGIA EXECUTED", "active", "2027-04-01", date(2019, 3, 27))
    assert rows["LG-22-3-6"]["mw_requested"] == 155 and rows["LG-22-3-6"]["status_class"] == "withdrawn"  # 'LG 22-3-6'
    assert rows["LG-21-9-3"]["status_class"] == "suspended"
    gas = rows["Q26-LG-3-31"]
    assert (gas["mw_requested"], gas["fuel"], gas["project_type"], gas["county"]) == (
        1033.6, "Gas", "LGIP, EPE affiliate", "El Paso County")
    assert rows["Q26-LG-3-27"]["poi"] == "Springerville-Macho Springs 345 kV Line"  # wrapped cell read in order
    assert {v["balancing_authority"] for v in rows.values()} == {"EPE"}


def test_refuses_other_files():
    with pytest.raises(ParseError, match="not a PDF"):
        epe.parse_pdf(b"<html></html>")


def test_terms_block_the_source():
    terms = (DATA / "terms_of_use_excerpt.txt").read_text()
    quote = "For commercial use of any portion of the contents you must secure the written consent"
    assert quote in terms and quote in epe.TERMS_NOTE
    assert epe.SOURCE.blocked and "written consent" in epe.SOURCE.blocked_reason


def test_load_once_unblocked_and_rerun(committed):
    sources.upsert_spec(epe.SOURCE)
    doc = documents.record_document(epe.SOURCE_ID, "https://www.epelectric.com/x/epe-interconnection-queue_active.pdf",
                                    _pdf(), content_type="application/pdf")
    stats = RunStats()
    assert epe.load(doc, stats) == date(2026, 7, 7)
    assert stats.extra["iso"]["epe"]["entries"] == 50
    row = committed.execute("SELECT value, tag, source_name, valid_from FROM fact_current "
                            "WHERE subject_id = 'epe:Q26-LG-3-26-4' AND field = 'mw_requested'").fetchone()
    assert (row["value"], row["tag"], row["source_name"], row["valid_from"]) == (
        3000, "F", "El Paso Electric", date(2026, 7, 7))
    again = RunStats()
    epe.load(doc, again)
    assert again.facts_new == 0


def test_schedule_and_cli():
    assert app.tasks["collect.epe_queue"].queue == "collect"
    crons = {name: t.cron for (name, _), t in app.periodic_registry.periodic_tasks.items()}
    assert crons["collect.epe_queue"] == epe.CRON_UTC
    assert cli.build_parser().parse_args(["collect", "epe_queue"])._handler is epe.collect_cli


@pytest.mark.network
def test_live_run_is_refused_while_blocked(committed):
    out = epe.run()
    assert not out["ok"] and "SourceBlocked" in out["error"]
    assert committed.execute("SELECT count(*) AS n FROM document WHERE source_id = 'epe_queue'").fetchone()["n"] == 0
