"""yahoo_equity: the chart parser on real Yahoo responses, facts and reruns, stopping at 429/challenges,
the schedule, the terms note, and one live run."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from marden import cli
from marden.collectors import yahoo_equity as Y
from marden.collectors.market_http import MarketFetcher
from marden.collectors.rates_common import ParseError, RunStats, due_et
from marden.core import db, documents, licensing, sources
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "yahoo_equity"
ORCL = (DATA / "chart_ORCL_2mo.json").read_bytes()  # saved from the chart API by the legacy research
CRWV_OPEN = (DATA / "chart_CRWV_5d_session_open.json").read_bytes()  # fetched 2026-10-21 13:38 UTC
TOO_MANY = (DATA / "chart_429_too_many_requests.txt").read_bytes()  # Yahoo's 429 body, 2026-10-21


def _fetcher(handler) -> MarketFetcher:
    return MarketFetcher(client=httpx.Client(transport=httpx.MockTransport(handler)), jitter=(0, 0),
                         backoff_base=0.01)


def test_parses_daily_bars():
    chart = Y.parse_chart(ORCL, symbol="ORCL")
    assert (chart.symbol, chart.currency, len(chart.bars), chart.unfinished) == ("ORCL", "USD", 44, [])
    first, last = chart.bars[0], chart.bars[-1]
    assert first.day == date(2026, 8, 17) and last.day == date(2026, 10, 16)
    assert last.values == {"open": Decimal("133.49"), "high": Decimal("133.8"), "low": Decimal("127.36"),
                           "close": Decimal("128.23"), "adj_close": Decimal("128.23"), "volume": 35996500}
    assert first.values["adj_close"] == Decimal("146.1096")  # dividend-adjusted, float32 noise rounded off


def test_a_session_that_has_not_closed_is_not_written():
    during = datetime(2026, 10, 21, 13, 38, tzinfo=UTC)
    chart = Y.parse_chart(CRWV_OPEN, symbol="CRWV", fetched_at=during)
    assert chart.unfinished == [date(2026, 10, 21)]
    assert [b.day for b in chart.bars] == [date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 19),
                                           date(2026, 10, 20)]
    after = datetime(2026, 10, 21, 20, 15, tzinfo=UTC)  # 16:15 EDT: the session is final
    assert Y.parse_chart(CRWV_OPEN, symbol="CRWV", fetched_at=after).bars[-1].day == date(2026, 10, 21)


def test_changed_or_wrong_responses_are_refused():
    with pytest.raises(ParseError, match="not JSON"):
        Y.parse_chart(TOO_MANY)
    with pytest.raises(ParseError, match="asked for MSFT"):
        Y.parse_chart(ORCL, symbol="MSFT")
    with pytest.raises(Y.SymbolNotFound):
        Y.parse_chart(b'{"chart":{"result":null,"error":{"code":"Not Found","description":"delisted"}}}', symbol="X")
    short = ORCL.replace(b'"volume":[', b'"volume":[1,', 1)
    with pytest.raises(ParseError, match="line up"):
        Y.parse_chart(short, symbol="ORCL")


def _stored(data: bytes, symbol: str = "ORCL") -> documents.DocumentRow:
    sources.upsert_spec(Y.SOURCE)
    return documents.record_document(Y.SOURCE_ID, Y.chart_url(symbol, "2y"), data, content_type="application/json")


def test_writes_series_facts_once(committed):
    doc = _stored(ORCL)
    chart = Y.parse_chart(doc.read(), symbol="ORCL")
    for expected in (44 * 6, 0):
        stats = RunStats()
        with db.transaction() as conn:
            assert Y.write_chart(conn, "ORCL", chart, producer=Y.PRODUCER, document_id=doc.id, stats=stats) == expected
    cur = {r["field"]: r for r in committed.execute(
        "SELECT * FROM fact_current WHERE subject_type = 'series' AND subject_id = 'equity:ORCL'").fetchall()}
    assert set(cur) == {"open", "high", "low", "close", "adj_close", "volume"}
    close = cur["close"]
    assert (close["value"], close["valid_from"], close["unit"]) == (128.23, date(2026, 10, 16), "USD")
    assert (close["tag"], close["source_name"], close["document_id"]) == ("F", "Yahoo Finance", doc.id)
    assert close["source_url"] == "https://finance.yahoo.com/quote/ORCL/history/"
    assert (cur["volume"]["value"], cur["volume"]["unit"]) == (35996500, "shares")
    assert licensing.is_unlicensed(close["source_name"])


def test_run_backfills_then_reads_the_last_month(committed):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.headers["user-agent"] == "Marden research cody@marden.ai"
        return httpx.Response(200, content=ORCL, headers={"content-type": "application/json;charset=utf-8"})

    first = Y.run(tickers=["orcl"], today=date(2026, 10, 21), fetcher=_fetcher(handler))
    assert first["ok"], first
    assert (first["facts_new"], first["documents_new"], first["tickers"]["ORCL"]["range"]) == (264, 1, "2y")
    second = Y.run(tickers=["ORCL"], today=date(2026, 10, 21), fetcher=_fetcher(handler))
    assert second["ok"] and second["facts_new"] == 0
    assert second["tickers"]["ORCL"]["range"] == "1mo" and second["documents_new"] == 1  # a new URL
    assert "range=2y" in calls[0] and "range=1mo" in calls[1] and len(calls) == 2
    runs = committed.execute(
        "SELECT ok, items_new FROM source_run WHERE source_id = 'yahoo_equity' ORDER BY id").fetchall()
    assert [(r["ok"], r["items_new"]) for r in runs] == [(True, 264), (True, 0)]


def test_a_429_is_recorded_and_the_run_stops(committed):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(429, content=TOO_MANY, headers={"content-type": "text/html"})

    out = Y.run(tickers=["AMD", "CRWV", "ORCL"], today=date(2026, 10, 21), fetcher=_fetcher(handler))
    assert out["ok"] is False and out["error"].startswith("Challenged: stopped: HTTP 429")
    assert calls == ["/v8/finance/chart/AMD"] * 2  # one polite retry, then stop: no other ticker is tried
    src = committed.execute("SELECT last_error, blocked FROM source WHERE id = 'yahoo_equity'").fetchone()
    assert "429" in src["last_error"] and src["blocked"] is False
    assert committed.execute("SELECT count(*) AS n FROM fact").fetchone()["n"] == 0


def test_a_cloudflare_challenge_is_recorded_and_the_run_stops(committed):
    challenge = (Path(__file__).parent / "data" / "market_http" / "cloudflare_challenge.html").read_bytes()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        headers = {"cf-mitigated": "challenge", "content-type": "text/html"}
        return httpx.Response(403, content=challenge, headers=headers)

    out = Y.run(tickers=["AMD", "CRWV"], today=date(2026, 10, 21), fetcher=_fetcher(handler))
    assert out["ok"] is False and out["error"].startswith("Challenged: stopped: HTTP 403")
    assert calls == ["/v8/finance/chart/AMD"]  # no retry, no other ticker, no other identity
    src = committed.execute("SELECT last_error, blocked FROM source WHERE id = 'yahoo_equity'").fetchone()
    assert "403" in src["last_error"] and src["blocked"] is False
    run = committed.execute("SELECT ok FROM source_run WHERE source_id = 'yahoo_equity'").fetchone()
    assert run["ok"] is False and committed.execute("SELECT count(*) AS n FROM fact").fetchone()["n"] == 0


def test_an_unknown_ticker_is_skipped_not_fatal(committed):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ORCL"):
            return httpx.Response(200, content=ORCL, headers={"content-type": "application/json"})
        return httpx.Response(404, content=b'{"chart":{"result":null,"error":{"code":"Not Found"}}}')

    out = Y.run(tickers=["ORCL", "ZZZZ"], today=date(2026, 10, 21), fetcher=_fetcher(handler))
    assert out["ok"] and out["not_found"] == ["ZZZZ"] and out["with_data"] == 1


def test_terms_note_quotes_the_terms_and_robots():
    terms = re.sub(r"\s+", " ", (DATA / "terms_excerpt.txt").read_text())
    quoted = [q for q in Y.TERMS_NOTE.split('"')[1::2] if len(q) >= 40]
    assert len(quoted) == 3
    for q in quoted:
        assert all(part in terms for part in q.split(" ... ")), q
    assert "Last updated: 4 August 2026" in terms
    assert (DATA / "robots_query1.txt").read_text().split() == ["User-agent:", "*", "Disallow:", "/"]
    assert Y.SOURCE.blocked is False and "UNLICENSED" in Y.SOURCE.terms_note
    assert Y.SOURCE_NAME == licensing.YAHOO_FINANCE == "Yahoo Finance"


def test_scheduled_on_weekdays_after_the_us_close():
    assert app.tasks["collect.yahoo_equity"].queue == "collect"
    crons = {pid: pt.cron for (name, pid), pt in app.periodic_registry.periodic_tasks.items()
             if name == "collect.yahoo_equity"}
    assert crons == {"weekdays_1820_et": "20 22,23 * * 1-5"}
    for day in (date(2026, 7, 1), date(2026, 12, 1)):
        slots = [int(datetime(day.year, day.month, day.day, h, 20, tzinfo=UTC).timestamp()) for h in (22, 23)]
        assert [due_et(ts, 18, 20) for ts in slots].count(True) == 1
    assert Y.collect_task(timestamp=int(datetime(2026, 12, 1, 22, 20, tzinfo=UTC).timestamp())) == {
        "skipped": "not 18:20 ET"}
    assert "yahoo_equity" in cli._groups["collect"]


@pytest.mark.network
def test_live_run_twice_writes_nothing_new(committed):
    first = Y.run(tickers=["MSFT"])
    if not first["ok"] and first["error"].startswith("Challenged"):
        pytest.skip(f"Yahoo refused the request; recorded on the source: {first['error']}")
    assert first["ok"], first
    assert first["tickers"]["MSFT"]["bars"] > 400 and first["facts_new"] > 2000
    second = Y.run(tickers=["MSFT"])
    if not second["ok"] and second["error"].startswith("Challenged"):
        pytest.skip(f"Yahoo refused the second request: {second['error']}")
    assert second["ok"], second
    assert second["tickers"]["MSFT"]["range"] == "1mo" and second["facts_new"] == 0
