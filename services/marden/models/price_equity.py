"""Equity-implied credit (CreditGrades; Finger et al. 2002), used only for daily moves between bond prints
(docs/methods/pricing-v0.1.md "Calibration"; locked rule: bond-only levels, equity only updates between
prints).

Inputs, point in time:
- daily closes, `series` `equity:<TICKER>` field `close` (Yahoo Finance, unlicensed-commercial: dropped
  when MARDEN_SHOW_UNLICENSED_PRICES is false, like every other row of that source);
- debt per share D = total debt / shares outstanding from the issuer's SEC XBRL facts, each value
  public 40 days after its period end (marden.models.pit): total debt is `total_debt_usd`, else
  `long_term_debt_usd` (+ `debt_current_usd` when filed for the same period); shares are
  `shares_outstanding` (cover page), else `common_shares_outstanding`;
- equity volatility: annualised standard deviation of the last VOL_WINDOW daily log returns (at least
  VOL_MIN_OBS).

CreditGrades with the published constants: mean global recovery L = 0.5, its uncertainty lambda = 0.3,
asset vol sigma = sigma_S * S / (S + L D), recovery on the debt R = 0.4; the 5y spread uses the 5y
Treasury rate. The equity-implied intensity is spread / (1 - R).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

import numpy as np
from scipy.stats import norm

from marden.core.db import Conn
from marden.models import pit

L_BAR = 0.5
LAMBDA_BARRIER = 0.3
RECOVERY = 0.4
HORIZON = 5.0
VOL_WINDOW = 250
VOL_MIN_OBS = 60
DEBT_FIELDS = ("total_debt_usd", "long_term_debt_usd", "debt_current_usd")
SHARE_FIELDS = ("shares_outstanding", "common_shares_outstanding")


def creditgrades_spread(s: float, d: float, sigma_s: float, r: float, t: float = HORIZON) -> float:
    """CreditGrades credit spread (decimal) for horizon t."""
    if s <= 0 or d <= 0 or sigma_s <= 0:
        raise ValueError("CreditGrades needs a positive price, debt per share and volatility")
    lam2 = LAMBDA_BARRIER**2
    sigma = sigma_s * s / (s + L_BAR * d)
    dd = (s + L_BAR * d) / (L_BAR * d) * math.exp(lam2)
    xi = lam2 / sigma**2
    z = math.sqrt(0.25 + 2.0 * r / sigma**2)

    def surv(u: float) -> float:
        a = math.sqrt(sigma**2 * u + lam2)
        return float(norm.cdf(-a / 2 + math.log(dd) / a) - dd * norm.cdf(-a / 2 - math.log(dd) / a))

    def g(u: float) -> float:
        su = sigma * math.sqrt(u)
        return float(dd ** (z + 0.5) * norm.cdf(-math.log(dd) / su - z * su)
                     + dd ** (-z + 0.5) * norm.cdf(-math.log(dd) / su + z * su))

    p0 = surv(0.0)
    h = math.exp(r * xi) * (g(t + xi) - g(xi))
    denom = p0 - surv(t) * math.exp(-r * t) - h
    if denom <= 0:
        return float("nan")
    return r * (1.0 - RECOVERY) * (1.0 - p0 + h) / denom


@dataclass
class EquitySeries:
    ticker: str
    closes: dict[date, float]
    debt_per_share: list[tuple[date, float, str]]  # (public date, D, basis), oldest first

    def days(self) -> list[date]:
        return sorted(self.closes)

    def vol(self, day: date) -> float | None:
        ds = [d for d in self.days() if d <= day][-(VOL_WINDOW + 1):]
        if len(ds) < VOL_MIN_OBS + 1:
            return None
        px = np.array([self.closes[d] for d in ds])
        rets = np.diff(np.log(px))
        return float(np.std(rets, ddof=1) * math.sqrt(252.0))

    def d_on(self, day: date) -> tuple[float, str] | None:
        found = [(d, v, b) for d, v, b in self.debt_per_share if d <= day]
        return (found[-1][1], found[-1][2]) if found else None

    def intensity(self, day: date, r: float) -> float | None:
        """Equity-implied intensity on `day` (None when an input is missing)."""
        if day not in self.closes:
            return None
        sigma, dps = self.vol(day), self.d_on(day)
        if sigma is None or dps is None:
            return None
        spread = creditgrades_spread(self.closes[day], dps[0], sigma, r)
        return None if math.isnan(spread) else spread / (1.0 - RECOVERY)


def _numeric(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def load(conn: Conn, as_of: date, entity_id: str, ticker: str | None) -> tuple[EquitySeries | None, list[str]]:
    """The issuer's equity series and debt per share as public on `as_of`, and what is missing."""
    missing: list[str] = []
    if not ticker:
        return None, [f"entity {entity_id}: ticker"]
    closes: dict[date, float] = {}
    for r in pit.fact_rows(conn, as_of, "series", [f"equity:{ticker}"], ["close"]):
        v = _numeric(r["value"])
        if v is not None and v > 0:
            closes.setdefault(r["valid_from"], v)
    if not closes:
        missing.append(f"series equity:{ticker} close")
    rows = pit.fact_rows(conn, as_of, "entity", [entity_id], [*DEBT_FIELDS, *SHARE_FIELDS])
    by_period: dict[date, dict[str, tuple[float, date]]] = defaultdict(dict)
    for r in rows:
        v = _numeric(r["value"])
        if v is None:
            continue
        by_period[r["valid_from"]].setdefault(r["field"], (v, pit.public_date(r)))
    shares = sorted((p, f[k]) for p, f in by_period.items() for k in SHARE_FIELDS if k in f)
    dps: list[tuple[date, float, str]] = []
    for period, f in sorted(by_period.items()):
        if "total_debt_usd" in f:
            debt, public, basis = f["total_debt_usd"][0], f["total_debt_usd"][1], "total_debt_usd"
        elif "long_term_debt_usd" in f:
            debt, public = f["long_term_debt_usd"]
            basis = "long_term_debt_usd"
            if "debt_current_usd" in f:
                debt += f["debt_current_usd"][0]
                basis += " + debt_current_usd"
        else:
            continue
        near = [s for s in shares if abs((s[0] - period).days) <= 120]
        if not near:
            continue
        sh_period, (sh, sh_public) = min(near, key=lambda s: abs((s[0] - period).days))
        if sh <= 0:
            continue
        dps.append((max(public, sh_public), debt / sh, f"{basis} {period} / shares {sh_period}"))
    dps.sort()
    if not dps:
        missing.append(f"entity {entity_id}: XBRL total debt and shares outstanding")
    if not closes or not dps:
        return None, missing
    return EquitySeries(ticker, closes, dps), missing


def loading(bond_days: list[tuple[date, float]], eq: EquitySeries, rates: dict[date, float],
            shrink: float = 3.0) -> tuple[float, int]:
    """Loading of bond-implied log-intensity changes on CreditGrades log-intensity changes, estimated
    from month-end pairs (OLS through the origin, shrunk toward 1 by `shrink` pseudo-observations,
    clipped to [0, 2]). Returns (beta, pairs used)."""
    month_end: dict[tuple[int, int], tuple[date, float]] = {}
    for d, lam in sorted(bond_days):
        month_end[(d.year, d.month)] = (d, lam)
    pts = []
    for d, lam in sorted(month_end.values()):
        cg = eq.intensity(d, rates.get(d, 0.04)) if lam > 0 else None
        if cg is not None and cg > 0:
            pts.append((math.log(lam), math.log(cg)))
    if len(pts) < 2:
        return 1.0, 0
    dy = np.diff([p[0] for p in pts])
    dx = np.diff([p[1] for p in pts])
    n = len(dx)
    sxx = float(np.sum(dx * dx))
    beta = (float(np.sum(dx * dy)) + shrink * 1.0 * (sxx / n if n else 0.0)) / (sxx + shrink * (sxx / n)) if sxx else 1.0
    return float(min(max(beta, 0.0), 2.0)), n
