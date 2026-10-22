"""etf_oas: the iShares page parser on captured (trimmed) product pages, the benchmark refusal, facts and
reruns, stopping at 403/CAPTCHA pages, the once-a-day window, the schedule, the terms note and the licence flag.

Fixtures (data/etf_oas): the USHY, HYBB, USIG and LQDB product pages fetched 2026-10-21 23:2x UTC, trimmed to
the three embedded component blocks the collector reads (OAS as of 2026-10-20). No test fetches ishares.com.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from marden import cli
from marden.collectors import etf_oas as E
from marden.collectors.rates_common import ParseError, RunStats, due_et
from marden.core import db, documents, http, licensing, sources
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "etf_oas"
PAGES = {f.ticker: (DATA / f"{f.ticker.lower()}_2026-10-20.html").read_bytes() for f in E.FUNDS}
LQDB = (DATA / "lqdb_2026-10-20.html").read_bytes()
USHY, HYBB, USIG = E.FUNDS
EVENING = datetime(2026, 10, 21, 23, 0, tzinfo=UTC)  # 19:00 EDT, after the 18:30 ET window opens


def _fetcher(handler: Callable[[httpx.Request], httpx.Response]) -> E.PageFetcher:
    return E.PageFetcher(client=httpx.Client(transport=httpx.MockTransport(handler)), jitter=(0, 0),
                         backoff_base=0.01)


def _serve(pages: dict[str, bytes], calls: list[str] | None = None) -> Callable[[httpx.Request], httpx.Response]:
    by_url = {f.url: pages[f.ticker] for f in E.FUNDS if f.ticker in pages}

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        body = by_url.get(str(request.url))
        if body is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, content=body, headers={"content-type": "text/html;charset=UTF-8"})

    return handler


@pytest.fixture
def fast_host() -> Iterator[None]:
    http.set_rate_limit(E.HOST, 1000.0)
    yield
    http.set_rate_limit(E.HOST, 1.0)


def test_parses_the_captured_fund_pages():
    ushy = E.parse_page(PAGES["USHY"], USHY)
    assert (ushy.benchmark, ushy.index_ticker, ushy.as_of) == (
        "ICE BofA US High Yield Constrained Index", "HUC0", date(2026, 10, 20))
    assert ushy.oas_bps == Decimal("307.6988") and ushy.oas_pct == Decimal("3.076988")
    assert ushy.effective_duration_years == Decimal("3.17863")
    assert ushy.avg_yield_to_maturity_pct == Decimal("8.21") and ushy.yield_to_worst_pct is None  # not on the page
    hybb, usig = E.parse_page(PAGES["HYBB"], HYBB), E.parse_page(PAGES["USIG"], USIG)
    assert (hybb.benchmark, hybb.oas_bps) == ("ICE BofA BB US High Yield Constrained Index", Decimal("200.66185"))
    assert (usig.benchmark, usig.index_ticker, usig.oas_bps) == ("ICE BofA US Corporate Index", "C0A0",
                                                                 Decimal("84.59256"))


def test_refuses_a_page_whose_benchmark_or_fund_is_not_the_expected_one():
    # LQDB was the candidate BBB proxy: its page names an iBoxx index, so it is not used (and never written).
    lqdb = E.Fund("LQDB", "318688", "ishares-bbb-rated-corporate-bond-etf", "ICE BofA BBB US Corporate Index",
                  "ice_bofa_us_bbb_oas", "ICE BofA BBB US Corporate Index (FRED BAMLC0A4CBBB)")
    with pytest.raises(E.BenchmarkMismatch, match="iBoxx USD Liquid Investment Grade BBB 0\\+ Index"):
        E.parse_page(LQDB, lqdb)
    with pytest.raises(E.BenchmarkMismatch, match="the page is for 'USHY'"):
        E.parse_page(PAGES["USHY"], HYBB)
    renamed = PAGES["USHY"].replace(b"ICE BofA US High Yield Constrained Index", b"ICE BofA US High Yield Index")
    with pytest.raises(E.BenchmarkMismatch, match="expected 'ICE BofA US High Yield Constrained Index'"):
        E.parse_page(renamed, USHY)
    assert issubclass(E.BenchmarkMismatch, ParseError)


def test_changed_pages_are_parse_errors():
    with pytest.raises(ParseError, match="no embedded component data"):
        E.parse_page(b"<html><body>Site Maintenance</body></html>", USHY)
    in_pct = PAGES["USHY"].replace(b"307.70 bps", b"3.08%")
    with pytest.raises(ParseError, match="not 'Option Adjusted Spread' in bps"):
        E.parse_page(in_pct, USHY)
    no_date = PAGES["USHY"].replace(b"20261020", b"null")
    with pytest.raises(ParseError, match="yyyymmdd"):
        E.parse_page(no_date, USHY)


def test_terms_robots_and_licence_flag():
    assert not E.SOURCE.blocked and E.SOURCE.kind == "portal"
    note = E.SOURCE.terms_note
    assert licensing.BLACKROCK_TERMS_URL in note and "solely for your personal, non-commercial use" in note
    assert "Use any robot, spider, intelligent agent, other automatic device" in note
    assert "robots.txt" in note and "/us/MYCATEGORYURL" in note and "business owner" in note
    assert {f.source_name for f in E.FUNDS} == {licensing.ishares_oas_name(t) for t in licensing.ISHARES_OAS_TICKERS}
    assert all(licensing.is_unlicensed(f.source_name) for f in E.FUNDS)
    assert USHY.source_name == "iShares USHY fund OAS (BlackRock)"
    assert set(licensing.unlicensed_source_names()) >= {f.source_name for f in E.FUNDS}
    assert [f.proxy_for for f in E.FUNDS] == ["ice_bofa_us_hy_oas", "ice_bofa_us_bb_oas", "ice_bofa_us_ig_oas"]
    assert USHY.url == "https://www.ishares.com/us/products/291299/ishares-broad-usd-high-yield-corporate-bond-etf"
    assert USHY.method.startswith("fund OAS of USHY, which tracks the ICE BofA US High Yield Constrained Index")


def _stored(fund: E.Fund, data: bytes) -> documents.DocumentRow:
    sources.upsert_spec(E.SOURCE)
    return documents.record_document(E.SOURCE_ID, fund.url, data, content_type="text/html", doc_type=E.DOC_TYPE)


def test_writes_the_series_facts_once(committed):
    doc = _stored(USHY, PAGES["USHY"])
    page = E.parse_page(doc.read(), USHY)
    news = []
    for _ in range(2):
        stats = RunStats()
        with db.transaction() as conn:
            E.write_page(conn, USHY, page, doc, stats)
        news.append(stats.facts_new)
    assert news == [8, 0]  # value, duration, avg YTM, benchmark, index ticker, proxy_for, proxy_method, display
    cur = {r["field"]: r for r in committed.execute(
        "SELECT * FROM fact_current WHERE subject_type = 'series' AND subject_id = 'etf_oas_ushy'").fetchall()}
    value = cur["value"]
    assert value["value"] == pytest.approx(3.076988) and value["unit"] == "pct" and value["tag"] == "R"
    assert value["valid_from"] == date(2026, 10, 20) and value["document_id"] == doc.id
    assert value["source_name"] == "iShares USHY fund OAS (BlackRock)" and value["source_url"] == USHY.url
    assert cur["effective_duration_years"]["unit"] == "years"
    assert cur["benchmark_index"]["value"] == "ICE BofA US High Yield Constrained Index"
    assert cur["proxy_for"]["value"] == "ice_bofa_us_hy_oas" and cur["display_allowed"]["value"] is False
    assert "yield_to_worst_pct" not in cur  # the page does not publish it; nothing is filled in


def test_run_fetches_raw_first_writes_and_reruns_idempotently(committed, fast_host):
    calls: list[str] = []
    first = E.run(force=True, now=EVENING, fetcher=_fetcher(_serve(PAGES, calls)))
    assert first["ok"], first
    assert first["documents_new"] == 3 and first["facts_new"] == 24 and first["requests"] == 3
    assert first["funds"]["USHY"]["oas_bps"] == pytest.approx(307.6988) and first["scope"] == "all"
    assert calls == [f.url for f in E.FUNDS]
    docs = committed.execute("SELECT doc_type, count(*) n FROM document WHERE source_id = 'etf_oas' GROUP BY 1")
    assert {r["doc_type"]: r["n"] for r in docs} == {E.DOC_TYPE: 3}
    again = E.run(force=True, now=EVENING, fetcher=_fetcher(_serve(PAGES)))
    assert again["ok"] and again["documents_new"] == 0 and again["facts_new"] == 0
    # The once-a-day limit: after a full ok run this evening an unforced run makes no request.
    calls.clear()
    skipped = E.run(now=EVENING, fetcher=_fetcher(_serve(PAGES, calls)))
    assert skipped["skipped"].startswith("already collected") and calls == []


def test_a_benchmark_mismatch_writes_nothing_for_that_fund_and_fails_the_run(committed, fast_host):
    pages = dict(PAGES, USHY=PAGES["USHY"].replace(b"ICE BofA US High Yield Constrained Index", b"Other Index"))
    out = E.run(force=True, now=EVENING, fetcher=_fetcher(_serve(pages)))
    assert not out["ok"] and "BenchmarkMismatch" in out["error"] and "Other Index" in out["error"]
    assert set(out["funds"]) == {"HYBB", "USIG"} and "USHY" in out["refused"]
    n = committed.execute("SELECT count(*) n FROM fact WHERE subject_id = 'etf_oas_ushy'").fetchone()["n"]
    assert n == 0
    src = committed.execute("SELECT last_error FROM source WHERE id = 'etf_oas'").fetchone()
    assert "benchmark is 'Other Index'" in src["last_error"]


@pytest.mark.parametrize("response", [
    httpx.Response(403, text="Forbidden"),
    httpx.Response(200, text="<html><title>Attention Required!</title><div>captcha</div></html>",
                   headers={"content-type": "text/html"}),
])
def test_stops_at_403_or_a_captcha_page_without_evasion(committed, fast_host, response):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["user-agent"])
        return response

    out = E.run(force=True, now=EVENING, fetcher=_fetcher(handler))
    assert not out["ok"] and "Challenged" in out["error"] and "no evasion" in out["error"]
    assert calls == ["Marden research cody@marden.ai"]  # one request, honest identity, then stop
    assert committed.execute("SELECT count(*) n FROM fact WHERE subject_type = 'series'").fetchone()["n"] == 0


def test_window_schedule_and_cli():
    afternoon = datetime(2026, 10, 21, 21, 0, tzinfo=UTC)  # 17:00 EDT: before the window
    assert E.run(now=afternoon)["skipped"].startswith("before 18:30 ET")
    assert app.tasks["collect.etf_oas"].queue == "collect"
    crons = {pid: pt.cron for (name, pid), pt in app.periodic_registry.periodic_tasks.items()
             if name == "collect.etf_oas"}
    assert crons == {"weekdays_1830_et": "30 22,23 * * 1-5"}
    for day in (date(2026, 7, 1), date(2026, 12, 1)):
        slots = [int(datetime(day.year, day.month, day.day, h, 30, tzinfo=UTC).timestamp()) for h in (22, 23)]
        assert [due_et(ts, 18, 30) for ts in slots].count(True) == 1
    assert E.collect_task(timestamp=int(datetime(2026, 7, 1, 23, 30, tzinfo=UTC).timestamp())) == {
        "skipped": "not 18:30 ET"}
    assert "etf_oas" in cli._groups["collect"]
