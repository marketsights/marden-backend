"""pjm_rpm: parsers on real PJM RPM files, facts / events / reruns on the test DB, schedule, live run."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from marden import cli
from marden.collectors import capacity_common as cap
from marden.collectors import capacity_zones, pjm_rpm
from marden.collectors import pjm_rpm_parse as parse
from marden.collectors.rates_common import ParseError, RunStats
from marden.core import db, documents, sources
from marden.core.facts import FactWriter
from marden.core.http import Fetcher
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "pjm_rpm"
BASE = "https://www.pjm.com/-/media/DotCom/markets-ops/rpm/rpm-auction-info"
BRA_2028 = parse.Listed(2028, "bra", f"{BASE}/2028-2029/2028-2029-bra-results.xlsx",
                        f"{BASE}/2028-2029/2028-2029-bra-results-report.pdf")
IA3_2026 = parse.Listed(2026, "3ia", f"{BASE}/2026-2027/2026-2027-3ia-results.xlsx",
                        f"{BASE}/2026-2027/2026-2027-3ia-report.pdf")


def _bytes(name: str) -> bytes:
    return (DATA / name).read_bytes()


def _parse(listed: parse.Listed, name: str) -> dict[str, dict[str, cap.Value]]:
    return parse.zone_results(parse.parse_results(_bytes(name), listed, name))


def test_finds_each_report_and_results_pair_on_the_rpm_page():
    listed, skipped = parse.find_auctions(_bytes("rpm_page_excerpt.html"))
    assert [x.period for x in listed] == [
        "2020-2021", "2020-2021-1ia", "2020-2021-2ia", "2020-2021-3ia", "2026-2027", "2026-2027-3ia", "2028-2029"]
    by_period = {x.period: x for x in listed}
    assert by_period["2028-2029"] == BRA_2028  # the results report, not the planning-parameters "Report PDF"
    assert by_period["2026-2027-3ia"] == IA3_2026
    assert by_period["2020-2021-1ia"].name == "2020/2021 First Incremental Auction"
    assert skipped == [f"2019-2020-{n}-incremental-auction-results.xls" for n in ("first", "second", "third")]  # pre-CP
    with pytest.raises(ParseError):
        parse.find_auctions(b"<html>maintenance</html>")


def test_parses_a_base_residual_auction_workbook():
    zones = _parse(BRA_2028, "2028-2029-bra-results.xlsx")
    rto, dom, aep = zones["RTO"], zones["DOM"], zones["AEP"]
    assert rto["clearing_price"] == cap.Value(Decimal(325), "USD/MW-day",
                                              "2028-2029-bra-results.xlsx 'BRA Resource Clearing Results'!D5 "
                                              "(Resource Clearing Price, RTO)")
    assert rto["cleared_mw"].value == Decimal("138317.8") and "zonal_capacity_price" not in rto
    assert dom["clearing_price"].value == 325 and dom["cleared_mw"].value == Decimal("21246.4")  # DOM is an LDA
    assert dom["zonal_capacity_price"].value == 325 and dom["lda"].value == "DOM"
    assert dom["zonal_ucap_obligation_mw"].value == Decimal("25104.549681")
    assert "clearing_price" not in aep and aep["lda"].value == "RTO"  # a zone, not an LDA: it clears in the RTO
    assert zones["AE"]["lda"].value == "EMAAC" and zones["BGE"]["lda"].value == "BGE"
    assert len(zones) == 28  # 17 LDAs + 21 zones, 10 codes shared


def test_an_incremental_auction_uses_its_own_sheets_only():
    zones = _parse(IA3_2026, "2026-2027-3ia-results_trimmed.xlsx")
    rto, dom = zones["RTO"], zones["DOM"]
    assert rto["clearing_price"].value == Decimal("164.7")  # the 3rd IA, not the BRA's 329.17
    assert rto["cleared_mw"].value == Decimal("3597.8") and rto["buy_bids_cleared_mw"].value == Decimal("3312.4")
    assert dom["zonal_capacity_price"].value == Decimal("329.077063")  # adjusted final zonal price (** column)
    assert "Adjusted Final Zonal Capacity Price**" in dom["zonal_capacity_price"].quote_loc


def test_pjm_layout_quirks():
    bra_2025 = parse.Listed(2025, "bra", f"{BASE}/2025-2026/2025-2026-base-residual-auction-results.xlsx", None)
    zones = _parse(bra_2025, "2025-2026-base-residual-auction-results_trimmed.xlsx")
    assert zones["DOM"]["clearing_price"].value == Decimal("444.26")
    assert zones["BGE"]["clearing_price"].value == Decimal("466.35")
    assert zones["DOM"]["lda"].value == "DOM"  # its LDA columns are blank in this file, but DOM was a modeled LDA
    ia1_2020 = parse.Listed(2020, "1ia", f"{BASE}/2020-2021-first-incremental-auction-results.xlsx", None)
    zones = _parse(ia1_2020, "2020-2021-first-incremental-auction-results_trimmed.xlsx")
    assert {"AEP", "EKPC", "DEOK"} <= set(zones)  # 'AEP #', 'EKPC#', 'DEOK #' carry footnote marks
    assert zones["RTO"]["clearing_price"].value == Decimal("42.9")
    assert "DOM" in zones and "clearing_price" not in zones["DOM"]  # DOM was no LDA in 2020/2021


def test_report_dates():
    ia = parse.report_dates(_bytes("2026-2027-3ia-report_p1-4.pdf"), "r.pdf")
    assert (ia.published, ia.opened) == (date(2026, 3, 9), date(2026, 2, 24))
    assert ia.published_loc == 'r.pdf: "results were posted on March 9, 2026"'
    old = parse.report_dates(_bytes("2020-2021-base-residual-auction-report_p1-3.pdf"), "r.pdf")
    assert (old.published, old.opened) == (date(2017, 5, 23), date(2017, 5, 10))
    cover = parse.report_dates(_bytes("2028-2029-bra-results-report_p1.pdf"), "r.pdf")
    assert (cover.published, cover.opened) == (date(2026, 7, 14), None) and "cover date" in cover.published_loc
    rerun = parse.report_dates(_bytes("2024-2025-base-residual-auction-report_p1.pdf"), "r.pdf")
    assert rerun.published == date(2024, 6, 18)  # the re-executed BRA's report: no stated date, PDF creation date
    assert "CreationDate" in rerun.published_loc


def test_changed_files_are_refused():
    with pytest.raises(ParseError, match="not an xlsx"):
        parse.parse_results(b"<html>Access denied</html>", BRA_2028, "x.xlsx")
    with pytest.raises(ParseError, match="no sheet"):  # a BRA workbook is not a 3rd IA workbook
        parse.parse_results(_bytes("2028-2029-bra-results.xlsx"), IA3_2026, "x.xlsx")
    with pytest.raises(ParseError, match="not a PDF"):
        parse.report_dates(b"<html>Access denied</html>", "x.pdf")
    with pytest.raises(ParseError, match="not a number"):
        cap.decimal("n/a", "price")


def _stored(listed: parse.Listed, workbook: str, report: str) -> tuple[documents.DocumentRow, documents.DocumentRow]:
    sources.upsert_spec(pjm_rpm.SOURCE)
    sid = pjm_rpm.SOURCE_ID
    wb = documents.record_document(sid, listed.results_url, _bytes(workbook), content_type="application/xlsx")
    pdf = documents.record_document(sid, listed.report_url, _bytes(report), content_type="application/pdf")
    return wb, pdf


def _site(committed, slug: str, *, v1: bool, iso: str, utility: str) -> None:
    committed.execute("INSERT INTO site (slug, name, state, in_v1) VALUES (%s, %s, 'VA', %s)", (slug, slug, v1))
    with db.transaction() as conn:
        facts = FactWriter(conn)
        for name, value in (("power_iso", iso), ("power_utility", utility)):
            facts.add("site", slug, name, value, tag="R", source_name="DCD", source_url="https://example.org/dcd",
                      valid_from=date(2026, 10, 17), producer="seed:test", status="published")


def test_load_writes_facts_once_and_announces_to_linked_v1_sites(committed):
    wb, pdf = _stored(BRA_2028, "2028-2029-bra-results.xlsx", "2028-2029-bra-results-report_p1.pdf")
    auction = pjm_rpm.build_auction(BRA_2028, wb, pdf)
    assert (auction.period, auction.published, auction.delivery_start, auction.delivery_end) == (
        "2028-2029", date(2026, 7, 14), date(2028, 6, 1), date(2029, 5, 31))
    first = RunStats()
    with db.transaction() as conn:
        cap.CapacityWriter(conn, first, source_name="PJM", producer=pjm_rpm.PRODUCER).write(auction)
    again = RunStats()
    with db.transaction() as conn:
        cap.CapacityWriter(conn, again, source_name="PJM", producer=pjm_rpm.PRODUCER).write(auction)
    assert first.facts_new == first.facts_seen > 0 and again.facts_new == 0
    row = committed.execute(
        "SELECT * FROM fact_current WHERE subject_type = 'series' AND subject_id = 'capacity:pjm:rto:2028-2029' "
        "AND field = 'clearing_price'").fetchone()
    assert (row["value"], row["unit"], row["tag"], row["source_name"], row["valid_from"]) == (
        325, "USD/MW-day", "F", "PJM", date(2026, 7, 14))
    assert row["source_url"] == BRA_2028.results_url and row["document_id"] == wb.id
    names = committed.execute(
        "SELECT value #>> '{}' AS v FROM fact WHERE subject_id = 'capacity:pjm:dom:2028-2029' "
        "AND field = 'auction_name'"
    ).fetchone()
    assert names["v"] == "2028/2029 Base Residual Auction"

    _site(committed, "test-dom", v1=True, iso="PJM (Dominion zone)", utility="Dominion Energy Virginia")
    _site(committed, "test-off-list", v1=False, iso="PJM", utility="Dominion Energy Virginia")
    stats = RunStats()
    with db.transaction() as conn:
        capacity_zones.assign(conn, stats)
        assert cap.emit_auction_events(conn, {"pjm": "PJM"}, stats) == 1
        assert cap.emit_auction_events(conn, {"pjm": "PJM"}, stats) == 0  # rerun: nothing new
    events = committed.execute("SELECT * FROM event WHERE kind = 'capacity_auction'").fetchall()
    assert [(e["subject_id"], e["event_date"], e["title"], e["status"], e["tag"]) for e in events] == [
        ("test-dom", date(2026, 7, 14), "PJM 2028/2029 Base Residual Auction: DOM zonal capacity price $325.00/MW-day",
         "published", "F")]
    zone = committed.execute("SELECT value #>> '{}' AS v FROM fact_current WHERE subject_id = 'test-dom' "
                             "AND field = 'capacity_zone'").fetchone()
    assert zone["v"] == "DOM"


def test_a_failed_fetch_is_recorded_and_nothing_written(committed, monkeypatch):
    monkeypatch.setattr(pjm_rpm, "PAGE_URL", "http://127.0.0.1:9/rpm")
    out = pjm_rpm.run(fetcher=Fetcher(max_retries=0, timeout=2))
    assert out["ok"] is False and out["error"].startswith("FetchError")
    src = committed.execute("SELECT last_error, last_ok_at FROM source WHERE id = 'pjm_rpm'").fetchone()
    assert "FetchError" in src["last_error"] and src["last_ok_at"] is None
    assert committed.execute("SELECT count(*) AS n FROM fact").fetchone()["n"] == 0


def test_scheduled_monthly_in_the_collect_queue():
    task = app.tasks["collect.pjm_rpm"]
    assert task.queue == "collect"
    crons = {pid: pt.cron for (name, pid), pt in app.periodic_registry.periodic_tasks.items()
             if name == "collect.pjm_rpm"}
    assert crons == {"monthly_2nd_1353_utc": "53 13 2 * *"}
    assert "pjm_rpm" in cli._groups["collect"]
    assert not sources.get_spec("pjm_rpm").blocked


@pytest.mark.network
def test_live_run_twice_writes_nothing_new(committed):
    first = pjm_rpm.run(years=[2028])
    assert first["ok"], first
    assert first["documents_new"] >= 3 and first["facts_new"] > 0
    assert [a["period"] for a in first["loaded"]] == ["2028-2029"]
    second = pjm_rpm.run(years=[2028])
    assert second["ok"], second
    assert second["facts_new"] == 0 and second["facts_seen"] == first["facts_seen"]
    assert second["documents_new"] <= 1  # only the RPM page, whose HTML changes on every request
