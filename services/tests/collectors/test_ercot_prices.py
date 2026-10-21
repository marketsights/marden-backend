"""ercot_prices: parsers on captured ERCOT Public API answers (NP4-190-CD DAM around both 2024/2025 clock changes,
NP6-905-CD real time on 2024-11-17 with intervals above 1,000 USD/MWh, a two-page answer), request windows, the
API flow (sign-in, one request per window, raw documents, facts, rerun writes nothing, settled windows reread
from storage), the key wall, the schedule and CLI, the terms and product findings, and a live run (network).

Captured with the collector's own queries on 2026-10-21 (gzip). The 2024-11-03 two-page answer was asked with
size=50 so that it pages; the collector asks for PAGE_SIZE rows.
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from marden import cli
from marden.collectors import ercot_api, ercot_load_zones
from marden.collectors import ercot_prices as prices
from marden.collectors import ercot_prices_parse as parse
from marden.collectors.rates_common import ParseError, RunStats, due_et
from marden.core import config, documents, http, licensing, sources
from marden.core.http import Fetcher
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "ercot_prices"
GIS_DATA = Path(__file__).parent / "data" / "ercot_gis"


def fixture(name: str) -> bytes:
    return gzip.decompress((DATA / f"{name}.json.gz").read_bytes())


def test_dam_fall_back_and_spring_forward_days():
    page = parse.dam_page(fixture("dam_hb_north_2024-11-02_2024-11-04"))
    assert (page.total_records, page.total_pages, len(page.rows)) == (73, 1, 73)
    days, incomplete = parse.daily(page.rows, per_hour=1)
    assert incomplete == [] and [d.day for d in days] == [date(2024, 11, 2), date(2024, 11, 3), date(2024, 11, 4)]
    fall = days[1]
    assert (fall.point, fall.periods, fall.avg, fall.on_peak_avg, fall.max) == (
        "HB_NORTH", 25, Decimal("16.5004"), Decimal("20.0744"), Decimal("46.18"))
    assert fall.hourly[:4] == [[1, Decimal("10.87")], [2, Decimal("10.49")], [2, Decimal("13.6")], [3, Decimal("6.76")]]
    assert fall.intervals_over_1000 is None
    spring = parse.daily(parse.dam_page(fixture("dam_lz_west_2025-03-08_2025-03-10")).rows, per_hour=1)[0]
    assert [(d.day.day, d.periods) for d in spring] == [(8, 24), (9, 23), (10, 24)]  # March 2025
    assert [h for h, _ in spring[1].hourly][:3] == [1, 2, 4]  # ERCOT skips hour ending 03 when clocks go forward
    assert spring[1].avg == Decimal("54.0765")
    assert (parse.hours_in_day(date(2025, 3, 9)), parse.hours_in_day(date(2024, 11, 3)),
            parse.hours_in_day(date(2026, 7, 1))) == (23, 25, 24)


def test_real_time_day_with_price_spikes():
    page = parse.rt_page(fixture("rt_lz_2024-11-17"), frozenset({"LZ"}))
    days, incomplete = parse.daily(page.rows, per_hour=4)
    assert incomplete == [] and len(days) == 8
    by = {d.point: d for d in days}
    aen = by["LZ_AEN"]
    assert (aen.periods, aen.intervals_over_1000, aen.max, aen.avg, aen.on_peak_avg) == (
        96, 7, Decimal("4065.62"), Decimal("182.7233"), Decimal("265.6538"))
    assert (by["LZ_SOUTH"].intervals_over_1000, by["LZ_HOUSTON"].intervals_over_1000) == (1, 3)
    assert aen.hourly is None
    assert parse.rt_page(fixture("rt_lz_2024-11-17"), frozenset({"HU"})).rows == []


def test_two_pages_make_one_fall_back_day():
    first = parse.rt_page(fixture("rt_ah_2024-11-03_page1"))
    second = parse.rt_page(fixture("rt_ah_2024-11-03_page2"))
    assert (first.total_pages, first.current_page, second.current_page) == (2, 1, 2)
    assert parse.daily(first.rows, per_hour=4)[1] == [{"point": "HB_HUBAVG", "day": "2024-11-03", "periods": 50,
                                                      "expected": 100, "reason": "periods missing or extra"}]
    days, incomplete = parse.daily(first.rows + second.rows, per_hour=4)
    assert incomplete == [] and (days[0].point, days[0].periods) == ("HB_HUBAVG", 100)


def test_refuses_other_shapes():
    with pytest.raises(ParseError, match="expected an ERCOT NP4-190-CD answer"):
        parse.dam_page(fixture("rt_ah_2024-11-03_page1"))
    with pytest.raises(ParseError, match="no _meta"):
        parse.rt_page(b"<html>Service unavailable</html>")
    body = json.loads(fixture("dam_hb_north_2024-11-02_2024-11-04"))
    body["data"][0][3] = 99999.0
    with pytest.raises(ParseError, match="outside"):
        parse.dam_page(json.dumps(body).encode())
    body = json.loads(fixture("dam_hb_north_2024-11-02_2024-11-04"))
    body["data"].append([*body["data"][0][:3], 1.0, body["data"][0][4]])  # a second price for one hour
    days, incomplete = parse.daily(parse.dam_page(json.dumps(body).encode()).rows, per_hour=1)
    assert [i["reason"] for i in incomplete] == ["two prices for one period"] and len(days) == 2


def test_windows_series_ids_and_streams():
    days = [date(2024, 10, 30), date(2024, 10, 31), date(2024, 11, 1), date(2024, 11, 2), date(2024, 11, 5)]
    assert list(prices.windows(days)) == [(days[0], days[1]), (days[2], days[3]), (days[4], days[4])]
    assert prices.series_id("LZ_WEST", "rt") == "power_price:ercot:lz_west:rt"
    assert len(prices.STREAMS) == 19 and {p for s in prices.STREAMS if s.market == "rt" for p in s.points} == set(
        prices.LOAD_ZONES + prices.HUBS)
    dam = prices.Stream("dam", "HB_NORTH", ("HB_NORTH",))
    assert dam.query(date(2024, 11, 2), date(2024, 11, 4), 1) == (
        f"{prices.DAM_URL}?deliveryDateFrom=2024-11-02&deliveryDateTo=2024-11-04&settlementPoint=HB_NORTH&size=50000")
    assert dam.query(date(2024, 11, 2), date(2024, 11, 4), 2).endswith("&page=2")


def test_products_terms_and_licence():
    listing = json.loads((DATA / "api_products_excerpt.json").read_text())
    ends = {p["emilId"]: p["artifacts"][0]["_links"]["endpoint"]["href"] for p in listing["products"]}
    assert ends == {"NP4-190-CD": prices.DAM_URL, "NP6-905-CD": prices.RT_URL}
    assert "NP4-160-SG" not in listing["emilIds"] and listing["np4-160-sg"]["status"] == 404
    api_terms = " ".join((GIS_DATA / "api_terms_excerpt.txt").read_text().split())
    for clause in ('Accounts registered by "bots" or other automated methods are not permitted',
                   "You may not download the same report from the Data Access Portal more than three times within "
                   "a 12-month period."):
        assert clause in api_terms and clause in prices.TERMS_NOTE
    reuse = ("raw data provided in public portions of this website may be used, reproduced, and redistributed in "
             "compilations, charts, and analyses without maintaining such notices.")
    assert reuse in " ".join((GIS_DATA / "terms_of_use_excerpt.txt").read_text().split()) and reuse in prices.TERMS_NOTE
    assert licensing.licence_for(prices.SOURCE_NAME)["status"] == "public" and not prices.SOURCE.blocked


EMPTY = {"NP4-190-CD": ["deliveryDate", "hourEnding", "settlementPoint", "settlementPointPrice", "DSTFlag"],
         "NP6-905-CD": ["deliveryDate", "deliveryHour", "deliveryInterval", "settlementPoint", "settlementPointType",
                        "settlementPointPrice", "DSTFlag"]}


def _empty(emil: str) -> dict:
    return {"_meta": {"totalRecords": 0, "pageSize": 50000, "totalPages": 0, "currentPage": 0},
            "report": {"reportEMIL": emil}, "fields": [{"name": n} for n in EMPTY[emil]], "data": []}


@pytest.fixture(autouse=True)
def _fast_mock_api(request):
    """Mocked answers need not wait for ERCOT's 0.4 requests a second; the live test keeps the real limit."""
    if request.node.get_closest_marker("network"):
        yield
        return
    http.set_rate_limit(ercot_api.API_HOST, 1000.0)
    http.set_rate_limit(ercot_api.TOKEN_HOST, 1000.0)
    yield
    http.set_rate_limit(ercot_api.API_HOST, 0.4)
    http.set_rate_limit(ercot_api.TOKEN_HOST, 0.2)


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("ERCOT_API_KEY", "sub-key-test")
    monkeypatch.setenv("ERCOT_API_USERNAME", "person@example.com")
    monkeypatch.setenv("ERCOT_API_PASSWORD", "pw-test")
    config.reload_settings()
    yield
    monkeypatch.undo()
    config.reload_settings()


class Api:
    """The API as captured: HB_NORTH DAM and HB_HUBAVG (type AH) real time; every other query answers empty."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == ercot_api.TOKEN_HOST:
            assert b"password=pw-test" in request.content
            return httpx.Response(200, json={"id_token": "tok"})
        assert request.headers["Ocp-Apim-Subscription-Key"] == "sub-key-test"
        assert request.headers["Authorization"] == "Bearer tok"
        q = {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}
        self.calls.append(f"{request.url.path.rsplit('/', 1)[1]} {q}")
        if request.url.path.endswith("dam_stlmnt_pnt_prices"):
            if q["settlementPoint"] == "HB_NORTH":
                return httpx.Response(200, content=fixture("dam_hb_north_2024-11-02_2024-11-04"))
            return httpx.Response(200, json=_empty("NP4-190-CD"))
        if q["settlementPointType"] == "AH":
            return httpx.Response(200, content=fixture(f"rt_ah_2024-11-03_page{q.get('page', '1')}"))
        return httpx.Response(200, json=_empty("NP6-905-CD"))


@pytest.fixture
def no_zones(monkeypatch):
    monkeypatch.setattr(ercot_load_zones, "assign", lambda stats, source_id, fetcher: None)


def test_api_flow_writes_complete_days_once(committed, keyed, no_zones, monkeypatch):
    monkeypatch.setattr(prices, "BACKFILL_DAYS", 2)
    api = Api()
    fetcher = Fetcher(client=httpx.Client(transport=httpx.MockTransport(api)))
    first = prices.run(fetcher=fetcher, today=date(2024, 11, 4))
    assert first["ok"], first
    info = first["ercot_prices"]
    assert info["requests"] == len(api.calls) == 15 + 4 + 1  # one per DAM point and RT type, AH has two pages
    assert info["days_written"] == {"dam": 3, "rt": 1} and first["facts_new"] == 3 * 4 + 4
    assert first["latest"]["power_price:ercot:hb_north:dam"][0] == "2024-11-04"
    rows = {(r["subject_id"], r["field"], r["valid_from"]): r for r in committed.execute(
        "SELECT * FROM fact WHERE subject_type = 'series'").fetchall()}
    fall = rows[("power_price:ercot:hb_north:dam", "avg", date(2024, 11, 3))]
    assert (Decimal(str(fall["value"])), fall["unit"], fall["tag"], fall["source_name"], fall["source_url"]) == (
        Decimal("16.5004"), "USD/MWh", "F", "ERCOT", prices.DAM_PRODUCT_URL)
    assert len(rows[("power_price:ercot:hb_north:dam", "hourly", date(2024, 11, 3))]["value"]) == 25
    spikes = rows[("power_price:ercot:hb_hubavg:rt", "intervals_over_1000", date(2024, 11, 3))]
    assert (spikes["value"], spikes["unit"], spikes["source_url"]) == (0, "intervals", prices.RT_PRODUCT_URL)
    doc = documents.get_document(fall["document_id"])
    assert doc is not None and doc.url.startswith(prices.DAM_URL) and "settlementPoint=HB_NORTH" in doc.url
    assert "sub-key-test" not in json.dumps(doc.meta) and "tok" not in doc.url
    second = prices.run(fetcher=fetcher, today=date(2024, 11, 4))
    assert second["ok"] and second["facts_new"] == 0
    asked = api.calls[len(api.calls) - second["ercot_prices"]["requests"]:]
    assert all("HB_NORTH" not in c or "'deliveryDateFrom': '2024-11-05'" in c for c in asked)  # only missing days


def test_settled_window_is_read_from_storage(committed, keyed):
    sources.upsert_spec(prices.SOURCE)
    stream = prices.Stream("dam", "HB_NORTH", ("HB_NORTH",))
    url = stream.query(date(2024, 11, 2), date(2024, 11, 4), 1)
    documents.record_document(prices.SOURCE_ID, url, fixture("dam_hb_north_2024-11-02_2024-11-04"),
                              content_type="application/json")

    def never(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a settled window is never asked for again: {request.url}")

    run = prices.Run(RunStats(), Fetcher(client=httpx.Client(transport=httpx.MockTransport(never))), date(2026, 1, 1))
    assert run.load_window(stream, date(2024, 11, 2), date(2024, 11, 4))
    assert run.info["stored_reused"] == 1 and run.info["requests"] == 0 and run.info["days_written"] == {"dam": 3}


def test_without_the_key_records_needs_key(committed, no_zones, monkeypatch):
    for name in ("ERCOT_API_KEY", "ERCOT_API_USERNAME", "ERCOT_API_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    config.reload_settings()

    def never(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no request may be made without the key: {request.url}")

    out = prices.run(fetcher=Fetcher(client=httpx.Client(transport=httpx.MockTransport(never))))
    assert not out["ok"] and "needs key" in out["error"] and "prices" in out["errors"]
    run = committed.execute("SELECT ok, error FROM source_run WHERE source_id = 'ercot_prices'").fetchone()
    assert run["ok"] is False and "ERCOT_API_KEY" in run["error"]
    assert committed.execute("SELECT count(*) AS n FROM fact").fetchone()["n"] == 0


def test_a_failed_zone_step_does_not_stop_prices(committed, keyed, monkeypatch):
    monkeypatch.setattr(prices, "BACKFILL_DAYS", 2)

    def broken(stats, source_id, fetcher):
        raise ParseError("ERCOT changed its Load Zone Map")

    monkeypatch.setattr(ercot_load_zones, "assign", broken)
    out = prices.run(fetcher=Fetcher(client=httpx.Client(transport=httpx.MockTransport(Api()))),
                     today=date(2024, 11, 4))
    assert not out["ok"] and "Load Zone Map" in out["error"] and out["facts_new"] == 16


def test_schedule_and_cli():
    task = app.tasks["collect.ercot_prices"]
    assert task.queue == "collect"
    crons = {pid: t.cron for (name, pid), t in app.periodic_registry.periodic_tasks.items()
             if name == "collect.ercot_prices"}
    assert crons == {"daily_dam_1407_ct": prices.CRON_DAM_UTC, "daily_rt_0047_ct": prices.CRON_RT_UTC}
    summer_dam = int(datetime(2026, 7, 1, 19, 7, tzinfo=UTC).timestamp())  # 14:07 CDT
    winter_dam = int(datetime(2026, 1, 6, 20, 7, tzinfo=UTC).timestamp())  # 14:07 CST
    winter_rt = int(datetime(2026, 1, 6, 6, 47, tzinfo=UTC).timestamp())  # 00:47 CST
    assert due_et(summer_dam, *prices.DAM_RUN_ET) and due_et(winter_dam, *prices.DAM_RUN_ET)
    assert due_et(winter_rt, *prices.RT_RUN_ET) and not due_et(winter_rt + 3600, *prices.RT_RUN_ET)
    off_hour = int(datetime(2026, 7, 1, 20, 7, tzinfo=UTC).timestamp())  # 15:07 CDT: the other UTC slot
    assert prices.collect_task(off_hour) == {"skipped": "not 14:07 or 00:47 Central"}
    assert cli.build_parser().parse_args(["collect", "ercot_prices"])._handler is prices.collect_cli


@pytest.mark.network
def test_live_prices_twice(committed, monkeypatch):
    """The live API with the configured key: the last two days for every point, then a rerun that writes
    nothing; without a key the run records 'needs key'."""
    monkeypatch.setattr(prices, "BACKFILL_DAYS", 2)
    if not config.get_settings().has_ercot_api():
        assert "needs key" in prices.run()["error"]
        return
    first = prices.run()
    assert first["ercot_prices"]["days_written"].get("rt", 0) >= 15 and first["facts_new"] > 0, first
    assert first["ok"], first
    second = prices.run()
    assert second["ok"] and second["facts_new"] == 0, second
