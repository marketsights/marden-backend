"""InstrumentRow.lastTrade: a traded bond whose Marden price has not moved since the last run says so
('Unchanged since Oct 20: no trade since Oct 20') instead of silently repeating the price."""

from __future__ import annotations

from datetime import date

from marden.readmodels.views.last_trade import last_trade


def _row(day: str, price: float, kind: str = "trade") -> dict:
    return {"kind": kind, "obs_date": date.fromisoformat(day), "price": price, "tag": "F ",
            "source_name": "FINRA TRACE (finra.org)", "source_url": "https://www.finra.org/x"}


def test_no_trade_since_and_unchanged_price_are_said_in_plain_words():
    observed = [_row("2026-10-19", 85.1), _row("2026-10-20", 85.6), _row("2026-10-21", 86.0, "dealer")]
    series = [_row("2026-10-20", 85.588, "marden"), _row("2026-10-21", 85.588, "marden"),
              _row("2026-10-22", 85.588, "marden")]
    got = last_trade(observed, series)
    assert got == {"date": "2026-10-20", "price": 85.6, "tag": "F", "sourceName": "FINRA TRACE (finra.org)",
                   "noTradeSince": True, "note": "Unchanged since Oct 21: no trade since Oct 20",
                   "sourceUrl": "https://www.finra.org/x"}
    moved = series[:-1] + [_row("2026-10-22", 85.2, "marden")]
    assert last_trade(observed, moved)["note"] == "Last trade Oct 20; no trade since"


def test_a_trade_on_the_price_day_or_none_at_all():
    today = [_row("2026-10-22", 85.0)]
    assert last_trade(today, [_row("2026-10-22", 85.0, "marden")])["noTradeSince"] is False
    assert last_trade(today, [_row("2026-10-22", 85.0, "marden")])["note"] is None
    assert last_trade([_row("2026-10-22", 85.0, "dealer")], []) is None
