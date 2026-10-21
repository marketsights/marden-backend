"""finra_trace: parsers on real finra.org backend answers, refusal handling, writes and reruns, schedule, live run.

Fixtures (tests/collectors/data/finra_trace/):
- bond_search_coreweave.json: a real security-master answer from services-dynarep.ddwa.finra.org, as the
  legacy refresh stored it on 2026-10-17 (envelope and all);
- eod_46270CAE9.json, eod_00115AAS8_excerpt.json: the real end-of-day records (the envelope's `data` list)
  and security record of two bonds from the legacy pull of 2026-10-20;
- bond_page_68389XBX2.html and template_404.html: what finra.org answered Marden on 2026-10-21 (the bond
  page, then a 404 page for the bond page's own template request), with their status and headers.

The page-flow tests replay these through httpx.MockTransport. finra.org refused the template request, so
no real template answer exists: the replay's template bodies are built in this file (marked TEMPLATE).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from marden import cli
from marden.collectors import finra_trace as ft
from marden.collectors.finra_trace_client import BOND_TEMPLATE, PRICE_YIELD_TEMPLATE, BondPageClient
from marden.collectors.finra_trace_parse import (
    ParseError,
    bond_page_url,
    eastern_to_utc,
    parse_envelope,
    parse_eod,
    parse_security,
    parse_template,
    refusal_reason,
)
from marden.collectors.rates_common import due_et
from marden.core import licensing
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "finra_trace"
UA = "Marden research cody@marden.ai"


def _json(name: str):
    return json.loads((DATA / name).read_text())


def _meta(name: str) -> dict:
    return _json(name)["headers"]


def _template(group: str, name: str, fields: list[str]) -> bytes:  # TEMPLATE: shape the bond page reads
    return json.dumps({"returnBody": {"layout": {"layoutMetadata": {"datasetGroup": group, "datasetName": name}},
                                      "schema": {"properties": {f: {"type": "string"} for f in fields}}}}).encode()


def _envelope(records: list[dict]) -> bytes:
    return json.dumps({"returnBody": {"statusCode": 200, "headers": {"Record-Total": [str(len(records))]},
                                      "data": json.dumps(records)}}).encode()


# ---------------------------------------------------------------- parsers on real answers
def test_parses_a_real_security_master_answer():
    records = parse_envelope((DATA / "bond_search_coreweave.json").read_bytes())
    assert len(records) == 12
    sec = parse_security(next(r for r in records if r["cusip"] == "21873SAL2"))
    assert (sec.issuer_name, sec.coupon_rate_pct, sec.maturity) == ("COREWEAVE INC", Decimal("2.875"), date(2033, 4, 1))
    assert sec.is_144a is True and sec.is_convertible is True
    assert (sec.last_trade_date, sec.last_sale_price) == (date(2026, 10, 16), Decimal("81.946000"))
    assert sec.last_trade_at is None  # BondSearch carries no lastTradeTime


def test_parses_real_end_of_day_history():
    iren = _json("eod_46270CAE9.json")
    rows = parse_eod(iren["eod"], "46270CAE9")
    assert len(iren["eod"]) == 180 and len(rows) == 179  # one day record has price 0 and no yield: no sale
    assert (rows[0].trade_date, rows[0].price, rows[0].yield_pct) == (date(2026, 1, 2), Decimal("79.35"), None)
    assert (rows[-1].trade_date, rows[-1].price) == (date(2026, 10, 16), Decimal("57.9426"))
    sec = parse_security(iren["sec"][0])
    assert sec.coupon_rate_pct == 0 and sec.maturity == date(2031, 7, 1) and sec.is_144a is True
    aep = _json("eod_00115AAS8_excerpt.json")
    kept = parse_eod(aep["eod"], "00115AAS8")
    zero_days = [r["tradeDate"] for r in aep["eod"] if r["lastSalePrice"] == 0]
    assert zero_days and len(kept) == len(aep["eod"]) - len(zero_days)  # day records with no last sale
    assert all(r.price > 0 and r.yield_pct is not None for r in kept)


def test_changed_or_odd_answers():
    assert parse_eod([{"cusip": "X" * 9, "tradeDate": "2026-02-20", "lastSalePrice": 100.5,
                       "lastSaleYield": 999999.99}])[0].yield_pct is None  # FINRA's 'no meaningful yield'
    with pytest.raises(ParseError, match="not JSON"):
        parse_envelope((DATA / "template_404.html").read_bytes())
    with pytest.raises(ParseError, match="statusCode"):
        parse_envelope(b'{"returnBody": {"statusCode": 403, "data": "[]"}}')
    with pytest.raises(ParseError, match="holds a row"):
        parse_eod([{"cusip": "68389XBX2", "tradeDate": "2026-10-16", "lastSalePrice": 52}], "46270CAE9")
    with pytest.raises(ParseError, match="two different"):
        parse_eod([{"cusip": "A" * 9, "tradeDate": "2026-10-16", "lastSalePrice": p} for p in (52, 53)])
    with pytest.raises(ParseError, match="template"):
        parse_template(b'{"returnBody": {}}')
    assert parse_envelope(b'{"returnBody": {"statusCode": 200, "data": ""}}') == []
    assert eastern_to_utc(date(2026, 10, 16), "15:59:07") == datetime(2026, 10, 16, 19, 59, 7, tzinfo=UTC)
    assert eastern_to_utc(date(2026, 12, 1), "09:30:00") == datetime(2026, 12, 1, 14, 30, tzinfo=UTC)


def test_refusals_are_recognised_on_real_answers():
    not_found = _json("template_404.meta.json")
    reason = refusal_reason(404, not_found["headers"], (DATA / "template_404.html").read_bytes(), expect_json=True)
    assert reason and "Not Found" in reason
    page = (DATA / "bond_page_68389XBX2.html").read_bytes()
    assert b"challenge-platform" in page  # Cloudflare's bot script rides on every page ...
    assert refusal_reason(200, _meta("bond_page_68389XBX2.meta.json"), page, expect_json=False) is None  # ... fine
    assert "cf-mitigated" in refusal_reason(403, {"cf-mitigated": "challenge"}, b"Just a moment...", expect_json=True)
    assert "429" in refusal_reason(429, {"Retry-After": "60"}, b"", expect_json=True)
    assert "403" in refusal_reason(403, {}, b"denied", expect_json=True)
    assert "login" in refusal_reason(200, {}, b"<html>", expect_json=True, final_url="https://gateway.finra.org/login")
    assert refusal_reason(200, {"content-type": "application/json"}, _envelope([]), expect_json=True) is None


def test_terms_note_and_licence_flag():
    terms = (DATA / "terms_of_use_excerpt.txt").read_text()
    for clause in ("Last modified: November 9, 2023",
                   "shall be used ONLY for your own non-commercial personal or professional use",
                   "d. develop or create a database of data using the FINRA Website",
                   "use any data mining, scraping or harvesting tools (including robots)",
                   "in conjunction with any machine learning, neural network, deep learning, predictive analytics"):
        assert clause in terms and clause in ft.TERMS_NOTE
    assert ft.SOURCE.blocked is False and ft.SOURCE.name == licensing.FINRA_TRACE == "FINRA TRACE (finra.org)"
    assert licensing.SOURCE_LICENCE[licensing.FINRA_TRACE]["status"] == "unlicensed-commercial"
    assert UA in ft.TERMS_NOTE
    assert bond_page_url("00202DAA5") == "https://www.finra.org/finra-data/fixed-income/bond?cusip=00202DAA5&bondType=CORP"


# ---------------------------------------------------------------- the page flow, replayed
class Replay:
    """httpx transport answering the bond page flow from fixtures; records every request."""

    def __init__(self, *, refuse_template: bool = False, records: dict[str, dict] | None = None):
        self.refuse_template = refuse_template
        self.records = records or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.startswith("https://www.finra.org/finra-data/fixed-income/bond"):
            return httpx.Response(200, headers={"content-type": "text/html; charset=UTF-8"},
                                  content=(DATA / "bond_page_68389XBX2.html").read_bytes())
        if url == BOND_TEMPLATE and self.refuse_template:
            meta = _json("template_404.meta.json")
            return httpx.Response(404, headers=meta["headers"], content=(DATA / "template_404.html").read_bytes())
        json_hdr = {"content-type": "application/json"}
        if url == BOND_TEMPLATE:
            return httpx.Response(200, headers=json_hdr, content=_template(
                "FixedIncomeMarket", "CorporateAndAgencySecurities", ["cusip", "issuerName", "couponRate"]))
        if url == PRICE_YIELD_TEMPLATE:
            return httpx.Response(200, headers=json_hdr, content=_template(
                "FixedIncomeMarket", "PriceYieldHistory", ["cusip", "tradeDate", "lastSalePrice"]))
        body = json.loads(request.content)
        cusip = body["orFilters"][0]["compareFilters"][0]["fieldValue"]
        rec = self.records.get(cusip)
        if url.endswith("/CorporateAndAgencySecurities"):
            return httpx.Response(200, headers=json_hdr, content=_envelope(rec["sec"] if rec else []))
        assert url.endswith("/PriceYieldHistory")
        return httpx.Response(200, headers=json_hdr, content=_envelope(rec["eod"] if rec else []))


def _client(replay: Replay) -> BondPageClient:
    return BondPageClient(ft.SOURCE_ID, client=httpx.Client(transport=httpx.MockTransport(replay)),
                          user_agent=UA, sleep=lambda _s: None)


def _instrument(committed, iid: str, cusip: str) -> None:
    committed.execute("INSERT INTO instrument (id, name, kind, issuer_name, cusip) VALUES (%s, %s, 'convert', 'x', %s)",
                      (iid, iid, cusip))


def test_a_refusal_stops_the_run_and_is_recorded(committed):
    _instrument(committed, "EX-IREN-180", "46270CAE9")
    replay = Replay(refuse_template=True)
    out = ft.run(flow=_client(replay), now=datetime(2026, 10, 21, 23, 0, tzinfo=UTC))
    assert out["ok"] is False and out["refused"] is True and out["requests"] == 2
    assert "AccessRefused: HTTP 404" in out["error"]
    assert [r.method for r in replay.requests] == ["GET", "GET"]  # the page, then the template: nothing more
    assert all(r.headers["user-agent"] == UA for r in replay.requests)
    src = committed.execute("SELECT blocked, last_error, last_ok_at FROM source WHERE id = 'finra_trace'").fetchone()
    assert src["blocked"] is False and "AccessRefused" in src["last_error"] and src["last_ok_at"] is None
    assert committed.execute("SELECT ok FROM source_run WHERE source_id = 'finra_trace'").fetchone()["ok"] is False
    doc = committed.execute("SELECT doc_type, meta FROM document WHERE source_id = 'finra_trace'").fetchone()
    assert doc["doc_type"] == "refusal" and doc["meta"]["http"]["status"] == 404
    assert committed.execute("SELECT count(*) AS n FROM price_obs").fetchone()["n"] == 0


def test_page_flow_writes_trades_and_missing_facts_once(committed):
    _instrument(committed, "EX-IREN-180", "46270CAE9")
    iren = _json("eod_46270CAE9.json")
    sec = dict(iren["sec"][0], lastTradeTime="15:59:07")
    replay = Replay(records={"46270CAE9": {"sec": [sec], "eod": iren["eod"]}})
    evening = datetime(2026, 10, 16, 23, 0, tzinfo=UTC)  # 19:00 ET: the 10/16 row is final
    out = ft.run(flow=_client(replay), now=evening)
    assert out["ok"], out
    assert out["prices_new"] == 179 + 1 and out["facts_new"] == 4 and out["cusips_with_history"] == 1
    bond_post, hist_post = [json.loads(r.content) for r in replay.requests if r.method == "POST"]
    assert bond_post["orFilters"][0]["compareFilters"][0] == {"fieldName": "cusip", "fieldValue": "46270CAE9",
                                                             "compareType": "EQUAL"}
    assert hist_post["dateRangeFilters"][0]["startDate"].startswith("2021-10-17")  # first run: five years
    assert all(r.headers["user-agent"] == UA and r.headers["origin"] == "https://www.finra.org"
               for r in replay.requests if r.method == "POST")
    rows = committed.execute(
        """SELECT obs_date, obs_at, price, tag, source_name, source_url, kind FROM price_obs
           WHERE instrument_id = 'EX-IREN-180' ORDER BY obs_date DESC, obs_at NULLS LAST""").fetchall()
    assert {(r["kind"], r["tag"], r["source_name"]) for r in rows} == {("trade", "F", "FINRA TRACE (finra.org)")}
    assert {r["source_url"] for r in rows} == {bond_page_url("46270CAE9")}
    assert rows[0]["obs_at"] == datetime(2026, 10, 16, 19, 59, 7, tzinfo=UTC) and float(rows[0]["price"]) == 57.9426
    facts = {r["field"]: r["value"] for r in committed.execute(
        "SELECT field, value FROM fact_current WHERE subject_id = 'EX-IREN-180'").fetchall()}
    assert facts == {"coupon_rate_pct": 0, "maturity": "2031-07-01", "is_144a": True, "issuer_name": "IREN LTD"}

    assert ft.run(flow=_client(replay), now=evening)["skipped"]  # once a day after the close
    again = ft.run(flow=_client(replay := Replay(records=replay.records)), now=evening, force=True)
    assert again["ok"] and again["prices_new"] == 0 and again["facts_new"] == 0 and again["documents_new"] == 0
    hist = [json.loads(r.content) for r in replay.requests if r.method == "POST"][1]
    assert hist["dateRangeFilters"][0]["startDate"].startswith("2026-10-02")  # later runs: 14 days


def test_today_is_written_only_after_the_trace_close(committed):
    _instrument(committed, "EX-IREN-180", "46270CAE9")
    iren = _json("eod_46270CAE9.json")
    replay = Replay(records={"46270CAE9": iren})
    afternoon = datetime(2026, 10, 16, 18, 0, tzinfo=UTC)  # 14:00 ET
    assert ft.run(flow=_client(replay), now=afternoon)["skipped"].startswith("before the 18:30 ET")
    assert replay.requests == []  # not one request before the close
    out = ft.run(flow=_client(replay), now=afternoon, force=True)
    assert out["ok"] and out["prices_new"] == 178 and out["partial_day_skipped"] == 1


def test_a_cusip_finra_does_not_list_costs_one_request(committed):
    _instrument(committed, "EX-LOAN-1", "21882DAB6")
    replay = Replay()
    out = ft.run(flow=_client(replay), now=datetime(2026, 10, 16, 23, 0, tzinfo=UTC))
    assert out["ok"] and out["not_on_finra"] == ["21882DAB6"] and out["prices_new"] == 0
    assert [r.method for r in replay.requests] == ["GET", "GET", "POST"]


def test_scheduled_weekdays_after_the_trace_close():
    assert app.tasks["collect.finra_trace"].queue == "collect"
    crons = {pid: pt.cron for (name, pid), pt in app.periodic_registry.periodic_tasks.items()
             if name == "collect.finra_trace"}
    assert crons == {"weekdays_1845_et": "45 22,23 * * 1-5"}
    for day in (date(2026, 7, 1), date(2026, 12, 1)):
        slots = [int(datetime(day.year, day.month, day.day, h, 45, tzinfo=UTC).timestamp()) for h in (22, 23)]
        assert [due_et(ts, 18, 45) for ts in slots].count(True) == 1
    assert ft.collect_task(timestamp=int(datetime(2026, 12, 1, 22, 45, tzinfo=UTC).timestamp())) == {
        "skipped": "not 18:45 ET"}
    assert "finra_trace" in cli._groups["collect"]


@pytest.mark.network
def test_live_run_twice_is_served_or_refused_and_recorded(committed):
    _instrument(committed, "EX-ORCL-TEST", "68389XBX2")
    outs = [ft.run(cusips=["68389XBX2"], force=True) for _ in range(2)]
    for out in outs:
        assert out["requests"] <= 6, out
        if not out["ok"]:  # finra.org refused this client: recorded, nothing written, no workaround
            assert out["refused"] and out["prices_new"] == 0
    if outs[0]["ok"]:
        assert outs[0]["prices_new"] > 0 and outs[1]["prices_new"] == 0
    runs = committed.execute("SELECT ok, error FROM source_run WHERE source_id = 'finra_trace' ORDER BY id").fetchall()
    assert [r["ok"] for r in runs] == [o["ok"] for o in outs]
