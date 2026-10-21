"""Börse Frankfurt (XFRA) bond quotes -> price_obs kind 'exchange' (tag R, 'Börse Frankfurt') + quote facts.

Unlicensed for commercial use (marden.core.licensing; docs/methods/licensing.md): the business owner
decided Marden collects it daily and accepts the terms risk. The terms clause is on the source row;
nothing here works around Deutsche Börse's controls (collectors/market_http.py).

Endpoints: the two JSON calls the live.deutsche-boerse.com bond page itself makes, per watched ISIN:
https://api.live.deutsche-boerse.com/v1/data/price_information/single?isin={ISIN}&mic=XFRA
https://api.live.deutsche-boerse.com/v1/data/quote_box/single?isin={ISIN}&mic=XFRA (only when listed)

- Watched ISINs: market_watch.watched_isins (instrument ISINs, identifiers facts, US ISINs of CUSIPs).
  An ISIN Frankfurt does not list (HTTP 404) is re-checked after NOT_LISTED_RECHECK_DAYS.
- price_obs per instrument carrying the ISIN: price = last fixing (percent of par), or the bid/ask mid
  when there is no last; obs_at = the fixing (or quote) time, obs_date its Frankfurt date; volume = the
  day's nominal turnover when the fixing is from the day fetched. These are specialist quotes and
  fixings (turnover is usually 0), not trades. Instruments not quoted in percent are skipped.
- Fact `exchange_quote` on the instrument (tag R) per quote date: bid, ask, sizes, last, spread,
  nominal turnover, fixings count, previous close, day range. A side the specialist is not quoting
  comes back as 0 and is left out; after the 17:30 close (so in the scheduled run) Frankfurt shows no
  bid or ask at all (seen 2026-10-21), so bid/ask appear only in runs during trading hours.
- source_url is the bond's page (https://live.deutsche-boerse.com/anleihe/<isin>); the exact JSON is
  each row's document.
- Cadence: weekdays 18:05 Frankfurt time (trading ends 17:30). `python -m marden collect boerse_frankfurt`.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from dateutil import tz

from marden import cli
from marden.collectors.market_http import Challenged, MarketFetcher
from marden.collectors.market_watch import isin_valid, watched_isins
from marden.collectors.rates_common import ParseError, RunStats, collect, print_stats
from marden.core import db, http, licensing
from marden.core.db import Conn
from marden.core.facts import FactWriter
from marden.core.http import FetchError, fetch_document
from marden.core.queue import app
from marden.core.records import PriceWriter
from marden.core.sources import declare_source

SOURCE_ID = "boerse_frankfurt"
SOURCE_NAME = licensing.BOERSE_FRANKFURT
PRODUCER = f"collector:{SOURCE_ID}"
HOST = "api.live.deutsche-boerse.com"
MIC = "XFRA"
PRICE_URL = f"https://{HOST}/v1/data/price_information/single?isin={{isin}}&mic={MIC}"
QUOTE_URL = f"https://{HOST}/v1/data/quote_box/single?isin={{isin}}&mic={MIC}"
PAGE_URL = "https://live.deutsche-boerse.com/anleihe/{isin_lower}"
QUOTE_FIELD = "exchange_quote"
NOT_LISTED_RECHECK_DAYS = 7
BERLIN = tz.gettz("Europe/Berlin")
RUN_AT_BERLIN = (18, 5)
CRON_UTC = "5 16,17 * * 1-5"  # 18:05 Frankfurt is 16:05 UTC (CEST) or 17:05 UTC (CET); due_berlin keeps one
PRICE_RANGE = (Decimal(0), Decimal(1000))  # percent of par; outside means the shape changed

TERMS_URL = "https://live.deutsche-boerse.com/disclaimer"
TERMS_NOTE = (
    "UNLICENSED for commercial use; collected by decision of the business owner, who was told these terms "
    "and accepts the risk (docs/methods/licensing.md; licence flag in marden.core.licensing). Checked "
    f"2026-10-21: {TERMS_URL}, \"Download Price History\": \"Any access to and use of the FWB-Data is subject "
    "to a licence which is only granted free of charge if the FWB-Data User does not commercialize the "
    "FWB-Data. In particular, a commercialization of FWB-Data takes place in the following events (and a "
    "licence fee applies): FWB-Data User onward disseminates FWB-Data to third parties for a fee (including a "
    "general fee for accessing its services); FWB-Data User creates and offers value-added services using "
    "FWB-Data which are then provided to third parties, trading venues, APAs and/or CTPs. The FWB-Data User "
    "acknowledges and accepts that in the event a commercialization takes place a separate fee liable licence "
    "with DBAG is required and any usage without a licence in place is strictly prohibited.\" No clause on "
    "automated access was found; live.deutsche-boerse.com/robots.txt has no Disallow rules and "
    "api.live.deutsche-boerse.com/robots.txt is 404. Licence fees: Deutsche Börse MDDA price list (Börse "
    "Frankfurt Pre-Trade distribution EUR 1,115/month real-time, non-display from EUR 360/month). Access: "
    "declared User-Agent \"Marden research cody@marden.ai\", 1 request/second plus jitter, once per weekday "
    "after 17:45 CET; any challenge, login wall, 401/403 or repeated 429 is recorded here and the run stops "
    "(no evasion). The data are specialist quotes and fixings, usually with zero turnover, not trades."
)

SOURCE = declare_source(SOURCE_ID, SOURCE_NAME, "api", f"https://{HOST}/v1/data/", 24 * 60, TERMS_NOTE)
http.set_rate_limit(HOST, 1.0)


def price_url(isin: str) -> str:
    return PRICE_URL.format(isin=isin)


def quote_url(isin: str) -> str:
    return QUOTE_URL.format(isin=isin)


def page_url(isin: str) -> str:
    return PAGE_URL.format(isin_lower=isin.lower())


def due_berlin(timestamp: int | None, hour: int, minute: int) -> bool:
    """True when a periodic run at `timestamp` (UTC epoch) is at hour:minute Frankfurt time."""
    if timestamp is None:
        return True
    local = datetime.fromtimestamp(timestamp, UTC).astimezone(BERLIN)
    return (local.hour, local.minute) == (hour, minute)


# ---------------------------------------------------------------- parse
@dataclass(frozen=True)
class Quote:
    isin: str
    last: Decimal | None
    last_at: datetime | None
    bid: Decimal | None
    ask: Decimal | None
    quote_at: datetime | None
    in_percent: bool
    detail: dict[str, Any]  # the exchange_quote fact value

    @property
    def mid(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return ((self.bid + self.ask) / 2).quantize(Decimal("0.0001")).normalize()

    def observation(self) -> tuple[Decimal, datetime] | None:
        """(price, time) for price_obs: the last fixing, else the bid/ask mid."""
        if self.last is not None and self.last_at is not None:
            return self.last, self.last_at
        if self.mid is not None and self.quote_at is not None:
            return self.mid, self.quote_at
        return None


def _json(data: bytes, what: str) -> dict[str, Any] | None:
    if not data.strip():
        return None
    try:
        payload = json.loads(data, parse_float=Decimal)
    except ValueError as exc:
        raise ParseError(f"{what} is not JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise ParseError(f"{what} is not a JSON object")
    return payload


def _num(payload: dict[str, Any], key: str) -> Decimal | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Decimal | int):
        raise ParseError(f"{key} is not a number: {value!r}")
    return Decimal(value).normalize()


def _price(payload: dict[str, Any], key: str) -> Decimal | None:
    value = _num(payload, key)
    if value is not None and not PRICE_RANGE[0] < value < PRICE_RANGE[1]:
        raise ParseError(f"{key} {value} is outside {PRICE_RANGE[0]}..{PRICE_RANGE[1]} (percent of par)")
    return value


def _time(payload: dict[str, Any], key: str) -> datetime | None:
    value = payload.get(key)
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ParseError(f"{key} is not an ISO timestamp: {value!r}") from None
    if when.tzinfo is None:
        raise ParseError(f"{key} has no time zone: {value!r}")
    return when


def parse_quote(isin: str, price_info: bytes, quote_box: bytes | None) -> Quote | None:
    """The quote from the two responses; None when Frankfurt reports no data for the ISIN."""
    pi = _json(price_info, "price_information")
    if pi is None or pi.get("status") == 404:
        return None
    if str(pi.get("isin") or "").upper() != isin:
        raise ParseError(f"price_information is for {pi.get('isin')!r}, not {isin}")
    if pi.get("mic") not in (None, MIC):
        raise ParseError(f"price_information is for venue {pi.get('mic')!r}, not {MIC}")
    qb = _json(quote_box, "quote_box") if quote_box is not None else None
    if qb is not None and qb.get("isin") and str(qb["isin"]).upper() != isin:
        raise ParseError(f"quote_box is for {qb.get('isin')!r}, not {isin}")
    qb = qb or {}
    in_percent = pi.get("tradedInPercent") is True
    pricer = _price if in_percent else _num
    last, last_at = pricer(pi, "lastPrice"), _time(pi, "timestampLastPrice")

    def side(limit: str, size: str) -> tuple[Decimal | None, Decimal | None]:
        # A side the specialist is not quoting comes back as limit 0, size 0: no quote, not a price of 0.
        if _num(qb, limit) == 0:
            return None, None
        return pricer(qb, limit), _num(qb, size)

    (bid, bid_size), (ask, ask_size) = side("bidLimit", "bidSize"), side("askLimit", "askSize")
    quote_at = _time(qb, "timestamp")
    currency = pi.get("currency")
    detail = {
        "mic": MIC, "last": last, "last_at": last_at.isoformat() if last_at else None,
        "bid": bid, "ask": ask, "bid_size": bid_size, "ask_size": ask_size,
        "quote_at": quote_at.isoformat() if quote_at else None,
        "spread": _num(qb, "spreadAbsolute") if bid is not None and ask is not None else None,
        "prev_close": pricer(pi, "closingPricePrevTradingDay"), "day_high": pricer(pi, "dayHigh"),
        "day_low": pricer(pi, "dayLow"), "price_fixings": _num(pi, "priceFixings"),
        # turnoverInEur is left out: Deutsche Börse re-converts it at live FX, so it moves after the close.
        "turnover_nominal": _num(pi, "turnoverNominal"),
        "instrument_status": qb.get("instrumentStatus"), "traded_in_percent": in_percent,
        "currency": currency.get("originalValue") if isinstance(currency, dict) else currency,
    }
    return Quote(isin, last, last_at, bid, ask, quote_at, in_percent, {k: v for k, v in detail.items()
                                                                        if v is not None})


# ---------------------------------------------------------------- write
def write_quote(
    conn: Conn, quote: Quote, instrument_ids: list[str], *, fetched_on: date, producer: str,
    price_doc: int | None, quote_doc: int | None, stats: RunStats,
) -> int:
    """price_obs and the exchange_quote fact for each instrument. Returns rows created."""
    if not quote.in_percent:
        stats.extra.setdefault("not_percent_of_par", []).append(quote.isin)
        return 0
    prices, facts = PriceWriter(conn), FactWriter(conn)
    url = page_url(quote.isin)
    obs = quote.observation()
    created = 0
    for iid in instrument_ids:
        if obs is not None:
            price, at = obs
            day = at.astimezone(BERLIN).date()
            volume = quote.detail.get("turnover_nominal") if day == fetched_on and quote.last is not None else None
            res = prices.add(iid, day, "exchange", price, tag="R", source_name=SOURCE_NAME, source_url=url,
                             obs_at=at, volume=volume, document_id=price_doc)
            stats.extra["price_obs_seen"] = stats.extra.get("price_obs_seen", 0) + 1
            if res.created:
                created += 1
                stats.extra["price_obs_new"] = stats.extra.get("price_obs_new", 0) + 1
        when = quote.quote_at or quote.last_at
        if when is None:
            continue
        res = facts.add("instrument", iid, QUOTE_FIELD, quote.detail, tag="R", source_name=SOURCE_NAME,
                        source_url=url, valid_from=when.astimezone(BERLIN).date(), producer=producer,
                        status="published", unit="pct_of_par", document_id=quote_doc or price_doc)
        stats.facts_seen += 1
        if res.created:
            created += 1
            stats.facts_new += 1
    return created


# ---------------------------------------------------------------- run
def recently_not_listed(today: date) -> dict[str, str]:
    """ISIN -> date last found not listed, from the latest run that checked listings (a run that failed
    part-way keeps what it learned), within the re-check window."""
    with db.transaction() as conn:
        row = conn.execute(
            """SELECT meta FROM source_run WHERE source_id = %s AND meta ? 'not_listed'
               ORDER BY started_at DESC, id DESC LIMIT 1""",
            (SOURCE_ID,),
        ).fetchone()
    seen = (row["meta"] or {}).get("not_listed", {}) if row else {}
    cutoff = today - timedelta(days=NOT_LISTED_RECHECK_DAYS)
    return {i: d for i, d in seen.items() if isinstance(d, str) and date.fromisoformat(d) > cutoff}


def collect_isin(isin: str, iids: list[str], today: date, fetcher: MarketFetcher, stats: RunStats) -> bool:
    """Fetch and store one ISIN. Returns False when Frankfurt does not list it."""
    meta = {"isin": isin, "mic": MIC, "instrument_ids": iids}
    try:
        price_doc = fetch_document(SOURCE_ID, price_url(isin), doc_type="fwb_price_information", external_id=isin,
                                   title=f"Börse Frankfurt price information {isin}", meta=meta, fetcher=fetcher)
    except Challenged:
        raise
    except FetchError as exc:
        if exc.status == 404:
            return False
        raise
    stats.document(price_doc)
    if parse_quote(isin, price_doc.read(), None) is None:
        return False
    quote_doc = fetch_document(SOURCE_ID, quote_url(isin), doc_type="fwb_quote_box", external_id=isin,
                               title=f"Börse Frankfurt quote box {isin}", meta=meta, fetcher=fetcher)
    stats.document(quote_doc)
    quote = parse_quote(isin, price_doc.read(), quote_doc.read())
    if quote is None:
        return False
    with db.transaction() as conn:
        write_quote(conn, quote, iids, fetched_on=today, producer=PRODUCER, price_doc=price_doc.id,
                    quote_doc=quote_doc.id if quote_doc.bytes else None, stats=stats)
    if quote.observation() is None:
        stats.extra.setdefault("listed_without_price", []).append(isin)
    else:
        stats.extra.setdefault("with_data", []).append(isin)
    return True


def run(
    *, isins: list[str] | None = None, recheck_all: bool = False, today: date | None = None,
    fetcher: MarketFetcher | None = None,
) -> dict[str, Any]:
    """One collection run over the watched ISINs (or `isins`). Returns the run stats."""
    today = today or datetime.now(BERLIN).date()
    own = fetcher is None
    market = fetcher or MarketFetcher()

    def body(stats: RunStats) -> None:
        with db.transaction() as conn:
            universe = watched_isins(conn)
        if isins:
            wanted = {i.strip().upper() for i in isins}
            bad = sorted(i for i in wanted if not isin_valid(i))
            if bad:
                raise ParseError(f"not valid ISINs: {', '.join(bad)}")
            universe = {i: universe.get(i, []) for i in sorted(wanted)}
        known = recently_not_listed(today)
        skip = {} if (recheck_all or isins) else known
        not_listed: dict[str, str] = dict(known)  # carried forward; this run's answers update it
        stats.extra.update(watched=len(universe), skipped_recently_not_listed=len(skip), not_listed=not_listed,
                           price_obs_seen=0, price_obs_new=0)
        try:
            for isin, iids in universe.items():
                if isin in skip:
                    continue
                if not iids:
                    stats.extra.setdefault("no_instrument", []).append(isin)
                    continue
                try:
                    listed = collect_isin(isin, iids, today, market, stats)
                except ParseError as exc:  # this ISIN's response changed shape: the others still load
                    stats.extra.setdefault("parse_errors", {})[isin] = str(exc)
                    continue
                if listed:
                    not_listed.pop(isin, None)
                else:
                    not_listed[isin] = today.isoformat()
            errors = stats.extra.get("parse_errors") or {}
            if errors:  # recorded on the source as a failed run; what parsed is kept
                raise ParseError(f"{len(errors)} ISIN responses did not parse, e.g. {next(iter(errors.items()))}")
        finally:
            stats.extra["listed"] = len(stats.extra.get("with_data", [])) + len(
                stats.extra.get("listed_without_price", []))
            stats.extra["requests"] = market.requests

    try:
        return collect(SOURCE, body)
    finally:
        if own:
            market.close()


# ---------------------------------------------------------------- schedule and CLI
@app.periodic(cron=CRON_UTC, periodic_id="weekdays_1805_berlin")
@app.task(name="collect.boerse_frankfurt", queue="collect", queueing_lock="collect.boerse_frankfurt",
          lock="collect.boerse_frankfurt")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    if not due_berlin(timestamp, *RUN_AT_BERLIN):
        return {"skipped": "not 18:05 Frankfurt time"}
    return run()


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--isin", action="append", dest="isins", help="only this ISIN (repeatable)")
    p.add_argument("--recheck-all", action="store_true", help="also re-check ISINs recently found not listed")


@cli.action("collect", SOURCE_ID, help="Börse Frankfurt bond quotes -> price_obs 'exchange' + exchange_quote facts",
            configure=_args)
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run(isins=args.isins, recheck_all=args.recheck_all))
