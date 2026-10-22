"""Cash flows and the reduced-form price (docs/methods/pricing-v0.1.md "The model"):

    P = sum_t CF_t S(t) D(t) + sum_t R(t) N [S(t-1) - S(t)] D(t),   S(t) = exp(-int_0^t lambda(u) du)

per 100 of par, D(t) = Treasury discount factor x exp(-lp t) (lp = liquidity premium, 0 for bond-implied
intensities). The default leg sits on a monthly grid merged with the payment dates, so recovery is paid at
the end of the month of default. Observed prices are clean; the solver adds accrued interest.

Terms (from the instrument's facts): fixed coupons pay semiannually on dates counted back from maturity
(30/360 accrual); floating coupons pay quarterly at the projected index plus the margin (Act/360), the
index projected as the Treasury forward rate plus the SOFR - 3m Treasury basis on the pricing day. A
maturity given to the month falls on the 15th, a bare year on July 1 (`Terms.assumptions` says so).
Principal is bullet: amortization is not modelled in v0.1.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache

import numpy as np
from dateutil.relativedelta import relativedelta
from scipy.optimize import brentq

from marden.models.price_curve import Curve, year_frac

RECOVERY = 0.40  # bond-implied CDS (locked rule)
MAX_LAMBDA = 4.0


@dataclass(frozen=True)
class Terms:
    instrument_id: str
    maturity: date
    coupon_type: str  # 'fixed' | 'floating'
    coupon_pct: float | None = None  # fixed rate, percent
    margin_bp: float | None = None  # floating margin over SOFR
    frequency: int = 2
    assumptions: tuple[str, ...] = field(default=())

    @property
    def floating(self) -> bool:
        return self.coupon_type == "floating"


@lru_cache(maxsize=4096)
def _coupon_dates(maturity: date, months: int) -> tuple[date, ...]:
    """Every coupon date of a schedule counted back from maturity, 60 years deep, oldest first."""
    return tuple(sorted(maturity - relativedelta(months=months * k) for k in range(60 * 12 // months + 1)))


def schedule(terms: Terms, settle: date) -> list[date]:
    """Coupon dates after `settle`, counted back from maturity."""
    dates = _coupon_dates(terms.maturity, 12 // terms.frequency)
    return list(dates[bisect.bisect_right(dates, settle):])


def previous_coupon(terms: Terms, settle: date) -> date:
    """The last coupon date on or before `settle`."""
    dates = _coupon_dates(terms.maturity, 12 // terms.frequency)
    return dates[max(bisect.bisect_right(dates, settle) - 1, 0)]


def days_30_360(a: date, b: date) -> int:
    d1, d2 = min(a.day, 30), b.day
    if d1 == 30 and d2 == 31:
        d2 = 30
    return 360 * (b.year - a.year) + 30 * (b.month - a.month) + (d2 - d1)


@dataclass(frozen=True)
class Flows:
    times: np.ndarray  # years from settle
    amounts: np.ndarray  # per 100 par (last includes principal)
    accrued: float
    maturity_years: float
    curve_day: date | None = None  # the curve the grid's Treasury discount factors were taken from
    grid: np.ndarray | None = field(default=None, repr=False)
    tsy_df: np.ndarray | None = field(default=None, repr=False)


def flows(terms: Terms, curve: Curve, settle: date, sofr_pct: float | None) -> Flows:
    """Projected cash flows per 100 par from `settle` (a floating note needs SOFR)."""
    dates = schedule(terms, settle)
    if not dates:
        raise ValueError(f"{terms.instrument_id}: matured on {terms.maturity}")
    prev = previous_coupon(terms, settle)
    times = np.array([year_frac(settle, d) for d in dates])
    amounts = np.zeros(len(dates))
    if terms.floating:
        if sofr_pct is None or terms.margin_bp is None:
            raise ValueError(f"{terms.instrument_id}: floating coupon needs SOFR and the margin")
        basis = sofr_pct / 100.0 - curve.forward(0.0, 0.25)
        starts = [prev, *dates[:-1]]
        for i, (a, b) in enumerate(zip(starts, dates, strict=True)):
            t0, t1 = max(year_frac(settle, a), 0.0), year_frac(settle, b)
            rate = (sofr_pct / 100.0 if t0 <= 0.0 else curve.forward(t0, t1) + basis) + terms.margin_bp / 1e4
            amounts[i] = 100.0 * rate * (b - a).days / 360.0
        current_rate = sofr_pct / 100.0 + terms.margin_bp / 1e4
        accrued = 100.0 * current_rate * (settle - prev).days / 360.0
    else:
        if terms.coupon_pct is None:
            raise ValueError(f"{terms.instrument_id}: fixed coupon missing")
        amounts[:] = terms.coupon_pct / terms.frequency
        period = days_30_360(prev, dates[0]) or 1
        accrued = terms.coupon_pct / terms.frequency * days_30_360(prev, settle) / period
    amounts[-1] += 100.0
    base = Flows(times, amounts, accrued, float(times[-1]))
    grid = _grid(base)
    return Flows(times, amounts, accrued, float(times[-1]), curve.day, grid, np.asarray(curve.df(grid)))


HazardFn = Callable[[np.ndarray], np.ndarray]  # interval midpoints (years) -> intensity per year
RecoveryFn = Callable[[np.ndarray], np.ndarray]  # grid times (years) -> recovery share of par


def _grid(f: Flows) -> np.ndarray:
    monthly = np.arange(1, math.ceil(f.maturity_years * 12) + 1) / 12.0
    return np.unique(np.concatenate([[0.0], monthly[monthly < f.maturity_years], f.times]))


def dirty_price(f: Flows, curve: Curve, hazard: float | HazardFn, recovery: float | RecoveryFn = RECOVERY,
                lp: float = 0.0) -> float:
    """Reduced-form dirty price per 100 par."""
    cached = f.grid is not None and f.tsy_df is not None and f.curve_day == curve.day
    grid = f.grid if cached else _grid(f)
    assert grid is not None
    dt = np.diff(grid)
    mids = grid[:-1] + dt / 2.0
    lam = np.full_like(mids, float(hazard)) if not callable(hazard) else np.asarray(hazard(mids), dtype=float)
    cum = np.concatenate([[0.0], np.cumsum(lam * dt)])
    surv = np.exp(-cum)
    tsy = f.tsy_df if cached else np.asarray(curve.df(grid))
    disc = tsy * np.exp(-lp * grid)
    idx = np.searchsorted(grid, f.times)
    coupon_leg = float(np.sum(f.amounts * surv[idx] * disc[idx]))
    rec = np.full(len(grid), float(recovery)) if not callable(recovery) else np.asarray(recovery(grid), dtype=float)
    default_leg = float(np.sum(100.0 * rec[1:] * (surv[:-1] - surv[1:]) * disc[1:]))
    return coupon_leg + default_leg


def clean_price(f: Flows, curve: Curve, hazard: float | HazardFn, recovery: float | RecoveryFn = RECOVERY,
                lp: float = 0.0) -> float:
    return dirty_price(f, curve, hazard, recovery, lp) - f.accrued


@dataclass(frozen=True)
class Solved:
    value: float
    bound: str | None  # 'floor' when the price is above the default-free value, 'cap' when below any fit


def solve_flat_hazard(f: Flows, curve: Curve, clean: float, recovery: float = RECOVERY, lp: float = 0.0) -> Solved:
    """Flat intensity that reprices `clean` (bond-implied CDS step 1)."""
    target = clean + f.accrued

    def gap(lam: float) -> float:
        return dirty_price(f, curve, lam, recovery, lp) - target

    if gap(0.0) <= 0.0:
        return Solved(0.0, "floor")
    if gap(MAX_LAMBDA) >= 0.0:
        return Solved(MAX_LAMBDA, "cap")
    return Solved(float(brentq(gap, 0.0, MAX_LAMBDA, xtol=1e-8)), None)


def solve_spread(f: Flows, curve: Curve, clean: float, hazard: float | HazardFn,
                 recovery: float | RecoveryFn, lo: float = -0.2, hi: float = 0.5) -> Solved:
    """The extra discount spread (decimal) over a hazard path that reprices `clean`: the liquidity
    premium at issue, or a mark's residual over the model."""
    target = clean + f.accrued

    def gap(s: float) -> float:
        return dirty_price(f, curve, hazard, recovery, s) - target

    if gap(lo) <= 0.0:
        return Solved(lo, "floor")
    if gap(hi) >= 0.0:
        return Solved(hi, "cap")
    return Solved(float(brentq(gap, lo, hi, xtol=1e-9)), None)


def yield_to_maturity(f: Flows, clean: float) -> float | None:
    """Semiannually compounded yield (percent) of the clean price."""
    target = clean + f.accrued
    if target <= 0:
        return None

    def gap(y: float) -> float:
        return float(np.sum(f.amounts / (1.0 + y / 2.0) ** (2.0 * f.times))) - target

    try:
        return 100.0 * float(brentq(gap, -0.5, 5.0, xtol=1e-10))
    except ValueError:
        return None


def clean_at_yield(f: Flows, yield_pct: float) -> float:
    """Clean price per 100 par at a semiannually compounded yield (percent): the inverse of
    `yield_to_maturity`."""
    y = yield_pct / 100.0
    return float(np.sum(f.amounts / (1.0 + y / 2.0) ** (2.0 * f.times))) - f.accrued


def g_spread_bp(f: Flows, curve: Curve, clean: float) -> float | None:
    """Yield minus the Treasury par yield at the same maturity (bp): the comps' 'spread at issue'."""
    y = yield_to_maturity(f, clean)
    return None if y is None else (y - curve.par_yield(f.maturity_years)) * 100.0
