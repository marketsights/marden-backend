"""BondTerminal bond quotes -> price_obs kind 'exchange' (tag R, 'BondTerminal').

API: https://bondterminal.com/api/v1 with `Authorization: Bearer $BT_API_KEY` (server-side only; the key is
never logged or stored). POST /bonds/batch takes up to 10 identifiers per call (Basic key) and costs one read
unit per identifier. Basic limits: 200 read units a day, 10 requests a minute, at most 100 different bonds per
30 days. This collector asks for every watched ISIN three times a weekday (10:35, 13:35 and 17:35 New York), in
batches of 10, one batch every 7 seconds, stalest quote first (curated EX-* instruments win ties). When
BondTerminal refuses for quota (429), the run stops and keeps the batches already stored.

Watched ISINs come from market_watch.watched_isins, curated instruments (EX-*) first. Each row's raw JSON is
stored as a document before it is parsed. `python -m marden collect bondterminal [--probe] [--isin I]`:
--probe prints what one batch returns and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from marden import cli
from marden.collectors.market_watch import watched_isins
from marden.collectors.rates_common import RunStats, collect, print_stats
from marden.core import db, documents
from marden.core.queue import app
from marden.core.records import PriceWriter
from marden.core.sources import declare_source

SOURCE_ID = "bondterminal"
SOURCE_NAME = "BondTerminal"
BASE = "https://bondterminal.com/api/v1"
BATCH = 10
PAUSE_SECONDS = 7.0  # 10 requests a minute on a Basic key
TERMS_NOTE = ("BondTerminal API, Basic key, personal use by the account holder: up to 100 bonds per 30 days, "
              "200 read units a day; keys are server-side only.")
SOURCE = declare_source(SOURCE_ID, SOURCE_NAME, "api", BASE, 24 * 60, TERMS_NOTE)
CRON_UTC = "35 14,17,21 * * 1-5"  # 10:35, 13:35, 17:35 New York (EDT)


def _key() -> str:
    key = os.environ.get("BT_API_KEY", "").strip()
    if not key:
        raise RuntimeError("BT_API_KEY is not set on this service")
    return key


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for k in ("data", "rows", "results", "bonds", "items"):
            if isinstance(payload.get(k), list):
                return [r for r in payload[k] if isinstance(r, dict)]
    return []


def _dig(row: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        cur: Any = row
        for part in path.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur not in (None, ""):
            return cur
    return None


def _dec(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def parse_row(row: dict[str, Any]) -> tuple[str, Decimal, datetime | None, Decimal | None] | None:
    """(isin, clean price % of par, as-of time, yield to worst %) or None when the row has no price."""
    isin = str(_dig(row, "isin") or "").upper()
    price = _dec(_dig(row, "price", "lastPrice", "cleanPrice", "market.price", "pricing.cleanPrice"))
    if not isin or price is None or not (Decimal(1) < price < Decimal(300)):
        return None
    ts = _dig(row, "analyticsStatus.priceAsOf", "priceTimestamp", "priceDate", "market.timestamp", "asOf")
    at = None
    if isinstance(ts, str):
        try:
            at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            at = at if at.tzinfo else at.replace(tzinfo=UTC)
        except ValueError:
            at = None
    ytw = _dec(_dig(row, "ytw", "yields.ytw", "yieldToWorst"))
    return isin, price, at, ytw


def _batch(client: httpx.Client, isins: list[str]) -> tuple[Any, bytes]:
    r = client.post(f"{BASE}/bonds/batch", json={"identifiers": isins},
                    headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"})
    if r.status_code == 429:
        raise QuotaReached(r.text[:300])
    r.raise_for_status()
    return r.json(), r.content


class QuotaReached(RuntimeError):
    """BondTerminal answered 429: the key's daily units, per-minute rate or 30-day bond count is used up."""


def _last_quoted(conn: Any) -> dict[str, str]:
    """instrument id -> time of its latest BondTerminal quote (ISO text, sorts in time order)."""
    rows = conn.execute(
        "SELECT instrument_id, max(coalesce(obs_at, obs_date::timestamptz))::text FROM price_obs"
        " WHERE source_name = %s GROUP BY instrument_id", (SOURCE_NAME,)).fetchall()
    return {r[0]: r[1] for r in rows}


def _universe(conn: Any, only: list[str] | None) -> dict[str, list[str]]:
    watched = watched_isins(conn)
    if only:
        return {i: watched.get(i, []) for i in (s.upper() for s in only)}
    last = _last_quoted(conn)

    def key(kv: tuple[str, list[str]]) -> tuple[str, bool, str]:
        seen = max((last.get(i, "") for i in kv[1]), default="")  # never quoted ("") first
        return seen, not any(x.startswith("EX-") for x in kv[1]), kv[0]

    return dict(sorted(watched.items(), key=key))


def run(*, isins: list[str] | None = None, probe: bool = False) -> dict[str, Any]:
    def body(stats: RunStats) -> None:
        with db.transaction() as conn:
            universe = _universe(conn, isins)
        names = list(universe)
        stats.extra["isins_asked"] = len(names)
        with httpx.Client(timeout=60) as client:
            for n, start in enumerate(range(0, len(names), BATCH)):
                chunk = names[start:start + BATCH]
                if n:
                    time.sleep(PAUSE_SECONDS)
                try:
                    payload, raw = _batch(client, chunk)
                except QuotaReached as exc:
                    stats.extra["stopped_quota"] = f"after {start} of {len(names)} ISINs: {exc}"
                    return
                rows = _rows(payload)
                if probe:
                    keys = list(payload)[:20] if isinstance(payload, dict) else "list"
                    first = rows[0] if rows else payload
                    sample = {"asked": chunk, "top_keys": keys, "rows": len(rows), "first_row": first}
                    print(json.dumps(sample, default=str)[:4000])
                    return
                with db.transaction() as conn:
                    doc = documents.record_document(SOURCE_ID, f"{BASE}/bonds/batch#{','.join(chunk)}", raw,
                                                    content_type="application/json", conn=conn).id
                    writer = PriceWriter(conn)
                    for row in rows:
                        parsed = parse_row(row)
                        if parsed is None:
                            stats.extra["rows_without_price"] = stats.extra.get("rows_without_price", 0) + 1
                            continue
                        isin, price, at, ytw = parsed
                        day = at.date() if at else date.today()
                        for iid in universe.get(isin, []):
                            res = writer.add(iid, day, "exchange", price, tag="R", source_name=SOURCE_NAME,
                                             source_url=f"https://bondterminal.com/bonds/{isin}", obs_at=at,
                                             yield_pct=ytw, document_id=doc)
                            stats.extra["price_obs_seen"] = stats.extra.get("price_obs_seen", 0) + 1
                            if res.created:
                                stats.extra["price_obs_new"] = stats.extra.get("price_obs_new", 0) + 1

    return collect(SOURCE, body)


@app.periodic(cron=CRON_UTC, periodic_id="weekdays_3x_new_york")
@app.task(name="collect.bondterminal", queue="collect", queueing_lock="collect.bondterminal")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--isin", action="append", dest="isins", help="only this ISIN (repeatable)")
    p.add_argument("--probe", action="store_true", help="fetch one batch and print it; write nothing")


@cli.action("collect", SOURCE_ID, help="BondTerminal bond quotes -> price_obs 'exchange' (R)", configure=_args)
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run(isins=args.isins, probe=args.probe))
