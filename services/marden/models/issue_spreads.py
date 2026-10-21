"""A deal comp's spread at issue, computed in code (pricing-v0.1 "Tier curves"; ARCHITECTURE rule 7).

Stored `issue_spread_bp` values tagged F or R are the issuer's or the press's own figure (a SOFR margin, a
reported 'T+225') and are used as given. Values tagged M are the legacy research archive's own arithmetic
('yield at issue 7.23% ... minus 5-yr Treasury 3.73%'), so they are never used: the spread is recomputed
here from the deal terms, in order:

1. a spread to Treasuries stated in the deal's rate text ('6.581% fixed (T+225 reported)'), tag R;
2. yield at issue (the coupon at the issue price over the tenor, semiannual bullet) minus the Treasury par
   yield at that tenor on the issue date (U.S. Treasury curve, marden.models.price_curve).

A comp that gives neither is left out with the reason; the archive's number is never the fallback.
"""

from __future__ import annotations

import re
from typing import Protocol

from scipy.optimize import brentq

REPORTED = re.compile(r"\bT\s*\+\s*(\d+(?:\.\d+)?)")


class ParCurve(Protocol):
    def par_yield(self, years: float) -> float: ...


def bullet_yield(coupon_pct: float, price: float, years: float, freq: int = 2) -> float | None:
    """Yield (percent, compounded `freq` times a year) of a bullet bond at `price` per 100 par."""
    n = max(1, round(years * freq))
    c = coupon_pct / freq

    def gap(y: float) -> float:
        d = 1.0 + y / freq
        return sum(c / d**k for k in range(1, n + 1)) + 100.0 / d**n - price

    try:
        return float(brentq(gap, -0.5, 2.0)) * 100.0
    except ValueError:
        return None


def issue_spread(stored_bp: float | None, stored_tag: str | None, *, rate_text: str, coupon_pct: float | None,
                 issue_price: float | None, tenor_years: float | None,
                 curve: ParCurve | None) -> tuple[float | None, str]:
    """(spread over the benchmark at issue in bp, how it was found); (None, why) when it cannot be."""
    if stored_bp is not None and (stored_tag or "").strip() in ("F", "R"):
        return float(stored_bp), "stated at issue"
    reported = REPORTED.search(rate_text or "")
    if reported:
        return float(reported.group(1)), "spread to Treasuries stated in the deal terms"
    if coupon_pct is None or tenor_years is None or tenor_years <= 0:
        return None, "no stated spread, coupon or tenor in the deal terms (the archive's computed spread is not used)"
    if curve is None:
        return None, "no Treasury curve public on the issue date"
    price = issue_price or 100.0
    ytm = bullet_yield(coupon_pct, price, tenor_years)
    if ytm is None:
        return None, "yield at issue did not solve"
    ust = curve.par_yield(tenor_years)
    return (ytm - ust) * 100.0, (f"yield at issue {ytm:.2f}% ({coupon_pct:g}% at {price:g}) minus "
                                 f"{tenor_years:g}y Treasury {ust:.2f}% (computed)")
