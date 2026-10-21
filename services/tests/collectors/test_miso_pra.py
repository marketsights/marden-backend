"""miso_pra: the deck parser on real (trimmed) PRA result postings, facts, events and reruns, schedule, live run."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pymupdf
import pytest

from marden import cli
from marden.collectors import capacity_zones
from marden.collectors import miso_pra as miso
from marden.collectors.rates_common import ParseError, RunStats
from marden.core import db, documents, sources
from marden.core.facts import FactWriter
from marden.core.http import Fetcher
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "miso_pra"
URL_2025 = miso.POSTINGS[1].url


def _stored(name: str, url: str) -> documents.DocumentRow:
    sources.upsert_spec(miso.SOURCE)
    return documents.record_document(miso.SOURCE_ID, url, (DATA / name).read_bytes(),
                                     content_type="application/pdf", doc_type="miso_pra_results")


def _site(committed, slug: str, power_iso: str) -> None:
    committed.execute("INSERT INTO site (slug, name, state, in_v1) VALUES (%s, %s, 'LA', true)", (slug, slug))
    with db.transaction() as conn:
        FactWriter(conn).add("site", slug, "power_iso", power_iso, tag="R", source_name="S&P Global",
                             source_url="https://example.com/hyperion", valid_from=date(2025, 5, 1),
                             producer="seed:test", status="published")
        capacity_zones.assign(conn, RunStats())


def _fact(committed, sid: str, field: str):
    return committed.execute(
        "SELECT * FROM fact_current WHERE subject_type = 'series' AND subject_id = %s AND field = %s", (sid, field)
    ).fetchone()


def test_parses_the_2025_corrected_posting():
    r = miso.parse_posting((DATA / "pra_2025_zone_tables.pdf").read_bytes())
    assert (r.planning_year, r.published) == (2025, date(2025, 5, 29))  # the corrected repost's date
    assert "slide header" in r.published_loc
    by_season = {t.season: t for t in r.tables}
    assert set(by_season["summer"].acp.values()) == {Decimal("666.50")}
    fall = by_season["fall"].acp  # the ERZ cell "83.24 - 91.60" is not a zone and is ignored
    assert [fall[f"Z{n}"] for n in (1, 7, 8, 9, 10)] == [Decimal("91.60"), Decimal("91.60"), Decimal("74.09"),
                                                          Decimal("74.09"), Decimal("74.10")]
    assert by_season["summer"].committed["Z9"] == Decimal("20221.6")
    assert by_season["spring"].acp["Z5"] == Decimal("69.88")


def test_parses_the_2024_posting_with_the_zone_5_shortfall():
    r = miso.parse_posting((DATA / "pra_2024_zone_tables.pdf").read_bytes())
    assert (r.planning_year, r.published, r.published_loc) == (2024, date(2024, 4, 25), "cover date 'April 25, 2024'")
    by_season = {t.season: t for t in r.tables}
    assert by_season["fall"].acp["Z5"] == Decimal("719.81") and by_season["fall"].acp["Z4"] == Decimal("15.00")
    assert by_season["winter"].acp["Z1"] == Decimal("0.75")
    assert by_season["summer"].committed["Z1"] == Decimal("19091")
    found = miso.auctions(r, source_url="u", document_id=None)
    assert [a.period for a in found] == ["2024-2025-summer", "2024-2025-fall", "2024-2025-winter", "2024-2025-spring"]
    assert found[2].name == "MISO 2024-2025 Planning Resource Auction (Winter 2024/25)"
    assert (found[2].delivery_start, found[2].delivery_end) == (date(2024, 12, 1), date(2025, 2, 28))
    assert [z.zone for z in found[0].zones] == [f"LRZ{n}" for n in range(1, 11)]


def test_season_dates():
    assert miso.season_dates("summer", 2026) == (date(2026, 6, 1), date(2026, 8, 31))
    assert miso.season_dates("fall", 2026) == (date(2026, 9, 1), date(2026, 11, 30))
    assert miso.season_dates("winter", 2027) == (date(2027, 12, 1), date(2028, 2, 29))
    assert miso.season_dates("spring", 2026) == (date(2027, 3, 1), date(2027, 5, 31))


def test_changed_decks_are_refused():
    with pytest.raises(ParseError, match="not a readable PDF"):
        miso.parse_posting(b"<?xml version='1.0'?><Error><Code>AccessDenied</Code></Error>")
    doc = pymupdf.open(stream=(DATA / "pra_2025_zone_tables.pdf").read_bytes(), filetype="pdf")
    doc.select([0, 1, 2, 3, 4])  # spring table dropped
    with pytest.raises(ParseError, match="expected all four"):
        miso.parse_posting(doc.tobytes())


def test_load_writes_facts_once_and_announces_to_linked_v1_sites(committed):
    _site(committed, "hyperion", "MISO (Zone 9)")
    doc = _stored("pra_2025_zone_tables.pdf", URL_2025)
    first = RunStats()
    miso.load_document(doc, first, today=date(2025, 6, 15))
    assert first.facts_new == 4 * 10 * 5  # seasons x zones x (name, start, end, price, MW)
    sid = "capacity:miso:lrz9:2025-2026-summer"
    price = _fact(committed, sid, "clearing_price")
    assert (price["value"], price["unit"], price["tag"], price["source_name"]) == (666.5, "USD/MW-day", "F", "MISO")
    assert (price["valid_from"], price["source_url"], price["document_id"]) == (date(2025, 5, 29), URL_2025, doc.id)
    assert "ACP ($/MW-Day)" in price["quote_loc"]
    assert _fact(committed, "capacity:miso:lrz9:2025-2026-fall", "clearing_price")["value"] == 74.09
    assert _fact(committed, sid, "cleared_mw")["value"] == 20221.6
    assert _fact(committed, "capacity:miso:lrz9:2025-2026-winter", "delivery_end")["value"] == "2026-02-28"
    events = committed.execute("SELECT * FROM event WHERE kind = 'capacity_auction'").fetchall()
    assert [(e["subject_id"], e["event_date"], e["source_url"]) for e in events] == [
        ("hyperion", date(2025, 5, 29), URL_2025)]
    assert events[0]["title"] == ("MISO 2025-2026 Planning Resource Auction: LRZ9 cleared summer $666.50, "
                                  "fall $74.09, winter $33.20, spring $69.88 /MW-day")

    again = RunStats()
    miso.load_document(doc, again, today=date(2025, 6, 15))
    assert again.facts_new == 0 and again.facts_seen == first.facts_seen
    assert committed.execute("SELECT count(*) AS n FROM event").fetchone()["n"] == 1


def test_history_raises_no_events(committed):
    _site(committed, "hyperion", "MISO (Zone 9)")
    miso.load_document(_stored("pra_2024_zone_tables.pdf", miso.POSTINGS[0].url), RunStats(), today=date(2026, 10, 21))
    assert committed.execute("SELECT count(*) AS n FROM event").fetchone()["n"] == 0
    assert _fact(committed, "capacity:miso:lrz5:2024-2025-fall", "clearing_price")["value"] == 719.81


def test_an_unreachable_deck_is_recorded_and_nothing_invented(committed):
    gone = miso.Posting("2026-2027", "http://127.0.0.1:9/2026%20PRA%20Results%20Posting.pdf")
    out = miso.run(postings=(gone,), fact_sheets=(), fetcher=Fetcher(max_retries=0, timeout=2))
    assert out["ok"] is False and out["error"].startswith("FetchError")
    src = committed.execute("SELECT last_error, last_ok_at FROM source WHERE id = 'miso_pra'").fetchone()
    assert "FetchError" in src["last_error"] and src["last_ok_at"] is None
    assert committed.execute("SELECT count(*) AS n FROM fact WHERE subject_type = 'series'").fetchone()["n"] == 0


def test_scheduled_monthly_on_the_collect_queue():
    task = app.tasks["collect.miso_pra"]
    assert task.queue == "collect"
    crons = {pid: pt.cron for (name, pid), pt in app.periodic_registry.periodic_tasks.items()
             if name == "collect.miso_pra"}
    assert crons == {"monthly_3rd_1129_utc": "29 11 3 * *"}
    assert "miso_pra" in cli._groups["collect"]
    assert all(p.url.startswith("https://cdn.misoenergy.org/") for p in miso.POSTINGS + miso.FACT_SHEETS)
    assert "Cloudflare" in miso.SOURCE.terms_note and not miso.SOURCE.blocked


@pytest.mark.network
def test_live_run_twice_writes_nothing_new(committed):
    first = miso.run()
    assert set(first["loaded"]) >= {"2024-2025", "2025-2026"}, first
    assert first["facts_new"] == 2 * 4 * 10 * 5
    assert all("2026-2027" in e for e in first.get("errors", [])), first  # only the 2026 deck may be missing
    sheet = committed.execute(
        "SELECT extract_state FROM document WHERE source_id = 'miso_pra' AND doc_type = 'miso_pra_fact_sheet'"
    ).fetchone()
    assert sheet["extract_state"] == "queued"
    second = miso.run()
    assert second["documents_new"] == 0 and second["facts_new"] == 0
    assert second["facts_seen"] == first["facts_seen"]
