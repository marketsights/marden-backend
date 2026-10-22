"""The Instruments curve card (spec 06 "Curve fitting"): the Treasury curve, and per credit tier a curve
fitted to that tier's own instruments as the price engine priced them on the run's as-of day.

Dots. Every instrument the engine priced and placed in one of the six tiers (price.instrument_tier), at
its years to maturity and its Marden yield (tag M, dated the run day), with its spread over Treasuries:
the yield minus the Treasury par yield at the same maturity on the run day's curve (bp), and the latest
observed price behind it (TRACE print, exchange or dealer quote, fund mark) with that observation's tag.

Benchmark. The U.S. Treasury daily par yield curve public on the run day (price_curve; tag F), drawn
as the reference every tier curve sits on.

Tier curves. Per tier the spread term structure

    s(T) = a + b x NS(T),   NS(T) = (1 - exp(-T/tau)) / (T/tau), tau = 3y (Nelson-Siegel slope loading)

is fitted to the tier's dots by Huber-weighted least squares (iteratively reweighted; a dot further than
HUBER_K robust SDs from the curve counts for less, so one far-off dot cannot pull the line). The robust SD
is 1.4826 x the median absolute residual, floored at SCALE_FLOOR_BP. The tier's yield curve is the
Treasury par curve plus s(T): solid across the dots' maturities, dashed EXTEND beyond each end.

- A tier with fewer than MIN_POINTS usable dots gets no curve: its dots are drawn with a plain note.
- The slope b is fitted only when at least SLOPE_MIN_SIDE dots sit on each side of the middle of the
  tier's maturity range (log scale) and the range spans at least SLOPE_MIN_SPAN x; otherwise one or two
  dots, or which issuers happen to sit at which end of a short range, would set it, so the spread is held
  flat and the curve has the Treasury curve's shape. A fitted slope that would take the drawn curve below
  Treasuries is not used either (the spread is held flat).
- With OUTLIER_MIN_POINTS or more dots, a dot more than OUTLIER_SDS robust SDs from the first fit is an
  outlier: drawn, flagged, left out, and the curve refitted without it.
- A dot the engine priced off the pricing tier curves (price_tiers, mode 'tier curve') is drawn but not
  fitted: it would fit the curve to itself.

Point in time: the dots are the run's own prices and the Treasury curve is the one public on the run day
(price_curve.CurveBook), so a backtest run draws the curve as it stood that day.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np

from marden.models.price_curve import TENORS, Curve
from marden.models.price_marks import Obs
from marden.models.price_tiers import TIERS

TAU = 3.0
HUBER_K = 1.345
SCALE_FLOOR_BP = 25.0  # so a dot is never an outlier for sitting under 75bp off a tight tier
MIN_POINTS = 3
SLOPE_MIN_SIDE = 3
SLOPE_MIN_SPAN = 2.0  # the dots must span at least a doubling of maturity
OUTLIER_MIN_POINTS = 5
OUTLIER_SDS = 3.0
EXTEND = 1.25  # dashed beyond the dots: x1.25 in maturity each way (spec 06: +-25%)
GRID_STEPS = 24
MIN_YEARS, MAX_YEARS = 0.25, 30.0
TREASURY_LABEL = "U.S. Treasury"
TREASURY_SOURCE = "U.S. Treasury daily par yield curve"
TREASURY_URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve"
FIT_SOURCE = "Marden tier curve: Treasury plus a robust fit of the tier's spreads"
# The tag of an observed price by where it comes from (price_marks.Obs.source; price_obs tags).
OBS_TAG = {"trace": "F", "nport": "F", "bdc": "F", "exchange": "R", "dealer": "R"}
OBS_WORDS = {"trace": "TRACE print", "nport": "N-PORT fund mark", "bdc": "BDC fund mark",
             "exchange": "Exchange quote", "dealer": "Dealer quote"}


def ns(t: float | np.ndarray) -> Any:
    x = np.maximum(np.asarray(t, dtype=float), 1e-6) / TAU
    out = (1.0 - np.exp(-x)) / x
    return float(out) if out.ndim == 0 else out


@dataclass(frozen=True)
class Dot:
    instrument_id: str
    tier: str
    years: float
    yield_pct: float
    spread_bp: float
    tier_priced: bool = False  # priced off the pricing tier curve (not fitted)


@dataclass
class TierCurve:
    tier: str
    dots: list[Dot]
    level_bp: float | None = None  # a
    slope_bp: float | None = None  # b (None: spread held flat)
    sd_bp: float | None = None  # robust SD of the fitted dots around the curve
    used: list[str] = field(default_factory=list)
    excluded: dict[str, str] = field(default_factory=dict)  # instrument -> why it is not in the fit
    note: str | None = None

    @property
    def fitted(self) -> bool:
        return self.level_bp is not None

    def spread(self, years: float) -> float | None:
        if self.level_bp is None:
            return None
        return self.level_bp + (self.slope_bp or 0.0) * ns(years)

    def span(self) -> tuple[float, float] | None:
        years = [d.years for d in self.dots if d.instrument_id in self.used]
        return (min(years), max(years)) if years else None


def _mad_sd(r: np.ndarray) -> float:
    return max(1.4826 * float(np.median(np.abs(r))), SCALE_FLOOR_BP)


def huber(years: np.ndarray, spreads: np.ndarray, sloped: bool, iters: int = 100) -> tuple[float, float | None, float]:
    """(a, b, robust SD) of s = a + b NS(T) by Huber IRLS; the scale is the MAD about the median, held fixed."""
    x = np.column_stack([np.ones(len(years)), ns(years)]) if sloped else np.ones((len(years), 1))
    scale = _mad_sd(spreads - float(np.median(spreads)))
    beta = np.linalg.lstsq(x, spreads, rcond=None)[0] if sloped else np.array([float(np.median(spreads))])
    k = HUBER_K * scale
    for _ in range(iters):
        r = spreads - x @ beta
        w = np.where(np.abs(r) <= k, 1.0, k / np.maximum(np.abs(r), 1e-9))
        sw = np.sqrt(w)
        new = np.linalg.lstsq(x * sw[:, None], spreads * sw, rcond=None)[0]
        if np.max(np.abs(new - beta)) < 1e-6:
            beta = new
            break
        beta = new
    sd = _mad_sd(spreads - x @ beta)
    return float(beta[0]), (float(beta[1]) if sloped else None), sd


def slope_identified(years: Sequence[float]) -> bool:
    """Enough dots on both sides of the middle of the maturity range (log scale) for a slope."""
    lo, hi = min(years), max(years)
    if hi / lo < SLOPE_MIN_SPAN:
        return False
    mid = math.sqrt(lo * hi)
    return sum(1 for y in years if y < mid) >= SLOPE_MIN_SIDE and sum(1 for y in years if y > mid) >= SLOPE_MIN_SIDE


def _fit(years: np.ndarray, spreads: np.ndarray) -> tuple[float, float | None, float]:
    """Huber fit with a slope where the dots identify one and it keeps the drawn curve above Treasuries."""
    if slope_identified(list(years)):
        a, b, sd = huber(years, spreads, True)
        drawn = np.exp(np.linspace(math.log(max(min(years) / EXTEND, MIN_YEARS)), math.log(max(years) * EXTEND), 50))
        if b is not None and float(np.min(a + b * ns(drawn))) > 0.0:
            return a, b, sd
    return huber(years, spreads, False)


def _bp(v: float) -> str:
    """Whole bp, halves rounded up, with thousands commas (as the web app formats bp)."""
    return f"{math.floor(v + 0.5):,}bp"


def fit_tier(tier: str, dots: list[Dot]) -> TierCurve:
    """One tier's curve (module doc)."""
    tc = TierCurve(tier, sorted(dots, key=lambda d: d.years))
    cand = []
    for d in tc.dots:
        if d.tier_priced:
            tc.excluded[d.instrument_id] = ("priced off the pricing tier curve, not from its own market data, so it "
                                            "is not used to fit the curve")
        else:
            cand.append(d)
    if len(cand) < MIN_POINTS:
        tc.note = (f"{len(cand)} priced instrument{'' if len(cand) == 1 else 's'}: a curve needs at least "
                   f"{MIN_POINTS}, so only the dots are shown")
        return tc
    years = np.array([d.years for d in cand])
    spreads = np.array([d.spread_bp for d in cand])
    a, b, sd = _fit(years, spreads)
    if len(cand) >= OUTLIER_MIN_POINTS:
        resid = spreads - (a + (b or 0.0) * ns(years))
        keep = []
        for d, r in zip(cand, resid, strict=True):
            if abs(r) > OUTLIER_SDS * sd:
                side, spread = ("above" if r > 0 else "below"), _bp(round(d.spread_bp, 1))
                tc.excluded[d.instrument_id] = (
                    f"outlier: its spread {spread} is {_bp(abs(r))} {side} the tier curve, more than "
                    f"{OUTLIER_SDS:g} robust SDs ({_bp(OUTLIER_SDS * sd)}); left out of the fit")
            else:
                keep.append(d)
        if len(keep) < len(cand):
            cand = keep
            years = np.array([d.years for d in cand])
            spreads = np.array([d.spread_bp for d in cand])
            a, b, sd = _fit(years, spreads)
    tc.level_bp, tc.slope_bp, tc.sd_bp = a, b, sd
    tc.used = [d.instrument_id for d in cand]
    lo, hi = min(years), max(years)
    if b is None:
        span = f"{lo:.1f}\u2013{hi:.1f}y"
        if hi / lo < SLOPE_MIN_SPAN:
            tc.note = f"Spread held flat: its dots all mature within {span}, too narrow a range to fit a slope"
        elif not slope_identified(list(years)):
            tc.note = (f"Spread held flat: too few dots at each end of {span} to fit a slope (it needs "
                       f"{SLOPE_MIN_SIDE} at each end)")
        else:
            tc.note = "Spread held flat: a fitted slope would take the curve below Treasuries"
    return tc


def _years_grid(lo: float, hi: float, steps: int = GRID_STEPS) -> list[float]:
    a, b = max(lo, MIN_YEARS), min(hi, MAX_YEARS)
    if b <= a:
        return [round(a, 3)]
    return [round(float(v), 3) for v in np.exp(np.linspace(math.log(a), math.log(b), steps))]


def _rounded(v: float | None, nd: int) -> float | None:
    return None if v is None else round(v, nd)


def last_observation(obs: Sequence[Obs]) -> dict[str, Any] | None:
    """The latest observed price of an instrument usable on the run day (price_marks), with its tag."""
    if not obs:
        return None
    o = max(obs, key=lambda x: (x.day, x.public))
    return {"date": o.day.isoformat(), "price": round(o.price, 3), "tag": OBS_TAG.get(o.source, "R"),
            "kind": OBS_WORDS.get(o.source, o.source), "sourceName": o.source_name,
            **({"sourceUrl": o.source_url} if o.source_url else {})}


def benchmark_doc(curve: Curve) -> dict[str, Any]:
    """The Treasury par curve as a CurveTier with kind 'benchmark' (no dots)."""
    nodes = [TENORS[k] for k in curve.par if TENORS[k] >= MIN_YEARS]
    years = sorted({*_years_grid(min(nodes), max(nodes), 40), *nodes})
    return {
        "tier": TREASURY_LABEL, "kind": "benchmark", "points": [],
        "fit": [{"years": round(y, 3), "yieldPct": round(curve.par_yield(y), 3), "spreadBp": 0.0} for y in years],
        "note": None,
        "source": {"tag": "F", "asOf": curve.day.isoformat(), "sourceName": TREASURY_SOURCE,
                   "sourceUrl": TREASURY_URL},
    }


def tier_doc(tc: TierCurve, curve: Curve, as_of: date, method_version: str, method_url: str,
             info: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """CurveTier (types.ts) for one tier: its dots (with spread, source and fit use) and its fitted curve."""
    points = []
    for d in tc.dots:
        extra = info.get(d.instrument_id, {})
        points.append({"years": d.years, "yieldPct": d.yield_pct, "instrumentId": d.instrument_id,
                       "spreadBp": round(d.spread_bp, 1),
                       "source": {"tag": "M", "asOf": as_of.isoformat(), "sourceName": "Marden model price",
                                  "sourceUrl": method_url, "methodVersion": method_version},
                       "lastObservation": extra.get("lastObservation"),
                       "excluded": tc.excluded.get(d.instrument_id)})
    fit: list[dict[str, Any]] = []
    span = tc.span()
    if tc.fitted and span is not None:
        for y in _years_grid(span[0] / EXTEND, span[1] * EXTEND):
            s = tc.spread(y)
            assert s is not None
            fit.append({"years": y, "yieldPct": round(curve.par_yield(y) + s / 100.0, 3), "spreadBp": round(s, 1)})
    return {
        "tier": tc.tier, "kind": "tier", "points": points, "fit": fit,
        "range": [span[0], span[1]] if tc.fitted and span is not None else None,
        "robustSdBp": _rounded(tc.sd_bp, 1), "note": tc.note,
        "source": {"tag": "M", "asOf": as_of.isoformat(), "sourceName": FIT_SOURCE, "sourceUrl": method_url,
                   "methodVersion": method_version},
    }


def dots_from_prices(prices: dict[str, dict[str, Any]], years: dict[str, float], curve: Curve) -> list[Dot]:
    """The card's dots: priced instruments with a tier, a yield and a maturity."""
    out = []
    for iid, o in sorted(prices.items()):
        tier, y = o.get("tier"), o.get("yieldPct")
        if tier not in TIERS or not isinstance(y, int | float) or iid not in years or years[iid] <= 0:
            continue
        t = years[iid]
        mode = (o.get("basis") or {}).get("mode")
        out.append(Dot(iid, tier, t, float(y), (float(y) - curve.par_yield(t)) * 100.0, mode == "tier curve"))
    return out


def card(prices: dict[str, dict[str, Any]], years: dict[str, float], obs: dict[str, list[Obs]], curve: Curve,
         as_of: date, method_version: str, method_url: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(CurveTier[] for the 'curve' output: Treasury first, then each tier; the per-tier fit for 'curve_fit')."""
    dots = dots_from_prices(prices, years, curve)
    info = {d.instrument_id: {"lastObservation": last_observation(obs.get(d.instrument_id, []))} for d in dots}
    docs = [benchmark_doc(curve)]
    fits: dict[str, Any] = {}
    for tier in TIERS:
        mine = [d for d in dots if d.tier == tier]
        if not mine:
            continue
        tc = fit_tier(tier, mine)
        docs.append(tier_doc(tc, curve, as_of, method_version, method_url, info))
        fits[tier] = {"dots": len(mine), "used": len(tc.used), "levelBp": _rounded(tc.level_bp, 1),
                      "slopeBp": _rounded(tc.slope_bp, 1), "robustSdBp": _rounded(tc.sd_bp, 1),
                      "excluded": tc.excluded, "note": tc.note}
    method = {"tiers": fits, "treasuryCurveDate": curve.day.isoformat(), "tauYears": TAU, "huberK": HUBER_K,
              "minPoints": MIN_POINTS, "outlierSds": OUTLIER_SDS, "outlierMinPoints": OUTLIER_MIN_POINTS,
              "slopeMinSide": SLOPE_MIN_SIDE, "slopeMinSpan": SLOPE_MIN_SPAN, "extend": EXTEND}
    return docs, method
