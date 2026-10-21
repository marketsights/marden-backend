"""Issuer default intensity from its traded bonds: the 'Marden bond-implied CDS' (pricing-v0.1).

For every issuer with straight bonds (kind 'bond', fixed coupon, USD; converts and foreign-currency
notes are left out because an option or an FX leg would distort the intensity):

1. each usable bond observation (price_marks: TRACE, exchange quotes, N-PORT medians with at least
   MIN_HOLDERS holders, public 60 days after the period) gives a flat intensity at 40% recovery on that
   day's Treasury curve (price_bonds.solve_flat_hazard);
2. a day's intensities are interpolated to the standard CDS maturity (price_cds.interpolate): the raw
   bond-implied level of that day;
3. the raw levels are fused by the Kalman filter (price_kalman) in log intensity; between bond prints
   the state moves with the equity-implied move (CreditGrades elasticity x the day's equity log return)
   times its loading (price_equity), so equity only updates between prints and never sets the level
   (locked rule);
4. the filtered intensity is converted with QuantLib's ISDA engine (price_cds.isda).

Every number keeps its inputs: the bonds of the latest cross-section, the equity loading and what is
missing (no straight bonds, no equity series, no debt per share).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from marden.core.db import Conn
from marden.models import price_equity
from marden.models.price_bonds import Flows, Terms, dirty_price, flows, solve_flat_hazard
from marden.models.price_cds import CdsQuote, cds_maturity, interpolate, isda
from marden.models.price_curve import CurveBook, year_frac
from marden.models.price_kalman import Reading
from marden.models.price_kalman import run as kalman
from marden.models.price_marks import Obs, daily
from marden.models.price_terms import Instrument

MIN_HOLDERS = {"oracle": 5}
DEFAULT_MIN_HOLDERS = 3
LOG_VOL = 0.5  # annual volatility of log intensity (state noise), judgment v0.1
Q_DAILY = LOG_VOL**2 / 252.0
FLOOR = 1e-4  # intensities are floored at 1bp before taking logs


@dataclass(frozen=True)
class BondPoint:
    instrument_id: str
    years: float
    hazard: float
    price: float
    source: str
    holders: int
    obs_day: date
    bound: str | None
    noise_log: float


@dataclass
class IssuerCurve:
    entity_id: str
    bonds: list[str]
    sections: dict[date, list[BondPoint]] = field(default_factory=dict)
    raw: dict[date, tuple[float, str]] = field(default_factory=dict)  # day -> (hazard at CDS maturity, how)
    days: list[date] = field(default_factory=list)
    mean: np.ndarray | None = None
    var: np.ndarray | None = None
    loading: float = 0.0
    loading_prior: float | None = None
    loading_pairs: int = 0
    ticker: str | None = None
    missing: list[str] = field(default_factory=list)

    def index(self, day: date) -> int | None:
        found = [i for i, d in enumerate(self.days) if d <= day]
        return found[-1] if found else None

    def hazard(self, day: date) -> tuple[float, float] | None:
        """Filtered (hazard, sd of log hazard) at the CDS maturity on `day`."""
        i = self.index(day)
        if i is None or self.mean is None or self.var is None:
            return None
        return math.exp(float(self.mean[i])), math.sqrt(float(self.var[i]))

    def latest_section(self, day: date) -> tuple[date, list[BondPoint]] | None:
        found = [d for d in self.sections if d <= day]
        return (max(found), self.sections[max(found)]) if found else None


def straight_bonds(instruments: dict[str, Instrument]) -> dict[str, list[Instrument]]:
    by_issuer: dict[str, list[Instrument]] = defaultdict(list)
    for inst in instruments.values():
        if inst.kind == "bond" and inst.issuer and inst.usd and inst.terms is not None and not inst.terms.floating:
            by_issuer[inst.issuer].append(inst)
    return by_issuer


def _point(inst: Instrument, o: Obs, book: CurveBook) -> BondPoint | None:
    curve = book.curve(o.day)
    if curve is None or inst.terms is None or inst.terms.maturity <= o.day:
        return None
    f: Flows = flows(inst.terms, curve, o.day, None)
    s = solve_flat_hazard(f, curve, o.price)
    base = max(s.value, FLOOR)
    h = max(base * 0.01, 1e-5)  # price noise -> log-intensity noise through dP/dlambda at the solution
    slope = abs(dirty_price(f, curve, base + h) - dirty_price(f, curve, max(base - h, 0.0))) / (2 * h)
    noise = max(o.noise / max(slope, 1e-6) / base, 0.02)
    return BondPoint(inst.id, year_frac(o.day, inst.terms.maturity), base, o.price, o.source, o.holders, o.day,
                     s.bound, noise)


def sections(bonds: list[Instrument], obs: dict[str, list[Obs]], book: CurveBook, min_holders: int,
             exclude: str | None = None, sources: tuple[str, ...] = ("trace", "exchange", "nport"),
             ) -> dict[date, list[BondPoint]]:
    """Per day, the bond intensities usable that day (one observation per bond and day, the best of
    `sources`)."""
    out: dict[date, list[BondPoint]] = defaultdict(list)
    for inst in bonds:
        if inst.id == exclude:
            continue
        for day, o in daily(obs.get(inst.id, []), sources).items():
            if o.source == "nport" and o.holders < min_holders:
                continue
            p = _point(inst, o, book)
            if p is not None:
                out[day].append(p)
    return dict(out)


def raw_level(day: date, points: list[BondPoint]) -> tuple[float, str, float]:
    """Hazard at the CDS maturity for one day's points, how it was read, and its log noise."""
    target = year_frac(day, cds_maturity(day))
    value, how = interpolate([(p.years, p.hazard) for p in points], target)
    nearest = min(points, key=lambda p: abs(p.years - target))
    return max(value, FLOOR), how, nearest.noise_log / math.sqrt(min(len(points), 2))


def build(conn: Conn, as_of: date, entity_id: str, bonds: list[Instrument], obs: dict[str, list[Obs]],
          book: CurveBook, ticker: str | None) -> IssuerCurve:
    """Raw and filtered bond-implied intensities of one issuer up to `as_of`."""
    cur = IssuerCurve(entity_id, [b.id for b in bonds], ticker=ticker)
    min_h = MIN_HOLDERS.get(entity_id, DEFAULT_MIN_HOLDERS)
    cur.sections = sections(bonds, obs, book, min_h)
    if not cur.sections:
        cur.missing.append(f"entity {entity_id}: no usable straight-bond price (N-PORT needs >= {min_h} holders)")
        return cur
    noise: dict[date, float] = {}
    for day, pts in cur.sections.items():
        value, how, nz = raw_level(day, pts)
        cur.raw[day] = (value, how)
        noise[day] = nz
    start = min(cur.raw)
    cur.days = book.business_days(start, as_of)
    if as_of not in cur.days and book.curve(as_of) is not None:
        cur.days = sorted({*cur.days, as_of})  # a weekend or holiday run reads the last curve
    eq, missing = price_equity.load(conn, as_of, entity_id, ticker)
    cur.missing.extend(missing)
    drift = np.zeros(len(cur.days))
    if eq is not None:
        rates = {d: (c.par_yield(5.0) / 100.0) for d in cur.days if (c := book.curve(d)) is not None}
        cum = price_equity.moves(eq, cur.days, rates)
        first_day, (first_lam, _) = min(cur.raw.items())
        anchor = next((d for d in cur.days if d >= first_day and d in eq.closes), None)
        prior = price_equity.prior_loading(eq, anchor, first_lam, rates.get(anchor, 0.04)) if anchor else None
        if prior is None:
            cur.missing.append(f"entity {entity_id}: CreditGrades elasticity (equity moves not used)")
            prior = 0.0
        cur.loading_prior = prior
        cur.loading, cur.loading_pairs = price_equity.loading([(d, v[0]) for d, v in sorted(cur.raw.items())], cum,
                                                              prior)
        values = [cum[d] for d in cur.days]
        drift[1:] = cur.loading * np.diff(values)
    index = {d: i for i, d in enumerate(cur.days)}
    readings = [Reading(index[d], math.log(v[0]), noise[d] ** 2, False, d.isoformat())
                for d, v in sorted(cur.raw.items()) if d in index]
    first = readings[0]
    filt = kalman(len(cur.days), first.value, 1.0, Q_DAILY, readings, drift)
    cur.mean, cur.var = filt.mean, filt.var
    return cur


def quote(cur: IssuerCurve, day: date, book: CurveBook) -> dict[str, object] | None:
    """The filtered CDS quote on `day` with an 80% band (ISDA conversion of hazard x exp(+-1.28 sd))."""
    got = cur.hazard(day)
    curve = book.curve(day)
    if got is None or curve is None:
        return None
    lam, sd = got
    q: CdsQuote = isda(day, curve, lam)
    lo = isda(day, curve, lam * math.exp(-1.2816 * sd)).spread_bp
    hi = isda(day, curve, lam * math.exp(1.2816 * sd)).spread_bp
    return {"hazard": lam, "logSd": sd, "spreadBp": q.spread_bp, "upfrontPct": q.upfront_pct,
            "maturity": q.maturity.isoformat(), "years": q.years, "band": {"low": lo, "high": hi}}


def raw_quote(day: date, points: list[BondPoint], book: CurveBook) -> dict[str, object] | None:
    """The unfiltered bond-implied CDS of one cross-section (what the methodology's Results grade)."""
    curve = book.curve(day)
    if curve is None or not points:
        return None
    lam, how, _ = raw_level(day, points)
    q = isda(day, curve, lam)
    return {"date": day.isoformat(), "hazard": lam, "how": how, "spreadBp": q.spread_bp,
            "upfrontPct": q.upfront_pct, "maturity": q.maturity.isoformat(),
            "bonds": [{"instrumentId": p.instrument_id, "price": round(p.price, 4), "source": p.source,
                       "holders": p.holders, "years": round(p.years, 3), "hazard": round(p.hazard, 6),
                       **({"bound": p.bound} if p.bound else {})} for p in sorted(points, key=lambda p: p.years)]}


def bond_hazard(cur: IssuerCurve, terms: Terms, day: date, exclude: str | None = None) -> float | None:
    """The issuer's intensity at a bond's maturity on `day`: the latest raw cross-section's line (without
    `exclude`) scaled to the filtered CDS-maturity level."""
    sec = cur.latest_section(day)
    got = cur.hazard(day)
    if sec is None or got is None:
        return None
    sec_day, pts = sec
    pts = [p for p in pts if p.instrument_id != exclude]
    if not pts:
        return None
    t_bond = year_frac(sec_day, terms.maturity)
    t_cds = year_frac(sec_day, cds_maturity(sec_day))
    at_bond, _ = interpolate([(p.years, p.hazard) for p in pts], t_bond)
    at_cds, _ = interpolate([(p.years, p.hazard) for p in pts], t_cds)
    shape = at_bond / at_cds if at_cds > FLOOR else 1.0
    return max(got[0] * shape, 0.0)
