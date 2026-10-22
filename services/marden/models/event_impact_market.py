"""The observed market move around an event (event impact model): an instrument's trades before and after it.

TRACE gives one end-of-day print per bond and day here, without the time the news broke, so the trades of the
event's own day are left out: `before` is the last trade day before the event, `after` the first trade day
after it (within AFTER_DAYS). The move is after minus before, in points, with the trades' own tag and source.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from marden.core.db import Conn
from marden.core.licensing import show_unlicensed_prices, unlicensed_source_names

AFTER_DAYS = 14
TRADE_KINDS = ("trade",)


@dataclass(frozen=True)
class TradeDay:
    day: date
    price: float  # the day's average print, clean, percent of par
    prints: int
    tag: str
    source_name: str
    source_url: str | None

    def doc(self) -> dict[str, Any]:
        out: dict[str, Any] = {"date": self.day.isoformat(), "price": round(self.price, 3), "trades": self.prints,
                               "tag": self.tag, "sourceName": self.source_name}
        if self.source_url:
            out["sourceUrl"] = self.source_url
        return out


def trade_days(conn: Conn, ids: Sequence[str], until: date) -> dict[str, list[TradeDay]]:
    """Each instrument's trade days on or before `until`, oldest first (the day's prints averaged)."""
    if not ids:
        return {}
    params: dict[str, Any] = {"ids": list(ids), "kinds": list(TRADE_KINDS), "until": until}
    sql = """SELECT instrument_id, obs_date, avg(price) AS price, count(*) AS n, min(tag) AS tag,
                    min(source_name) AS source_name, min(source_url) AS source_url
               FROM price_obs WHERE instrument_id = ANY(%(ids)s) AND kind = ANY(%(kinds)s) AND price IS NOT NULL
                AND obs_date <= %(until)s"""
    if not show_unlicensed_prices():
        sql += " AND source_name <> ALL(%(hidden)s)"
        params["hidden"] = unlicensed_source_names()
    sql += " GROUP BY instrument_id, obs_date ORDER BY instrument_id, obs_date"
    out: dict[str, list[TradeDay]] = {}
    for r in conn.execute(sql, params).fetchall():
        out.setdefault(r["instrument_id"], []).append(
            TradeDay(r["obs_date"], float(r["price"]), int(r["n"]), str(r["tag"]).strip(), r["source_name"],
                     r["source_url"]))
    return out


def around(days: Sequence[TradeDay], event: date) -> tuple[TradeDay | None, TradeDay | None]:
    """(last trade day before `event`, first trade day after it within AFTER_DAYS)."""
    before = next((d for d in reversed(days) if d.day < event), None)
    after = next((d for d in days if event < d.day <= event + timedelta(days=AFTER_DAYS)), None)
    return before, after


def market_move(iid: str, name: str, days: Sequence[TradeDay], event: date) -> dict[str, Any]:
    """MarketMove: the trades around `event` and the move between them; `why` says what is missing."""
    before, after = around(days, event)
    out: dict[str, Any] = {"instrumentId": iid, "name": name, "before": before.doc() if before else None,
                           "after": after.doc() if after else None, "changePts": None, "why": None}
    if not days:
        out["why"] = "No trades on record: it does not trade on TRACE."
    elif before is None:
        out["why"] = f"No trade before {event:%b} {event.day}."
    elif after is None:
        out["why"] = f"No trade in the {AFTER_DAYS} days after {event:%b} {event.day}."
    else:
        out["changePts"] = round(after.price - before.price, 3)
    return out
