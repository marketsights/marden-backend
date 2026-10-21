"""nyiso_icap: parsers on real ICAP AMS pages, facts, events and reruns on the test DB, schedule, live run."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from marden import cli
from marden.collectors import capacity_zones
from marden.collectors import nyiso_icap as ny
from marden.collectors.rates_common import ParseError, RunStats
from marden.core import db, documents, sources
from marden.core.facts import FactWriter
from marden.core.http import Fetcher
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "nyiso_icap"
TODAY = date(2026, 10, 21)


def _seasons() -> dict[str, ny.Season]:
    return {s.description: s for s in ny.parse_seasons((DATA / "seasons_excerpt.json").read_bytes())}


def _page(name: str, kind: str) -> ny.PageResults:
    page = ny.parse_page((DATA / name).read_bytes(), kind)
    assert page is not None
    return page


def _stored(name: str, kind: str, season: ny.Season, month: date | None = None) -> documents.DocumentRow:
    sources.upsert_spec(ny.SOURCE)
    return documents.record_document(
        ny.SOURCE_ID, ny.page_url(kind, season, month), (DATA / name).read_bytes(), content_type="text/html",
        doc_type=f"nyiso_icap_{kind}_html",
    )


def _site_in_zone(committed, slug: str, power_iso: str) -> None:
    committed.execute("INSERT INTO site (slug, name, state, in_v1) VALUES (%s, %s, 'NY', true)", (slug, slug))
    with db.transaction() as conn:
        FactWriter(conn).add("site", slug, "power_iso", power_iso, tag="F", source_name="SEC EDGAR",
                             source_url="https://www.sec.gov/example", valid_from=date(2026, 10, 17),
                             producer="seed:test", status="published")
        capacity_zones.assign(conn, RunStats())


def _fact(committed, sid: str, field: str):
    return committed.execute(
        "SELECT * FROM fact_current WHERE subject_type = 'series' AND subject_id = %s AND field = %s", (sid, field)
    ).fetchone()


def test_parses_the_strip_auction_page():
    page = _page("strip_summer_2026.html", "strip")
    assert (page.season, page.first_month, page.posted) == ("Summer 2026", date(2026, 5, 1), date(2026, 4, 1))
    assert page.values["NYC"][0] == {"price": Decimal("19.28"), "awarded": Decimal("475.4")}
    assert page.values["G-J Locality"][0]["price"] == Decimal("5.35")
    found = ny.auctions(page, "strip", _seasons()["Summer 2026"], source_url="u", document_id=None)
    [(auction, headline, title)] = found
    assert (auction.period, auction.name, headline) == ("summer-2026-strip", "NYISO Summer 2026 Strip Auction", True)
    assert (auction.delivery_start, auction.delivery_end) == (date(2026, 5, 1), date(2026, 10, 31))
    nyc = next(z for z in auction.zones if z.zone == "NYC")
    assert nyc.values["clearing_price"].value == Decimal("633.86")  # 19.28 x 1000 / 30.4167
    assert nyc.values["clearing_price_native"].value == Decimal("19.28")
    assert [z.zone for z in auction.zones] == ["NYCA", "G-J", "NYC", "LI"]  # external areas are not written


def test_monthly_auction_prices_every_remaining_month_and_heads_the_first():
    page = _page("monthly_2026_06.html", "monthly")
    assert page.columns == [date(2026, m, 1) for m in range(6, 11)]
    found = ny.auctions(page, "monthly", _seasons()["Summer 2026"], source_url="u", document_id=None)
    assert [a.period for a, _, _ in found][:2] == ["2026-06-monthly-2026-06", "2026-07-monthly-2026-06"]
    assert [h for _, h, _ in found] == [True, False, False, False, False]
    july = found[1][0]
    nyc = next(z for z in july.zones if z.zone == "NYC")
    assert nyc.values["clearing_price_native"].value == Decimal("30.16")
    assert nyc.values["cleared_mw"].value == Decimal("16.2")
    assert july.name == "NYISO Monthly Auction for Jun 2026 (Jul 2026 capacity)"


def test_spot_auction_clears_deficiency_plus_excess():
    page = _page("spot_2026_10.html", "spot")
    assert (page.first_month, page.posted) == (date(2026, 10, 1), date(2026, 9, 29))
    [(auction, _, _)] = ny.auctions(page, "spot", _seasons()["Summer 2026"], source_url="u", document_id=None)
    nyca = next(z for z in auction.zones if z.zone == "NYCA")
    assert nyca.values["cleared_mw"].value == Decimal("6268.7") + Decimal("1647.9")
    assert nyca.values["clearing_price_native"].value == Decimal("6.00")
    assert auction.period == "2026-10-spot"


def test_unposted_and_changed_pages():
    assert ny.parse_page((DATA / "spot_2026_11_not_posted.html").read_bytes(), "spot") is None
    with pytest.raises(ParseError, match="neither results"):
        ny.parse_page(b"<html><body>Service unavailable</body></html>", "strip")
    page = _page("strip_summer_2026.html", "strip")
    with pytest.raises(ParseError, match="asked for"):
        ny.auctions(page, "strip", _seasons()["Winter 2026-2027"], source_url="u", document_id=None)
    broken = (DATA / "strip_summer_2026.html").read_bytes().replace(b">NYC</td>", b">New York City</td>")
    with pytest.raises(ParseError, match="localities"):
        ny.parse_page(broken, "strip")


def test_seasons_due_and_page_urls():
    seasons = ny.parse_seasons((DATA / "seasons_excerpt.json").read_bytes())
    due = [s.description for s in ny.seasons_due(seasons, TODAY)]
    assert due == ["Summer 2021", "Winter 2025-2026", "Summer 2026", "Winter 2026-2027"]
    winter = _seasons()["Winter 2026-2027"]
    assert [m.month for m in winter.months()] == [11, 12, 1, 2, 3, 4]
    urls = ny.pages(winter)
    assert len(urls) == 13
    assert urls[0][1] == f"{ny.BASE}/public/auc_view_strip_detail.do?seasonId=705521&display=Display"
    assert urls[7][1] == f"{ny.BASE}/public/auc_view_spot_detail.do?seasonId=705521&month=11/2026&display=Display"
    assert [ny.series_zone(z) for z in ("A", "F", "G", "I", "J", "K", "Z")] == [
        "nyca", "nyca", "g-j", "g-j", "nyc", "li", None]


def test_load_writes_facts_once_and_announces_to_linked_v1_sites(committed):
    _site_in_zone(committed, "lake-mariner", "NYISO (Zone A)")
    winter = _seasons()["Winter 2026-2027"]
    doc = _stored("strip_winter_2026_2027.html", "strip", winter)
    first = RunStats()
    assert ny.load_document(doc, "strip", winter, first, today=TODAY) == 1
    assert first.facts_new == 4 * 6  # 4 localities x (name, start, end, price, native price, MW)
    sid = "capacity:nyiso:nyca:winter-2026-2027-strip"
    price = _fact(committed, sid, "clearing_price")
    assert (price["value"], price["unit"], price["tag"], price["source_name"]) == (115.07, "USD/MW-day", "F", "NYISO")
    assert price["valid_from"] == date(2026, 10, 2) and price["source_url"] == doc.url
    assert price["document_id"] == doc.id and price["status"] == "published"
    assert _fact(committed, sid, "clearing_price_native")["value"] == 3.5
    assert _fact(committed, sid, "cleared_mw")["value"] == 1536.2
    assert _fact(committed, sid, "delivery_end")["value"] == "2027-04-30"
    events = committed.execute("SELECT * FROM event WHERE kind = 'capacity_auction'").fetchall()
    assert [(e["subject_id"], e["event_date"], e["status"]) for e in events] == [
        ("lake-mariner", date(2026, 10, 2), "published")]
    assert events[0]["title"] == "NYISO Winter 2026-2027 Strip Auction: NYCA $3.50/kW-month ($115.07/MW-day)"

    again = RunStats()
    ny.load_document(doc, "strip", winter, again, today=TODAY)
    assert again.facts_new == 0 and again.facts_seen == first.facts_seen
    assert committed.execute("SELECT count(*) AS n FROM event").fetchone()["n"] == 1


def test_only_the_prompt_month_and_recent_auctions_are_announced(committed):
    _site_in_zone(committed, "lake-mariner", "NYISO (Zone A)")
    seasons = _seasons()
    ny.load_document(_stored("monthly_2026_11.html", "monthly", seasons["Winter 2026-2027"], date(2026, 11, 1)),
                     "monthly", seasons["Winter 2026-2027"], RunStats(), today=TODAY)
    ny.load_document(_stored("strip_summer_2026.html", "strip", seasons["Summer 2026"]), "strip",
                     seasons["Summer 2026"], RunStats(), today=TODAY)  # posted 2026-04-01: history, no event
    titles = [r["title"] for r in committed.execute("SELECT title FROM event ORDER BY id").fetchall()]
    assert titles == ["NYISO Monthly Auction for Nov 2026: NYCA $2.72/kW-month ($89.42/MW-day)"]
    april = _fact(committed, "capacity:nyiso:nyc:2027-04-monthly-2026-11", "clearing_price_native")
    assert april["value"] == 4.56


def test_a_failed_fetch_is_recorded_and_nothing_written(committed, monkeypatch):
    monkeypatch.setattr(ny, "SEASONS_URL", "http://127.0.0.1:9/seasons")
    out = ny.run(today=TODAY, fetcher=Fetcher(max_retries=0, timeout=2))
    assert out["ok"] is False and out["error"].startswith("FetchError")
    src = committed.execute("SELECT last_error, last_ok_at FROM source WHERE id = 'nyiso_icap'").fetchone()
    assert "FetchError" in src["last_error"] and src["last_ok_at"] is None
    assert committed.execute("SELECT count(*) AS n FROM fact WHERE subject_type = 'series'").fetchone()["n"] == 0


def test_scheduled_monthly_on_the_collect_queue():
    task = app.tasks["collect.nyiso_icap"]
    assert task.queue == "collect"
    crons = {pid: pt.cron for (name, pid), pt in app.periodic_registry.periodic_tasks.items()
             if name == "collect.nyiso_icap"}
    assert crons == {"monthly_2nd_1117_utc": "17 11 2 * *"}
    assert "nyiso_icap" in cli._groups["collect"]
    assert not ny.SOURCE.blocked and "legal-notice" in ny.SOURCE.terms_note


@pytest.mark.network
def test_live_run_twice_writes_nothing_new(committed):
    first = ny.run(first=date(2026, 5, 1))
    assert first["ok"], first
    assert first["documents_new"] >= 10 and first["facts_new"] > 0
    assert "nyiso:nyc" in first["latest"]
    second = ny.run(first=date(2026, 5, 1))
    assert second["ok"], second
    assert second["documents_new"] == 0 and second["facts_new"] == 0
    assert second["skipped_settled"] >= 10
