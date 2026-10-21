"""isone_fca: parsers on real ISO-NE files, facts / zones / events / reruns on the test DB, schedule, live run."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from marden import cli
from marden.collectors import capacity_common as cap
from marden.collectors import capacity_zones, isone_fca
from marden.collectors import isone_fca_parse as parse
from marden.collectors.rates_common import ParseError, RunStats
from marden.core import db, documents, sources
from marden.core.facts import FactWriter
from marden.core.http import Fetcher
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "isone_fca"
PR_18 = "https://www.iso-ne.com/static-assets/documents/100008/20240209_pr_fca18_initial_results.pdf"


def _bytes(name: str) -> bytes:
    return (DATA / name).read_bytes()


def test_parses_the_results_report():
    sections = parse.parse_report(_bytes("fca-results-report_excerpt.pdf"), first_fca=14)
    assert sorted(sections) == [14, 15, 16, 17, 18]
    s18 = sections[18]
    assert (s18.ccp_start, s18.ccp_end, s18.auction_date) == (date(2027, 6, 1), date(2028, 5, 31), date(2024, 2, 5))
    assert {z: v[0] for z, v in s18.prices.items()} == {"ROP": Decimal("3.58"), "NNE": Decimal("3.58"),
                                                          "MAINE": Decimal("3.58")}
    assert s18.prices["ROP"][1].startswith("The payment rate for the Rest of Pool, Northern New England, and Maine")
    assert {z: v[0] for z, v in sections[17].prices.items()} == {z: Decimal("2.59") for z in ("ROP", "NNE", "MAINE")}
    assert {z: str(v[0]) for z, v in sections[16].prices.items()} == {
        "SENE": "2.639", "ROP": "2.591", "NNE": "2.531", "MAINE": "2.531"}
    assert {z: str(v[0]) for z, v in sections[15].prices.items()} == {  # "closed in round 4 with a Payment Rate of"
        "SENE": "3.98", "ROP": "2.611", "NNE": "2.477", "MAINE": "2.477"}
    assert sections[14].prices == {} and sections[14].system_price[0] == Decimal("2.001")  # "The system closed ..."
    assert parse.unknown_sections(_bytes("fca-results-report_excerpt.pdf")) == []


def test_interface_only_rates_are_not_zone_prices():
    zones, system = parse.prices_in(
        "The payment rate for the New Brunswick Interface is 2.551/kW-month. Phase I/II HQ Excess Interface closed "
        "with a Payment Rate of $ 3.701/kW Month and the New Brunswick Interface closed with a Payment Rate of "
        "$3.155/kW Month which concluded the auction.")
    assert zones == {} and system is None
    with pytest.raises(ParseError, match="two payment rates"):
        parse.prices_in("The Maine Capacity Zone closed with a Payment Rate of $2.1/kW-month and $2.2/kW-month.")


def test_parses_the_obligations_workbook():
    obligations = parse.parse_obligations(_bytes("fca_obligations_excerpt.xlsx"), first_fca=15)
    assert sorted(obligations) == [15, 17, 18]
    o18 = obligations[18]
    assert (o18.sheet, o18.month) == ("FCA 18", date(2027, 6, 1))
    assert sorted(o18.zones) == ["MAINE", "NNE", "ROP"]
    assert o18.zones["ROP"].load_zones == ["CT", "NEMA", "RI", "SEMA", "WCMA"]
    assert (o18.zones["MAINE"].mw, o18.zones["MAINE"].resources) == (Decimal("13.75"), 2)
    # FCA 17 lists SEMA/RI/NEMA resources under zone id 8506 but names the zone Rest-of-Pool: one ROP zone
    assert sorted(obligations[17].zones) == ["MAINE", "NNE", "ROP"]
    assert "SEMA" in obligations[17].zones["ROP"].load_zones
    assert obligations[15].zones["SENE"].load_zones == ["NEMA", "RI", "SEMA"]


def test_key_stats_page_and_press_release():
    page = _bytes("keystats_markets_excerpt.html")
    assert parse.keystats_totals(page)[18] == Decimal(31556)
    assert parse.press_releases(page)[18] == PR_18
    assert parse.release_date(_bytes("20240209_pr_fca18_initial_results_p1.pdf"), "pr.pdf") == (
        date(2024, 2, 9), 'pr.pdf: "Holyoke, MA—February 9, 2024"')


def test_changed_files_are_refused():
    with pytest.raises(ParseError, match="not a PDF"):
        parse.parse_report(b"<html>Throttled</html>")
    with pytest.raises(ParseError, match="not an xlsx"):
        parse.parse_obligations(b"<html>Throttled</html>")
    with pytest.raises(ParseError, match="unknown ISO-NE capacity zone"):
        parse.zone_code("Connecticut")


def test_obligation_sums_must_match_the_published_totals():
    obligations = parse.parse_obligations(_bytes("fca_obligations_excerpt.xlsx"), first_fca=18)
    total = sum(z.mw for z in obligations[18].zones.values())
    stats = RunStats()
    isone_fca.check_totals(obligations, {18: total.quantize(Decimal(1))}, stats)
    assert stats.extra["total_mw_check"] == {"18": [float(total), float(total.quantize(Decimal(1)))]}
    with pytest.raises(ParseError, match="differs from ISO-NE's total"):
        isone_fca.check_totals(obligations, {18: Decimal(31556)}, RunStats())  # the excerpt holds few resources


def _stored() -> tuple[documents.DocumentRow, ...]:
    sources.upsert_spec(isone_fca.SOURCE)
    report = documents.record_document(isone_fca.SOURCE_ID, parse.REPORT_URL, _bytes("fca-results-report_excerpt.pdf"),
                                       content_type="application/pdf")
    obl = documents.record_document(isone_fca.SOURCE_ID, parse.OBLIGATIONS_URL, _bytes("fca_obligations_excerpt.xlsx"),
                                    content_type="application/xlsx")
    pr = documents.record_document(isone_fca.SOURCE_ID, PR_18, _bytes("20240209_pr_fca18_initial_results_p1.pdf"),
                                   content_type="application/pdf")
    return report, obl, pr


def test_fca_facts_zones_and_events(committed):
    report, obl, pr = _stored()
    section = parse.parse_report(report.read(), first_fca=18)[18]
    obligations = parse.parse_obligations(obl.read(), first_fca=18)[18]
    auction = isone_fca.build_auction(section, obligations, report, obl, parse.release_date(pr.read(), "pr.pdf"))
    for expect_new in (True, False):
        stats = RunStats()
        with db.transaction() as conn:
            cap.CapacityWriter(conn, stats, source_name="ISO New England", producer=isone_fca.PRODUCER).write(auction)
        assert (stats.facts_new > 0) is expect_new
    rows = {r["field"]: r for r in committed.execute(
        "SELECT * FROM fact_current WHERE subject_type = 'series' AND subject_id = 'capacity:isone:rop:2027-2028'")}
    assert rows["clearing_price"]["value"] == 117.7 and rows["clearing_price"]["unit"] == "USD/MW-day"
    assert rows["clearing_price"]["quote_loc"].startswith(
        "converted by Marden: 3.58 USD/kW-month x 1000 / 30.4167 = 117.70 USD/MW-day")
    assert rows["clearing_price_native"]["value"] == 3.58 and rows["clearing_price_native"]["unit"] == "USD/kW-month"
    assert rows["clearing_price_native"]["source_url"] == parse.REPORT_URL
    assert rows["cleared_mw"]["source_url"] == parse.OBLIGATIONS_URL and rows["cleared_mw"]["document_id"] == obl.id
    assert rows["cleared_mw"]["value"] == float(obligations.zones["ROP"].mw)
    assert rows["load_zones"]["value"] == ["CT", "NEMA", "RI", "SEMA", "WCMA"]
    assert rows["auction_name"]["value"] == "FCA 18" and rows["auction_date"]["value"] == "2024-02-05"
    assert {r["valid_from"] for r in rows.values()} == {date(2024, 2, 9)}  # the results press release

    committed.execute("INSERT INTO site (slug, name, state, in_v1) VALUES ('test-ma', 'Test MA', 'MA', true)")
    with db.transaction() as conn:
        FactWriter(conn).add("site", "test-ma", "iso", "ISO-NE", tag="F", source_name="HIFLD",
                             source_url="https://example.org/iso.gpkg", valid_from=date(2026, 9, 1),
                             producer="collector:grid_join", status="published")
        capacity_zones.assign(conn, RunStats())
        assert cap.emit_auction_events(conn, isone_fca.EVENT_MARKETS, RunStats()) == 1
    zone = committed.execute(
        "SELECT * FROM fact_current WHERE subject_id = 'test-ma' AND field = 'capacity_zone'").fetchone()
    assert zone["value"] == "ROP" and zone["tag"] == "M" and zone["valid_from"] == date(2026, 9, 1)
    assert "capacity zone holding state MA in 2027-2028" in zone["quote_loc"]
    event = committed.execute("SELECT * FROM event WHERE kind = 'capacity_auction'").fetchone()
    assert event["title"] == "ISO-NE FCA 18: ROP clearing price $117.70/MW-day"
    assert event["event_date"] == date(2024, 2, 9) and event["source_url"] == parse.REPORT_URL


def test_a_failed_fetch_is_recorded(committed, monkeypatch):
    monkeypatch.setattr(isone_fca, "KEYSTATS_URL", "http://127.0.0.1:9/key-stats")
    out = isone_fca.run(fetcher=Fetcher(max_retries=0, timeout=2))
    assert out["ok"] is False and out["error"].startswith("FetchError")
    assert committed.execute("SELECT ok FROM source_run WHERE source_id = 'isone_fca'").fetchone()["ok"] is False
    assert committed.execute("SELECT count(*) AS n FROM fact").fetchone()["n"] == 0


def test_scheduled_monthly_in_the_collect_queue():
    assert app.tasks["collect.isone_fca"].queue == "collect"
    crons = {pid: pt.cron for (name, pid), pt in app.periodic_registry.periodic_tasks.items()
             if name == "collect.isone_fca"}
    assert crons == {"monthly_4th_1453_utc": "53 14 4 * *"}
    assert "isone_fca" in cli._groups["collect"]
    assert not sources.get_spec("isone_fca").blocked and "non-personal use" in isone_fca.TERMS_NOTE


@pytest.mark.network
def test_live_run_twice_writes_nothing_new(committed):
    first = isone_fca.run()
    assert first["ok"], first
    assert first["fcas"][:4] == [15, 16, 17, 18] and first["facts_new"] > 0
    assert {"18", "17", "16", "15"} <= set(first["total_mw_check"])
    second = isone_fca.run()
    assert second["ok"], second
    assert second["facts_new"] == 0 and second["facts_seen"] == first["facts_seen"]
