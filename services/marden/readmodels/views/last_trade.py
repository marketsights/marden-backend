"""InstrumentRow.lastTrade: the latest trade print (price_obs kind 'trade') behind a traded instrument's price,
so a Marden price that has not moved since the last run can say why ('Unchanged since Oct 20: no trade since
Oct 20') instead of silently repeating. None for an instrument that never traded."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from marden.core.db import Row
from marden.readmodels.views.instrument_inputs import iso, num

UNCHANGED_PTS = 0.0005  # the stored price has three decimals


def _short(d: date) -> str:
    return f"{d:%b} {d.day}"


def last_trade(observed: Sequence[Row], series: Sequence[Row]) -> dict[str, Any] | None:
    """`observed`: price_obs rows oldest first (any kinds); `series`: the Marden daily price, one row a day."""
    trades = [r for r in observed if r["kind"] == "trade" and num(r["price"]) is not None]
    if not trades:
        return None
    last = trades[-1]
    day: date = last["obs_date"]
    priced = series[-1]["obs_date"] if series else None
    stale = priced is not None and day < priced
    note = None
    if stale:
        note = f"Last trade {_short(day)}; no trade since"
        if len(series) >= 2 and abs(float(series[-1]["price"]) - float(series[-2]["price"])) < UNCHANGED_PTS:
            note = f"Unchanged since {_short(series[-2]['obs_date'])}: no trade since {_short(day)}"
    out: dict[str, Any] = {"date": iso(day), "price": round(float(last["price"]), 3), "tag": str(last["tag"]).strip(),
                           "sourceName": last["source_name"] or "trade", "noTradeSince": stale, "note": note}
    if last.get("source_url"):
        out["sourceUrl"] = last["source_url"]
    return out
