"""Yahoo Finance end-of-day equity prices -> series facts `equity:<TICKER>` (tag F, 'Yahoo Finance').

Unlicensed for commercial use (marden.core.licensing; docs/methods/licensing.md): the business owner
decided Marden collects it daily and accepts the terms risk. The terms clause and robots.txt are on
the source row; nothing here works around Yahoo's controls (collectors/market_http.py).

Endpoint (the one finance.yahoo.com's own charts call), one request per ticker:
https://query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}?range={2y|1mo}&interval=1d&events=div%2Csplit&includeAdjustedClose=true

- Watched tickers: market_watch.BASE_TICKERS plus every entity.ticker.
- Backfill: 2 years for a ticker this collector has not loaded (or last loaded > 25 days ago, or a
  new split in the window, since Yahoo restates history for splits); otherwise the last month.
- Facts on subject_type 'series', subject_id 'equity:<TICKER>', one per trading date (valid_from):
  open, high, low, close, adj_close (unit = trading currency), volume (shares); dividend (per share)
  and split ({numerator, denominator}) on their ex-dates. source_url is the ticker's Yahoo history page;
  the exact response is the fact's document. A value `seed legacy_yahoo` already holds for the date is
  not written again (write_chart).
- A session that has not closed (the run is early, or the bar is still live) is not written.
- Cadence: weekdays 18:20 ET, after the US close. `python -m marden collect yahoo_equity [--ticker T]`.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from dateutil import tz

from marden import cli
from marden.collectors.market_http import Challenged, MarketFetcher
from marden.collectors.market_watch import watched_tickers, yahoo_symbol
from marden.collectors.rates_common import ParseError, RunStats, collect, due_et, print_stats, today_et
from marden.core import db, http, licensing
from marden.core.db import Conn
from marden.core.facts import FactWriter
from marden.core.http import FetchError, fetch_document
from marden.core.jsonutil import canonical_json
from marden.core.queue import app
from marden.core.sources import declare_source

SOURCE_ID = "yahoo_equity"
SOURCE_NAME = licensing.YAHOO_FINANCE
PRODUCER = f"collector:{SOURCE_ID}"
HOST = "query1.finance.yahoo.com"
CHART_URL = (f"https://{HOST}/v8/finance/chart/{{symbol}}?range={{range}}&interval=1d"
             "&events=div%2Csplit&includeAdjustedClose=true")
PAGE_URL = "https://finance.yahoo.com/quote/{symbol}/history/"
SERIES_PREFIX = "equity:"
PRICE_FIELDS = ("open", "high", "low", "close", "adj_close")
BACKFILL_RANGE = "2y"
DAILY_RANGE = "1mo"
BACKFILL_IF_OLDER_DAYS = 25
FINAL_AFTER = timedelta(minutes=10)  # a session is final this long after its scheduled close
RUN_AT_ET = (18, 20)
CRON_UTC = "20 22,23 * * 1-5"  # 18:20 ET is 22:20 UTC (EDT) or 23:20 UTC (EST); due_et keeps the right one
PRICE_QUANTUM = Decimal("0.0001")  # the API sends float32 noise (69.69000244140625)

TERMS_URL = "https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html"
TERMS_NOTE = (
    "UNLICENSED for commercial use; collected by decision of the business owner, who was told these terms "
    "and accepts the risk (docs/methods/licensing.md; licence flag in marden.core.licensing). Checked "
    f"2026-10-21: Yahoo Terms of Service ({TERMS_URL}, last updated 4 August 2026) say you may not \"access "
    "or collect data, or attempt to access or collect data, from our Services using any automated means, "
    "devices, programs, algorithms or methodologies, including but not limited to robots, spiders, scrapers, "
    "data mining tools, or data gathering or extraction tools, for any purpose without our express, prior "
    "permission\", may not use data \"(a) to create any database, archive, mobile application, data feed, "
    "widget or any other aggregated data source that competes with or constitutes a material substitute for "
    "the Services\", and \"Unless otherwise expressly stated, you may not access or reuse the Services, or any "
    "portion thereof, for any commercial purpose.\" https://query1.finance.yahoo.com/robots.txt: "
    "\"User-agent: * / Disallow: /\". No Yahoo permission has been obtained. Access: declared User-Agent "
    "\"Marden research cody@marden.ai\", 1 request/second plus jitter, one request per ticker per weekday (a second "
    "after a new split) after "
    "18:00 ET; any challenge, login wall, 401/403 or repeated 429 is recorded here and the run stops (no "
    "evasion). Licensed replacement: an exchange or vendor equity feed with redistribution rights."
)

SOURCE = declare_source(SOURCE_ID, SOURCE_NAME, "api", f"https://{HOST}/v8/finance/chart/", 24 * 60, TERMS_NOTE)
http.set_rate_limit(HOST, 1.0)


def series_id(ticker: str) -> str:
    return f"{SERIES_PREFIX}{ticker.strip().upper()}"


def chart_url(symbol: str, range_: str) -> str:
    return CHART_URL.format(symbol=symbol, range=range_)


def page_url(symbol: str) -> str:
    return PAGE_URL.format(symbol=symbol)


# ---------------------------------------------------------------- parse
@dataclass(frozen=True)
class Bar:
    day: date
    values: dict[str, Decimal | int]  # PRICE_FIELDS and 'volume' that the response gives


@dataclass
class Chart:
    symbol: str
    currency: str
    bars: list[Bar]
    dividends: list[tuple[date, Decimal]] = field(default_factory=list)
    splits: list[tuple[date, int, int]] = field(default_factory=list)
    unfinished: list[date] = field(default_factory=list)  # bars of a session that had not closed
    duplicate_days: int = 0


class SymbolNotFound(ParseError):
    """Yahoo has no chart for this symbol (delisted or never listed)."""


def _price(value: Any, what: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Decimal | int):
        raise ParseError(f"{what} is not a number: {value!r}")
    dec = Decimal(value)
    if not dec.is_finite() or dec < 0:
        raise ParseError(f"{what} is not a finite non-negative number: {value!r}")
    return dec.quantize(PRICE_QUANTUM, rounding=ROUND_HALF_EVEN).normalize()


def _series(container: Any, key: str, n: int) -> list[Any]:
    values = container.get(key) if isinstance(container, dict) else None
    if values is None:
        return [None] * n
    if not isinstance(values, list) or len(values) != n:
        raise ParseError(f"chart indicator {key} does not line up with the timestamps")
    return values


def _unfinished_session(meta: dict[str, Any], zone: Any, fetched_at: datetime | None) -> date | None:
    """The date of the current session when it had not closed when the response was made."""
    regular = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    start, end = regular.get("start"), regular.get("end")
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    closed = (meta.get("regularMarketTime") or 0) >= end
    if fetched_at is not None:
        closed = closed or fetched_at >= datetime.fromtimestamp(end, UTC) + FINAL_AFTER
    return None if closed else datetime.fromtimestamp(start, UTC).astimezone(zone).date()


def parse_chart(data: bytes, *, symbol: str | None = None, fetched_at: datetime | None = None) -> Chart:
    """Daily bars, dividends and splits from one chart response (fetched_at: when it was fetched)."""
    try:
        payload = json.loads(data, parse_float=Decimal)
    except ValueError as exc:
        raise ParseError(f"chart response is not JSON: {exc}") from None
    chart = payload.get("chart") if isinstance(payload, dict) else None
    if not isinstance(chart, dict):
        raise ParseError("chart response has no 'chart' object")
    if chart.get("error"):
        err = chart["error"]
        code = err.get("code") if isinstance(err, dict) else err
        if code == "Not Found":
            raise SymbolNotFound(f"no chart for {symbol}: {err}")
        raise ParseError(f"chart error: {err}"[:300])
    results = chart.get("result")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise ParseError("chart response does not hold exactly one result")
    res = results[0]
    meta = res.get("meta") or {}
    got = str(meta.get("symbol") or "")
    if symbol and got.upper() != symbol.upper():
        raise ParseError(f"asked for {symbol}, the response is for {got!r}")
    if meta.get("dataGranularity") not in (None, "1d"):
        raise ParseError(f"granularity {meta.get('dataGranularity')!r} is not daily")
    zone = tz.gettz(meta.get("exchangeTimezoneName") or "America/New_York")
    if zone is None:
        raise ParseError(f"unknown exchange time zone {meta.get('exchangeTimezoneName')!r}")
    currency = str(meta.get("currency") or "").upper()
    if not currency:
        raise ParseError("chart meta has no currency")
    stamps = res.get("timestamp") or []
    if not isinstance(stamps, list) or not all(isinstance(t, int) for t in stamps):
        raise ParseError("chart timestamps are not epoch seconds")
    n = len(stamps)
    indicators = res.get("indicators") or {}
    quote = (indicators.get("quote") or [{}])[0] if stamps else {}
    adj = (indicators.get("adjclose") or [{}])[0] if stamps else {}
    cols = {k: _series(quote, k, n) for k in ("open", "high", "low", "close", "volume")}
    cols["adj_close"] = _series(adj, "adjclose", n)
    unfinished = _unfinished_session(meta, zone, fetched_at)
    out = Chart(got or (symbol or ""), currency, [])
    by_day: dict[date, Bar] = {}
    for i, ts in enumerate(stamps):
        day = datetime.fromtimestamp(ts, UTC).astimezone(zone).date()
        if unfinished is not None and day >= unfinished:
            if day not in out.unfinished:
                out.unfinished.append(day)
            continue
        values: dict[str, Decimal | int] = {}
        for name in PRICE_FIELDS:
            v = _price(cols[name][i], f"{got} {day} {name}")
            if v is not None:
                values[name] = v
        vol = cols["volume"][i]
        if vol is not None:
            if isinstance(vol, bool) or not isinstance(vol, int | Decimal) or vol < 0:
                raise ParseError(f"{got} {day} volume is not a count: {vol!r}")
            values["volume"] = int(vol)
        if "close" not in values:
            continue  # no trade data for the day: nothing written
        if day in by_day:
            out.duplicate_days += 1
        by_day[day] = Bar(day, values)  # a repeated day keeps its last bar
    out.bars = [by_day[d] for d in sorted(by_day)]
    events = res.get("events") or {}
    for item in (events.get("dividends") or {}).values():
        amount = _price(item.get("amount"), f"{got} dividend")
        if amount is not None and isinstance(item.get("date"), int):
            out.dividends.append((datetime.fromtimestamp(item["date"], UTC).astimezone(zone).date(), amount))
    for item in (events.get("splits") or {}).values():
        num, den = item.get("numerator"), item.get("denominator")
        if isinstance(item.get("date"), int) and num and den:
            day = datetime.fromtimestamp(item["date"], UTC).astimezone(zone).date()
            out.splits.append((day, int(num), int(den)))
    out.dividends.sort()
    out.splits.sort()
    return out


# ---------------------------------------------------------------- write
def write_chart(
    conn: Conn, ticker: str, chart: Chart, *, producer: str, document_id: int | None, stats: RunStats,
) -> int:
    """Published series facts for every bar and corporate action in `chart`. Returns facts created.

    One value per series, field and date across writers: a value another writer (the collector or the
    `legacy_yahoo` archive loader) already holds is not written again; the archive never adds a value for
    a date the collector holds; a changed value from the collector (a restatement) is written."""
    facts = FactWriter(conn)
    sid = series_id(ticker)
    url = page_url(chart.symbol or yahoo_symbol(ticker))
    held = held_values(conn, sid)
    created = 0

    def add(field_name: str, value: Any, day: date, unit: str | None) -> None:
        nonlocal created
        others = {p: vals for p, vals in held.get((field_name, day), {}).items() if p != producer}
        if others and (canonical_json(value) in set().union(*others.values()) or
                       (producer != PRODUCER and PRODUCER in others)):
            stats.facts_seen += 1
            stats.extra["held_by_other_writer"] = stats.extra.get("held_by_other_writer", 0) + 1
            return
        res = facts.add("series", sid, field_name, value, tag="F", source_name=SOURCE_NAME, source_url=url,
                        valid_from=day, producer=producer, status="published", unit=unit, document_id=document_id)
        stats.facts_seen += 1
        if res.created:
            created += 1
            stats.facts_new += 1
            stats.new_by_series[sid] = stats.new_by_series.get(sid, 0) + 1

    for bar in chart.bars:
        for name, value in bar.values.items():
            add(name, value, bar.day, "shares" if name == "volume" else chart.currency)
    for day, amount in chart.dividends:
        add("dividend", amount, day, chart.currency)
    for day, num, den in chart.splits:
        add("split", {"numerator": num, "denominator": den}, day, None)
    if chart.bars:
        last = chart.bars[-1]
        newest = stats.latest.get(sid)
        if newest is None or newest[0] < last.day.isoformat():
            stats.latest[sid] = [last.day.isoformat(), float(last.values["close"])]
    return created


def held_values(conn: Conn, sid: str) -> dict[tuple[str, date], dict[str, set[str]]]:
    """(field, date) -> producer -> canonical values of the published Yahoo Finance facts on a series."""
    out: dict[tuple[str, date], dict[str, set[str]]] = {}
    for r in conn.execute(
        """SELECT field, valid_from, producer, value FROM fact WHERE subject_type = 'series' AND subject_id = %s
           AND source_name = %s AND status = 'published'""", (sid, SOURCE_NAME),
    ).fetchall():
        by_producer = out.setdefault((r["field"], r["valid_from"]), {})
        by_producer.setdefault(r["producer"], set()).add(canonical_json(r["value"]))
    return out


def latest_loaded(conn: Conn, ticker: str) -> date | None:
    """The newest close held for the ticker (from either writer), or None until the collector has loaded it."""
    row = conn.execute(
        """SELECT max(valid_from) AS d, bool_or(producer = %s) AS own FROM fact WHERE subject_type = 'series'
           AND subject_id = %s AND field = 'close' AND source_name = %s AND status = 'published'""",
        (PRODUCER, series_id(ticker), SOURCE_NAME),
    ).fetchone()
    return row["d"] if row and row["own"] else None


def stored_splits(conn: Conn, ticker: str) -> set[date]:
    rows = conn.execute(
        "SELECT valid_from FROM fact WHERE subject_type = 'series' AND subject_id = %s AND field = 'split'",
        (series_id(ticker),),
    ).fetchall()
    return {r["valid_from"] for r in rows}


# ---------------------------------------------------------------- run
def _fetch_chart(ticker: str, range_: str, fetcher: MarketFetcher, stats: RunStats) -> tuple[Chart, int]:
    symbol = yahoo_symbol(ticker)
    doc = fetch_document(
        SOURCE_ID, chart_url(symbol, range_), doc_type="yahoo_chart_json", title=f"Yahoo chart {symbol} {range_}",
        external_id=symbol, meta={"ticker": ticker, "range": range_}, fetcher=fetcher,
    )
    stats.document(doc)
    return parse_chart(doc.read(), symbol=symbol, fetched_at=doc.fetched_at), doc.id


def collect_ticker(ticker: str, today: date, fetcher: MarketFetcher, stats: RunStats, *, backfill: bool) -> None:
    with db.transaction() as conn:
        latest = latest_loaded(conn, ticker)
    stale = latest is None or (today - latest).days > BACKFILL_IF_OLDER_DAYS
    range_ = BACKFILL_RANGE if backfill or stale else DAILY_RANGE
    try:
        chart, doc_id = _fetch_chart(ticker, range_, fetcher, stats)
        if range_ == DAILY_RANGE:
            with db.transaction() as conn:
                known = stored_splits(conn, ticker)
            if any(day not in known for day, _, _ in chart.splits):
                range_ = BACKFILL_RANGE  # a split restates history: re-learn the whole window
                chart, doc_id = _fetch_chart(ticker, range_, fetcher, stats)
    except Challenged:
        raise  # bot protection or rate limiting: the whole run stops
    except (SymbolNotFound, FetchError) as exc:
        if isinstance(exc, FetchError) and exc.status != 404:
            raise
        stats.extra.setdefault("not_found", []).append(ticker)
        return
    with db.transaction() as conn:
        write_chart(conn, ticker, chart, producer=PRODUCER, document_id=doc_id, stats=stats)
    info = stats.extra.setdefault("tickers", {})
    info[ticker] = {"range": range_, "bars": len(chart.bars),
                    "last": chart.bars[-1].day.isoformat() if chart.bars else None}
    if chart.unfinished:
        stats.extra.setdefault("unfinished_sessions_skipped", {})[ticker] = [d.isoformat() for d in chart.unfinished]


def run(
    *, tickers: list[str] | None = None, backfill: bool = False, today: date | None = None,
    fetcher: MarketFetcher | None = None,
) -> dict[str, Any]:
    """One collection run over the watched tickers (or `tickers`). Returns the run stats."""
    today = today or today_et()
    own = fetcher is None
    market = fetcher or MarketFetcher()

    def body(stats: RunStats) -> None:
        if tickers:
            names = sorted({t.strip().upper() for t in tickers})
        else:
            with db.transaction() as conn:
                names = watched_tickers(conn)
        stats.extra["watched"] = len(names)
        try:
            for ticker in names:
                collect_ticker(ticker, today, market, stats, backfill=backfill)
        finally:
            stats.extra["with_data"] = len(stats.extra.get("tickers", {}))
            stats.extra["requests"] = market.requests

    try:
        return collect(SOURCE, body)
    finally:
        if own:
            market.close()


# ---------------------------------------------------------------- schedule and CLI
@app.periodic(cron=CRON_UTC, periodic_id="weekdays_1820_et")
@app.task(name="collect.yahoo_equity", queue="collect", queueing_lock="collect.yahoo_equity",
          lock="collect.yahoo_equity")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    if not due_et(timestamp, *RUN_AT_ET):
        return {"skipped": "not 18:20 ET"}
    return run()


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--ticker", action="append", dest="tickers", help="only this ticker (repeatable)")
    p.add_argument("--backfill", action="store_true", help="fetch the full 2-year window for every ticker")


@cli.action("collect", SOURCE_ID, help="Yahoo Finance daily equity prices -> series equity:<TICKER>",
            configure=_args)
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run(tickers=args.tickers, backfill=args.backfill))
