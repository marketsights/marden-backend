"""Bond-implied CDS (docs/methods/pricing-v0.1.md "Bond-implied CDS"), steps 2 and 3.

2. The flat intensities of an issuer's bonds on one day are interpolated linearly by maturity to the
   standard CDS maturity date (QuantLib `cdsMaturity`, CDS2015 rule: the 20th of Jun/Dec after the
   Mar/Sep roll; 2031-12-20 for an Oct 2026 trade). When every bond matures on one side of that date
   the line through the two nearest bonds is extrapolated (floored at zero); one bond is used flat.
   Bonds are never averaged (locked rule).
3. QuantLib's ISDA engine (IsdaCdsEngine) prices a standard contract on a flat hazard curve at that
   intensity: quarterly CDS2015 schedule, Act/360 premium accrual (last period Act/360 inclusive),
   accrual paid on default, 40% recovery, discounted on the day's Treasury zero curve. Outputs: the
   conventional spread (bp) and the upfront (percent of notional) of a 500bp running-coupon contract,
   positive when the protection buyer pays.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import QuantLib as ql

from marden.models.price_curve import Curve, year_frac

RECOVERY = 0.40
COUPON = 0.05
TENOR_YEARS = 5


def ql_date(d: date) -> ql.Date:
    return ql.Date(d.day, d.month, d.year)


def py_date(d: ql.Date) -> date:
    return date(d.year(), d.month(), d.dayOfMonth())


def cds_maturity(trade: date, years: int = TENOR_YEARS) -> date:
    return py_date(ql.cdsMaturity(ql_date(trade), ql.Period(years, ql.Years), ql.DateGeneration.CDS2015))


def interpolate(points: Sequence[tuple[float, float]], target: float) -> tuple[float, str]:
    """Intensity at `target` years from (maturity years, intensity) points; returns (value, how)."""
    pts = sorted(points)
    if not pts:
        raise ValueError("no bond intensities to interpolate")
    if len(pts) == 1:
        return pts[0][1], "flat (one bond)"
    for (t0, l0), (t1, l1) in zip(pts, pts[1:], strict=False):
        if t0 <= target <= t1:
            w = 0.0 if t1 == t0 else (target - t0) / (t1 - t0)
            return l0 + w * (l1 - l0), "interpolated"
    (t0, l0), (t1, l1) = (pts[-2], pts[-1]) if target > pts[-1][0] else (pts[0], pts[1])
    if t1 == t0:
        return (l0 + l1) / 2.0, "flat (bonds share a maturity)"
    value = l0 + (target - t0) * (l1 - l0) / (t1 - t0)
    return max(value, 0.0), "extrapolated"


def _discount_handle(day: date, curve: Curve) -> ql.YieldTermStructureHandle:
    """The day's Treasury curve as a log-linear discount curve (flat forwards between nodes, which the
    ISDA engine requires)."""
    td = ql_date(day)
    dc = ql.Actual365Fixed()
    dates, dfs = [td], [1.0]
    for t in curve.times:
        d = td + max(int(round(float(t) * 365.25)), 1)
        if d > dates[-1]:
            dates.append(d)
            dfs.append(float(curve.df(dc.yearFraction(td, d) * 365.0 / 365.25)))
    dcurve = ql.DiscountCurve(dates, dfs, dc)
    dcurve.enableExtrapolation()
    return ql.YieldTermStructureHandle(dcurve)


@dataclass(frozen=True)
class CdsQuote:
    trade: date
    maturity: date
    years: float
    hazard: float
    spread_bp: float
    upfront_pct: float


def isda(day: date, curve: Curve, hazard: float, maturity: date | None = None,
         recovery: float = RECOVERY) -> CdsQuote:
    """Conventional spread and 500bp upfront of a standard CDS on a flat `hazard` curve. A weekend
    day trades on the business day before it."""
    day = py_date(ql.WeekendsOnly().adjust(ql_date(day), ql.Preceding))
    ql.Settings.instance().evaluationDate = ql_date(day)
    mat = maturity or cds_maturity(day)
    td = ql_date(day)
    dc = ql.Actual365Fixed()
    disc = _discount_handle(day, curve)
    prob = ql.DefaultProbabilityTermStructureHandle(ql.FlatHazardRate(td, ql.QuoteHandle(ql.SimpleQuote(hazard)), dc))
    sched = ql.Schedule(td, ql_date(mat), ql.Period(ql.Quarterly), ql.WeekendsOnly(), ql.Following, ql.Unadjusted,
                        ql.DateGeneration.CDS2015, False)
    engine = ql.IsdaCdsEngine(prob, recovery, disc)

    def contract(upfront: float) -> ql.CreditDefaultSwap:
        c = ql.CreditDefaultSwap(ql.Protection.Buyer, 1e7, upfront, COUPON, sched, ql.Following, ql.Actual360(), True,
                                 True, td + 1, ql.WeekendsOnly().advance(td, 3, ql.Days), ql.FaceValueClaim(),
                                 ql.Actual360(True), True, td, 3)
        c.setPricingEngine(engine)
        return c

    upfront = contract(0.0).fairUpfront()
    spread = contract(upfront).conventionalSpread(recovery, disc, dc, ql.CreditDefaultSwap.ISDA)
    return CdsQuote(day, mat, year_frac(day, mat), hazard, spread * 1e4, upfront * 100.0)


def hazard_for_spread(day: date, curve: Curve, spread_bp: float, recovery: float = RECOVERY) -> float:
    """The flat intensity whose standard 5y CDS quotes `spread_bp` (inverse of `isda`, for tests and
    for converting tenant spreads)."""
    from scipy.optimize import brentq

    return float(brentq(lambda h: isda(day, curve, h, recovery=recovery).spread_bp - spread_bp, 1e-6, 3.0, xtol=1e-9))
