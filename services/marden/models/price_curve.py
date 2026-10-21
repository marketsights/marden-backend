"""Treasury discount curves for the price model (docs/methods/pricing-v0.1.md "The model": D(t) on the
Treasury curve; "Bond-implied CDS" step 1: "that day's Treasury curve").

Input: the U.S. Treasury daily par yield curve, `series` facts `ust_par_<tenor>` (field `value`, percent,
one fact per curve date). A curve for day d is the latest curve dated on or before d (a holiday or a
weekend uses the previous business day's curve); a curve older than MAX_STALE_DAYS is missing.

Bootstrap: bill tenors (<= 1y) are bond-equivalent yields, taken as semiannually compounded zero rates;
coupon tenors (>= 2y) are par yields of semiannual bonds. Par yields are interpolated linearly to every
half year out to 30y and stripped into discount factors; between nodes the continuously compounded zero
rate is linear in time, beyond 30y it is flat. SOFR (`series` `sofr`, NY Fed) is read alongside for
floating coupons.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np

from marden.core.db import Conn
from marden.models import pit

TENORS: dict[str, float] = {
    "1m": 1 / 12, "6w": 1.5 / 12, "2m": 2 / 12, "3m": 0.25, "4m": 4 / 12, "6m": 0.5, "1y": 1.0, "2y": 2.0,
    "3y": 3.0, "5y": 5.0, "7y": 7.0, "10y": 10.0, "20y": 20.0, "30y": 30.0,
}
SERIES = {f"ust_par_{k}": k for k in TENORS}
MAX_STALE_DAYS = 7
SOFR_SERIES = "sofr"


def year_frac(start: date, end: date) -> float:
    """Act/365.25 year fraction (the model's time axis)."""
    return (end - start).days / 365.25


@dataclass(frozen=True)
class Curve:
    """One day's Treasury zero curve. `df(t)` discount factor at t years, `zero(t)` continuous rate."""

    day: date
    par: dict[str, float]  # tenor -> par yield (percent)
    times: np.ndarray = field(repr=False)
    zeros: np.ndarray = field(repr=False)

    def zero(self, t: float | np.ndarray) -> Any:
        t_arr = np.asarray(t, dtype=float)
        out = np.interp(t_arr, self.times, self.zeros, left=self.zeros[0], right=self.zeros[-1])
        return float(out) if out.ndim == 0 else out

    def df(self, t: float | np.ndarray) -> Any:
        t_arr = np.maximum(np.asarray(t, dtype=float), 0.0)
        out = np.exp(-np.asarray(self.zero(t_arr)) * t_arr)
        return float(out) if np.ndim(out) == 0 else out

    def forward(self, t0: float, t1: float) -> float:
        """Simple forward rate between t0 and t1 (decimal)."""
        if t1 <= t0:
            return self.zero(max(t0, 1e-6))
        return (self.df(t0) / self.df(t1) - 1.0) / (t1 - t0)

    def par_yield(self, years: float) -> float:
        """Par yield (percent) at a maturity, linear in the published tenors."""
        xs = sorted((TENORS[k], v) for k, v in self.par.items())
        return float(np.interp(years, [x for x, _ in xs], [v for _, v in xs]))


def bootstrap(day: date, par: dict[str, float]) -> Curve:
    """Strip a par curve (tenor -> percent) into zero rates."""
    pts = sorted((TENORS[k], v / 100.0) for k, v in par.items() if k in TENORS and v is not None)
    if len(pts) < 4:
        raise ValueError(f"Treasury curve {day}: only {len(pts)} tenors")
    px = [p for p, _ in pts]
    py = [y for _, y in pts]
    times: list[float] = []
    zeros: list[float] = []
    for t, y in pts:  # bills: semiannual zero (bond-equivalent) rates
        if t <= 1.0:
            times.append(t)
            zeros.append(2.0 * math.log(1.0 + y / 2.0))
    dfs: dict[float, float] = {}
    grid = [0.5 * k for k in range(1, 61)]
    for t in grid:
        c = float(np.interp(t, px, py))
        if t <= 1.0:
            dfs[t] = math.exp(-float(np.interp(t, times, zeros)) * t)
            continue
        annuity = sum(dfs[s] for s in grid if s < t)
        dfs[t] = (1.0 - c / 2.0 * annuity) / (1.0 + c / 2.0)
        times.append(t)
        zeros.append(-math.log(dfs[t]) / t)
    order = np.argsort(times)
    return Curve(day, dict(par), np.asarray(times)[order], np.asarray(zeros)[order])


@dataclass
class CurveBook:
    """Every Treasury curve and SOFR fixing public on or before a run's as-of day."""

    as_of: date
    curves: dict[date, dict[str, float]]
    sofr: dict[date, float]
    _days: list[date] = field(default_factory=list)
    _sofr_days: list[date] = field(default_factory=list)
    _cache: dict[date, Curve] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._days = sorted(d for d, p in self.curves.items() if len(p) >= 8)
        self._sofr_days = sorted(self.sofr)

    def curve_day(self, day: date) -> date | None:
        i = bisect.bisect_right(self._days, day)
        if i == 0:
            return None
        found = self._days[i - 1]
        return found if (day - found).days <= MAX_STALE_DAYS else None

    def curve(self, day: date) -> Curve | None:
        found = self.curve_day(day)
        if found is None:
            return None
        if found not in self._cache:
            self._cache[found] = bootstrap(found, self.curves[found])
        return self._cache[found]

    def sofr_on(self, day: date) -> float | None:
        """SOFR (percent) published on or before `day` (within MAX_STALE_DAYS)."""
        i = bisect.bisect_right(self._sofr_days, day)
        if i == 0:
            return None
        found = self._sofr_days[i - 1]
        return self.sofr[found] if (day - found).days <= MAX_STALE_DAYS else None

    def business_days(self, start: date, end: date) -> list[date]:
        """Curve dates in [start, end] (the Treasury's business days)."""
        lo = bisect.bisect_left(self._days, start)
        hi = bisect.bisect_right(self._days, end)
        return self._days[lo:hi]

    def first_day(self) -> date | None:
        return self._days[0] if self._days else None


def load_curves(conn: Conn, as_of: date, since: date | None = None) -> CurveBook:
    """Treasury par curves and SOFR public by `as_of` (point in time: a curve is public on its date)."""
    rows = pit.fact_rows(conn, as_of, "series", [*SERIES, SOFR_SERIES], ["value"])
    curves: dict[date, dict[str, float]] = {}
    sofr: dict[date, float] = {}
    for r in rows:
        day: date = r["valid_from"]
        if since is not None and day < since:
            continue
        value = r["value"]
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        if r["subject_id"] == SOFR_SERIES:
            sofr.setdefault(day, float(value))
        else:
            curves.setdefault(day, {}).setdefault(SERIES[r["subject_id"]], float(value))
    return CurveBook(as_of, curves, sofr)


def weekdays(start: date, end: date) -> list[date]:
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def nearest(days: Sequence[date], target: date) -> date | None:
    """The day in a sorted list closest to `target` (ties: the earlier)."""
    if not days:
        return None
    i = bisect.bisect_left(days, target)
    best = [d for d in (days[i - 1] if i > 0 else None, days[i] if i < len(days) else None) if d is not None]
    return min(best, key=lambda d: (abs((d - target).days), d))
