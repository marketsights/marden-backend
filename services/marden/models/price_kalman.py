"""Kalman filter for sparse, noisy, stale observations (docs/methods/pricing-v0.1.md "Calibration").

A scalar state x follows a random walk on business days with a known daily drift input u (the
equity-implied move between bond prints, or zero):

    x_k = x_{k-1} + u_k + w_k,   w_k ~ N(0, q)

An observation on day k reads the state directly (TRACE, exchange quotes, bond-implied levels) or, for a
stale fund mark, as a smoothed lagged reading (Getmansky, Lo and Makarov 2004):

    y_k = sum_j theta_j x_{k - lag_j} + v_k,   v_k ~ N(0, r)

with GLM weights THETA on lags of 0, 1 and 2 months (21 business days). The lags are carried as an
augmented state [x_k, x_{k-1}, ..., x_{k-L}], so the filter stays exact and linear. Observations are
processed on their own dates; the caller passes only observations that were public on the run's as-of
day, so a run for a past day is an honest backtest. Returns the filtered mean and variance per day.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

MONTH_BD = 21
THETA = (0.6, 0.3, 0.1)  # GLM smoothing weights for stale marks (judgment, v0.1)
LAGS = (0, MONTH_BD, 2 * MONTH_BD)


@dataclass(frozen=True)
class Reading:
    index: int  # business-day index of the observation's own date
    value: float
    var: float
    stale: bool = False
    label: str = ""


@dataclass
class Filtered:
    mean: np.ndarray
    var: np.ndarray
    used: list[Reading] = field(default_factory=list)
    innovations: list[tuple[Reading, float]] = field(default_factory=list)

    def at(self, k: int) -> tuple[float, float]:
        return float(self.mean[k]), float(self.var[k])


def run(n: int, x0: float, p0: float, q: float, readings: Sequence[Reading],
        drift: np.ndarray | None = None) -> Filtered:
    """Filter over `n` business days (index 0..n-1)."""
    lag = LAGS[-1]
    dim = lag + 1
    x = np.full(dim, x0, dtype=float)
    p = np.full((dim, dim), p0, dtype=float)  # the lagged copies start fully correlated with x_0
    u = np.zeros(n) if drift is None else np.asarray(drift, dtype=float)
    by_day: dict[int, list[Reading]] = {}
    for r in readings:
        if 0 <= r.index < n:
            by_day.setdefault(r.index, []).append(r)
    mean = np.empty(n)
    var = np.empty(n)
    out = Filtered(mean, var)
    for k in range(n):
        if k > 0:
            x = np.concatenate([[x[0] + u[k]], x[:-1]])
            p_new = np.empty_like(p)
            p_new[1:, 1:] = p[:-1, :-1]
            p_new[0, 1:] = p[0, :-1]
            p_new[1:, 0] = p[:-1, 0]
            p_new[0, 0] = p[0, 0] + q
            p = p_new
        for r in by_day.get(k, []):
            h = np.zeros(dim)
            if r.stale:
                for theta, lg in zip(THETA, LAGS, strict=True):
                    h[min(lg, k)] += theta
            else:
                h[0] = 1.0
            ph = p @ h
            s = float(h @ ph) + r.var
            innov = r.value - float(h @ x)
            gain = ph / s
            x = x + gain * innov
            p = p - np.outer(gain, ph)
            p = (p + p.T) / 2.0
            out.used.append(r)
            out.innovations.append((r, innov))
        mean[k] = x[0]
        var[k] = max(p[0, 0], 0.0)
    return out
