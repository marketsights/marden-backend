"""Price and yield moves over fixed windows (moves model): which prices a window compares, and what drove it.

Windows end on the as-of day and start on: the previous business day (1D), 7 days before (1W), one month
before (1M), the last day of the previous quarter (QTD) and Dec 31 of the previous year (YTD).

A window compares the instrument's traded price when it traded inside the window (TRACE, the day's average
print), else its Marden model price. The start price is the last price on or before the window's start;
when the history begins later, the first price inside the window, and the window says so (`partial`, and
`start` is the real start date). A window never reuses a price from before its start as if it were current:
when the last price is on or before the start, there is no move to show and `why` says so.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from dateutil.relativedelta import relativedelta

WINDOWS = ("1D", "1W", "1M", "QTD", "YTD")
LABELS = {"1D": "1 day", "1W": "1 week", "1M": "1 month", "QTD": "Quarter to date", "YTD": "Year to date"}
NO_MOVE_PTS = 0.05  # below this a move is shown as no move (prices are kept to three decimals)
GAP_DAYS = {"1D": 3, "1W": 3, "1M": 5, "QTD": 7, "YTD": 10}  # a start price older than this is called out


def when(d: date) -> str:
    return f"{d:%b} {d.day}"


def nominal_start(key: str, day: date, previous_business_day: date | None = None) -> date:
    if key == "1D":
        return previous_business_day or _weekday_before(day)
    if key == "1W":
        return day - timedelta(days=7)
    if key == "1M":
        return day - relativedelta(months=1)
    if key == "QTD":
        return date(day.year, 3 * ((day.month - 1) // 3) + 1, 1) - timedelta(days=1)
    if key == "YTD":
        return date(day.year - 1, 12, 31)
    raise ValueError(f"unknown window {key!r}")


def _weekday_before(day: date) -> date:
    d = day - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


@dataclass(frozen=True)
class Point:
    day: date
    price: float
    basis: str  # 'trade' | 'model'
    tag: str
    source_name: str
    source_url: str | None = None
    prints: int = 1
    method: str | None = None  # the price model's method version behind a Marden price (never shown)

    def doc(self, y: dict[str, Any] | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"date": self.day.isoformat(), "price": round(self.price, 3), "tag": self.tag,
                               "sourceName": self.source_name, "basis": self.basis}
        if self.source_url:
            out["sourceUrl"] = self.source_url
        if self.basis == "trade":
            out["trades"] = self.prints
        y = y or {}
        out.update({"ytm": y.get("ytm"), "ytw": y.get("ytw"), "dmBp": y.get("dmBp")})
        return out


def pick(points: Sequence[Point], start: date, end: date) -> tuple[Point | None, Point | None, bool]:
    """(start point, end point, partial): the last point on or before `start`, else the first one after it
    (partial); the end point is the last on or before `end`. Points oldest first."""
    upto = [p for p in points if p.day <= end]
    if not upto:
        return None, None, False
    last = upto[-1]
    before = [p for p in upto if p.day <= start]
    if before:
        return before[-1], last, False
    inside = [p for p in upto if start < p.day < last.day]
    return (inside[0], last, True) if inside else (None, last, False)


def traded_in(trades: Sequence[Point], start: date, end: date) -> bool:
    return any(start < p.day <= end for p in trades)


@dataclass
class Window:
    key: str
    nominal: date
    end: date
    basis: str | None = None
    frm: Point | None = None
    to: Point | None = None
    partial: bool = False
    why: str | None = None
    note: str | None = None

    @property
    def change(self) -> float | None:
        return None if self.frm is None or self.to is None or self.why else round(self.to.price - self.frm.price, 3)


def window(key: str, day: date, prev_bd: date | None, model: Sequence[Point], trades: Sequence[Point]) -> Window:
    """Which two prices the window compares, or why it has none."""
    start = nominal_start(key, day, prev_bd)
    w = Window(key, start, day)
    use_trades = traded_in(trades, start, day)
    points = trades if use_trades else model
    w.basis = "trade" if use_trades else "model"
    w.frm, w.to, w.partial = pick(points, start, day)
    what = "trade" if use_trades else "Marden price"
    if w.to is None:
        w.why = (f"No trade in this window (the last was {when(trades[-1].day)}) and no Marden price: the price "
                 "model does not price it." if trades else "No Marden price or trade yet.")
    elif w.to.day <= start:
        w.why = f"No {what} since {when(w.to.day)}, before this window starts ({when(start)})."
        w.frm = None
    elif w.frm is None:
        w.why = f"Only one {what} in this window ({when(w.to.day)}): nothing to compare it with."
    elif w.partial:
        w.note = (f"Starts {when(w.frm.day)}: the first {what} in this window "
                  f"(Marden prices are on record from {when(model[0].day)})" if not use_trades and model else
                  f"Starts {when(w.frm.day)}: the first {what} in this window")
    elif (start - w.frm.day).days > GAP_DAYS[key]:
        w.note = f"Starts {when(w.frm.day)}: no {what} between then and {when(start)}"
    if not w.why and w.frm and w.to and not use_trades and w.frm.method and w.to.method and w.frm.method != w.to.method:
        w.note = ((w.note + ". ") if w.note else "") + (
            "The price model's method was updated between these two prices, so part of this move is the update, "
            "not new information")
    return w


def main_driver(total: float | None, events: Sequence[dict[str, Any]], rates: float | None,
                basis: str | None) -> dict[str, Any]:
    """The component with the largest share of the move: an event (each priced change on its own), rates, or
    spreads (the rest of the model move); a traded move is driven by the trades. {kind, pts, key?}."""
    if total is None:
        return {"kind": "none", "pts": None}
    if basis == "trade":
        return {"kind": "trade", "pts": total}
    if abs(total) < NO_MOVE_PTS:
        return {"kind": "none", "pts": total}
    parts: list[dict[str, Any]] = [{"kind": "event", "pts": e["pts"], "key": e["key"]} for e in events
                                   if isinstance(e.get("pts"), int | float)]
    explained = sum(p["pts"] for p in parts)
    if rates is not None:
        parts.append({"kind": "rates", "pts": rates})
        explained += rates
    parts.append({"kind": "spreads", "pts": round(total - explained, 3)})
    return max(parts, key=lambda p: abs(p["pts"]))
