"""boerse_frankfurt: parsing real price_information / quote_box responses, price_obs + quote facts and
reruns, not-listed ISINs, stopping at bot protection, the schedule, the terms note, and one live run."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from marden import cli
from marden.collectors import boerse_frankfurt as FWB
from marden.collectors.market_http import MarketFetcher
from marden.collectors.rates_common import ParseError, RunStats
from marden.core import db, licensing
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "boerse_frankfurt"
CHALLENGE = (Path(__file__).parent / "data" / "market_http" / "cloudflare_challenge.html").read_bytes()
CRWV33, ORCL50, BTDR31 = "US21873SAL25", "US68389XBX21", "US09175RAE09"  # fetched 2026-10-21 ~13:38 UTC


def _pi(isin: str) -> bytes:
    name = f"price_information_{isin}{'_404' if isin == BTDR31 else ''}.json"
    return (DATA / name).read_bytes()


def _qb(isin: str) -> bytes:
    return (DATA / f"quote_box_{isin}{'_empty' if isin == BTDR31 else ''}.json").read_bytes()


def _instrument(conn, iid: str, *, isin: str | None = None, cusip: str | None = None) -> None:
    conn.execute(
        "INSERT INTO instrument (id, name, kind, issuer_name, cusip, isin) VALUES (%s, %s, 'convert', 'x', %s, %s)",
        (iid, iid, cusip, isin),
    )


def _server(calls: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.url.path}?isin={request.url.params['isin']}")
        assert request.headers["user-agent"] == "Marden research cody@marden.ai"
        isin = request.url.params["isin"]
        if request.url.path.endswith("/price_information/single"):
            body = _pi(isin)
            return httpx.Response(404 if isin == BTDR31 else 200, content=body,
                                  headers={"content-type": "application/json"})
        return httpx.Response(200, content=_qb(isin), headers={"content-type": "application/json"})
    return handler


def _fetcher(handler) -> MarketFetcher:
    return MarketFetcher(client=httpx.Client(transport=httpx.MockTransport(handler)), jitter=(0, 0),
                         backoff_base=0.01)


def test_parses_a_listed_bond():
    q = FWB.parse_quote(CRWV33, _pi(CRWV33), _qb(CRWV33))
    assert q is not None and q.in_percent
    assert (q.last, q.bid, q.ask, q.mid) == (Decimal("82.52"), Decimal("82.52"), Decimal("84.67"), Decimal("83.595"))
    assert q.last_at == datetime(2026, 10, 21, 9, 54, 15, tzinfo=timezone(timedelta(hours=2)))
    assert q.observation() == (Decimal("82.52"), q.last_at)
    assert q.detail["bid_size"] == 2000000 and q.detail["turnover_nominal"] == 0 and q.detail["price_fixings"] == 1
    assert q.detail["quote_at"] == "2026-10-21T07:54:19+00:00" and q.detail["currency"] == "USD"


def test_an_unlisted_isin_has_no_quote():
    assert FWB.parse_quote(BTDR31, _pi(BTDR31), _qb(BTDR31)) is None
    assert _qb(BTDR31) == b""


def test_a_side_quoted_at_zero_is_no_quote():
    isin = "US68389XDA00"  # fetched 2026-10-21 14:03 UTC, seconds after a fixing: both sides 0, size 0
    q = FWB.parse_quote(isin, (DATA / f"price_information_{isin}.json").read_bytes(),
                        (DATA / f"quote_box_{isin}_no_quote.json").read_bytes())
    assert q is not None and (q.bid, q.ask, q.mid) == (None, None, None)
    assert q.observation() == (Decimal("84.77"), q.last_at)
    assert not {"bid", "ask", "bid_size", "ask_size", "spread"} & set(q.detail)


def test_mid_is_used_when_there_is_no_last_fixing():
    pi = json.loads(_pi(ORCL50))
    pi["lastPrice"] = pi["timestampLastPrice"] = None
    q = FWB.parse_quote(ORCL50, json.dumps(pi).encode(), _qb(ORCL50))
    assert q is not None and q.observation() == (Decimal("52"), q.quote_at)  # (51.77 + 52.23) / 2


def test_changed_or_wrong_responses_are_refused():
    with pytest.raises(ParseError, match="not US68389XBX21"):
        FWB.parse_quote(ORCL50, _pi(CRWV33), None)
    with pytest.raises(ParseError, match="not JSON"):
        FWB.parse_quote(CRWV33, CHALLENGE, None)
    bad = json.loads(_pi(CRWV33))
    bad["lastPrice"] = 8252
    with pytest.raises(ParseError, match="percent of par"):
        FWB.parse_quote(CRWV33, json.dumps(bad).encode(), None)
    bad = json.loads(_pi(CRWV33))
    bad["timestampLastPrice"] = "2026-10-21T09:54:15"
    with pytest.raises(ParseError, match="time zone"):
        FWB.parse_quote(CRWV33, json.dumps(bad).encode(), None)


def test_writes_price_and_quote_once(committed):
    _instrument(committed, "EX-CRWV-099", isin=CRWV33)
    q = FWB.parse_quote(CRWV33, _pi(CRWV33), _qb(CRWV33))
    for expected in (2, 0):
        with db.transaction() as conn:
            got = FWB.write_quote(conn, q, ["EX-CRWV-099"], fetched_on=date(2026, 10, 21), producer=FWB.PRODUCER,
                                  price_doc=None, quote_doc=None, stats=RunStats())
        assert got == expected
    p = committed.execute("SELECT * FROM price_obs").fetchone()
    assert (p["instrument_id"], p["obs_date"], p["kind"], p["price"], p["tag"]) == (
        "EX-CRWV-099", date(2026, 10, 21), "exchange", Decimal("82.52"), "R")
    assert p["obs_at"] == datetime(2026, 10, 21, 7, 54, 15, tzinfo=UTC) and p["volume"] == 0
    assert p["source_name"] == "Börse Frankfurt" and p["source_url"] == "https://live.deutsche-boerse.com/anleihe/us21873sal25"
    f = committed.execute("SELECT * FROM fact WHERE field = 'exchange_quote'").fetchone()
    assert (f["subject_id"], f["valid_from"], f["tag"], f["source_name"]) == (
        "EX-CRWV-099", date(2026, 10, 21), "R", "Börse Frankfurt")
    assert (f["value"]["bid"], f["value"]["ask"], f["value"]["ask_size"]) == (82.52, 84.67, 2000000)
    assert licensing.is_unlicensed(p["source_name"]) and licensing.is_unlicensed(f["source_name"])


def test_run_over_the_watched_isins_then_rerun(committed):
    _instrument(committed, "EX-CRWV-099", isin=CRWV33)
    _instrument(committed, "EX-ORCL-254", cusip="68389XBX2")  # US ISIN derived from the CUSIP
    _instrument(committed, "NP-BTDR-09175RAE0", cusip="09175RAE0")
    calls: list[str] = []
    first = FWB.run(today=date(2026, 10, 21), fetcher=_fetcher(_server(calls)))
    assert first["ok"], first
    assert first["watched"] == 3 and first["listed"] == 2 and first["not_listed"] == {BTDR31: "2026-10-21"}
    assert first["price_obs_new"] == 2 and first["facts_new"] == 2 and first["requests"] == 5
    assert sorted(first["with_data"]) == [CRWV33, ORCL50]
    calls.clear()
    second = FWB.run(today=date(2026, 10, 22), fetcher=_fetcher(_server(calls)))
    assert second["ok"] and second["price_obs_new"] == 0 and second["facts_new"] == 0
    assert second["skipped_recently_not_listed"] == 1 and not any(BTDR31 in c for c in calls)  # not re-asked
    assert second["documents_new"] == 0
    later = FWB.recently_not_listed(date(2026, 10, 21) + timedelta(days=FWB.NOT_LISTED_RECHECK_DAYS))
    assert later == {}  # re-checked once the window passes


def test_one_unparseable_isin_fails_the_run_but_keeps_the_rest(committed):
    _instrument(committed, "EX-CRWV-099", isin=CRWV33)
    _instrument(committed, "EX-ORCL-254", isin=ORCL50)

    def handler(request: httpx.Request) -> httpx.Response:
        isin = request.url.params["isin"]
        body = _pi(CRWV33) if request.url.path.endswith("/price_information/single") else _qb(isin)
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})  # ORCL gets CRWV's

    out = FWB.run(today=date(2026, 10, 21), fetcher=_fetcher(handler))
    assert out["ok"] is False and out["error"].startswith("ParseError: 1 ISIN responses did not parse")
    assert list(out["parse_errors"]) == [ORCL50] and out["with_data"] == [CRWV33]
    assert [r["instrument_id"] for r in committed.execute("SELECT instrument_id FROM price_obs").fetchall()] == [
        "EX-CRWV-099"]


def test_a_challenge_is_recorded_and_the_run_stops(committed):
    for i, isin in enumerate((CRWV33, ORCL50)):
        _instrument(committed, f"I-{i}", isin=isin)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        headers = {"cf-mitigated": "challenge", "content-type": "text/html"}
        return httpx.Response(403, content=CHALLENGE, headers=headers)

    out = FWB.run(today=date(2026, 10, 21), fetcher=_fetcher(handler))
    assert out["ok"] is False and out["error"].startswith("Challenged: stopped: HTTP 403")
    assert len(calls) == 1  # no retry, no other ISIN, no other headers
    assert "403" in committed.execute("SELECT last_error FROM source WHERE id = 'boerse_frankfurt'").fetchone()[
        "last_error"]
    assert committed.execute("SELECT count(*) AS n FROM price_obs").fetchone()["n"] == 0


def test_a_challenge_page_with_status_200_also_stops(committed):
    _instrument(committed, "I-0", isin=CRWV33)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=CHALLENGE, headers={"content-type": "text/html; charset=UTF-8"})

    out = FWB.run(today=date(2026, 10, 21), fetcher=_fetcher(handler))
    assert out["ok"] is False and "HTML challenge page" in out["error"]


def test_terms_note_quotes_the_disclaimer():
    terms = re.sub(r"\s+", " ", (DATA / "disclaimer_excerpt.txt").read_text())
    quoted = [q for q in FWB.TERMS_NOTE.split('"')[1::2] if len(q) >= 40]
    assert len(quoted) == 1 and quoted[0] in terms
    assert FWB.SOURCE.blocked is False and "UNLICENSED" in FWB.SOURCE.terms_note
    assert FWB.SOURCE_NAME == licensing.BOERSE_FRANKFURT == "Börse Frankfurt"


def test_scheduled_on_weekdays_after_the_frankfurt_close():
    assert app.tasks["collect.boerse_frankfurt"].queue == "collect"
    crons = {pid: pt.cron for (name, pid), pt in app.periodic_registry.periodic_tasks.items()
             if name == "collect.boerse_frankfurt"}
    assert crons == {"weekdays_1805_berlin": "5 16,17 * * 1-5"}
    for day in (date(2026, 7, 1), date(2026, 12, 1)):  # CEST and CET
        slots = [int(datetime(day.year, day.month, day.day, h, 5, tzinfo=UTC).timestamp()) for h in (16, 17)]
        assert [FWB.due_berlin(ts, 18, 5) for ts in slots].count(True) == 1
    assert FWB.collect_task(timestamp=int(datetime(2026, 12, 1, 16, 5, tzinfo=UTC).timestamp())) == {
        "skipped": "not 18:05 Frankfurt time"}
    assert "boerse_frankfurt" in cli._groups["collect"]


@pytest.mark.network
def test_live_run_twice(committed):
    _instrument(committed, "EX-ORCL-254", isin=ORCL50)
    first = FWB.run(isins=[ORCL50])
    if not first["ok"] and first["error"].startswith("Challenged"):
        pytest.skip(f"Börse Frankfurt refused the request; recorded on the source: {first['error']}")
    assert first["ok"], first
    assert first["with_data"] == [ORCL50] and first["price_obs_new"] == 1
    second = FWB.run(isins=[ORCL50])
    assert second["ok"], second
    if second["documents_new"] == 0:  # nothing changed at the exchange between the runs
        assert second.get("price_obs_new", 0) == 0 and second["facts_new"] == 0
