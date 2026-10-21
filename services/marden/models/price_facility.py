"""The Marden model price of one instrument on one day (pricing-v0.1 "The model" and "Calibration").

Two modes:
- issuer bond: a straight bond of an issuer with a bond-implied curve (price_issuer). Its intensity on a
  day is the issuer curve at the bond's maturity, recovery 40%, no liquidity premium.
- facility: everything else with terms. The intensity is built up along the chain (price_chain); the
  liquidity premium is solved once so the model reprices the issue price (the comp's, else 100) on the
  issue date with that day's inputs, then held. If the tenant's curve starts after the issue date, its
  first value is used for that solve and the basis says so. Without an issue comp the premium is
  solved on the first usable observation; with neither, the price is the tier curve's (price_tiers).

Either way a residual spread r over the model is filtered (price_kalman) from the instrument's own
observations (price_marks; a fund mark of a loan is a stale GLM reading), a random walk of RESID_VOL a
year. Price = reduced-form clean price at lp + r on the as-of day; the 80% band is the price at
r +- 1.28 sd(r) combined with the tenant's intensity uncertainty. Ranges are uncalibrated ('model v0').
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np

from marden.core.db import Conn
from marden.models import price_signals as sig
from marden.models.price_bonds import RECOVERY, Flows, clean_price, flows, g_spread_bp, solve_spread, yield_to_maturity
from marden.models.price_chain import Chain, Legs, hazard_fn, recovery_fn
from marden.models.price_curve import CurveBook
from marden.models.price_issuer import IssuerCurve, bond_hazard
from marden.models.price_kalman import Reading
from marden.models.price_kalman import run as kalman
from marden.models.price_marks import Obs
from marden.models.price_terms import Instrument
from marden.models.price_tiers import TierFit

RESID_VOL = 0.012  # annual volatility of the residual spread (decimal), judgment v0.1
Q_RESID = RESID_VOL**2 / 252.0
CALIBRATED_SD = 0.0010  # 10bp: the residual is known at the calibration price
Z80 = 1.2816


@dataclass
class Ctx:
    conn: Conn
    as_of: date
    book: CurveBook
    curves: dict[str, IssuerCurve]
    tiers: TierFit | None
    signals: dict[str, list[sig.Signal]] = field(default_factory=dict)
    exclude_dealer: bool = False


@dataclass
class Model:
    """How one instrument is priced on any day: hazard + recovery for (day) -> function."""

    mode: str
    hazard: Callable[[date], tuple[float | Callable[[np.ndarray], np.ndarray] | None, list[str], dict[str, Any]]]
    recovery: float | Callable[[np.ndarray], np.ndarray]


def legs(ctx: Ctx, ch: Chain, day: date, backfill: bool = False) -> tuple[Legs, list[str], dict[str, Any]]:
    missing: list[str] = []
    info: dict[str, Any] = {}

    def lam(ent: str | None, role: str) -> float | None:
        if ent is None:
            return None
        cur = ctx.curves[ent]
        got = cur.hazard(day)
        if got is None and backfill and cur.days:
            got = cur.hazard(cur.days[0])
            info[f"{role}Backfilled"] = cur.days[0].isoformat()
        if got is None:
            missing.append(f"{role} {ent}: bond-implied intensity not yet available on {day}")
            return None
        info[f"{role}Hazard"] = got[0]
        info[f"{role}LogSd"] = got[1]
        return got[0]

    tenant, obligor, guarantor = lam(ch.tenant, "tenant"), lam(ch.obligor, "obligor"), lam(ch.guarantor, "guarantor")
    denials, delays = sig.counts(ctx.signals.get(ch.signals_site or "", []), day)
    info.update({"denials": denials, "delays": delays})
    return Legs(tenant, obligor, guarantor, denials, delays), missing, info


def model_for(ctx: Ctx, inst: Instrument, ch: Chain | None) -> Model:
    if ch is None:  # issuer bond
        cur = ctx.curves[inst.issuer or ""]

        def issuer_hazard(day: date) -> tuple[float | None, list[str], dict[str, Any]]:
            assert inst.terms is not None
            h = bond_hazard(cur, inst.terms, day)
            got = cur.hazard(day)
            info = {"issuerHazard": got[0], "issuerLogSd": got[1]} if got else {}
            return h, ([] if h is not None else [f"issuer {inst.issuer}: curve not available on {day}"]), info

        return Model("issuer bond", issuer_hazard, RECOVERY)

    def chain_hazard(day: date, backfill: bool = False) -> tuple[Callable[[np.ndarray], np.ndarray], list[str],
                                                                  dict[str, Any]]:
        lg, missing, info = legs(ctx, ch, day, backfill)
        return hazard_fn(ch, lg, day), missing, info

    return Model("facility", chain_hazard, recovery_fn(ch))  # type: ignore[arg-type]


def _flows(ctx: Ctx, inst: Instrument, day: date) -> Flows | None:
    curve = ctx.book.curve(day)
    if curve is None or inst.terms is None or inst.terms.maturity <= day:
        return None
    sofr = ctx.book.sofr_on(day)
    if inst.terms.floating and sofr is None:
        return None
    return flows(inst.terms, curve, day, sofr)


def _spread_dv(f: Flows, ctx: Ctx, day: date, hz: Any, rec: Any, s: float) -> float:
    curve = ctx.book.curve(day)
    assert curve is not None
    return abs(clean_price(f, curve, hz, rec, s - 1e-4) - clean_price(f, curve, hz, rec, s + 1e-4)) / 2.0


def calibrate(ctx: Ctx, inst: Instrument, m: Model, obs: list[Obs]) -> tuple[date, float, dict[str, Any]] | None:
    """(calibration day, liquidity premium, basis) or None."""
    if m.mode == "issuer bond":
        first = next((o.day for o in obs), None)
        return (first or ctx.as_of), 0.0, {"how": "bond-implied: no liquidity premium"}
    issue = inst.issue_date
    if issue is not None and issue <= ctx.as_of:
        f = _flows(ctx, inst, issue)
        if f is not None:
            hz, missing, info = m.hazard(issue, True)  # type: ignore[call-arg]
            s = solve_spread(f, ctx.book.curve(issue), inst.issue_price, hz, m.recovery)  # type: ignore[arg-type]
            return issue, s.value, {"how": f"solved at issue to price {inst.issue_price:g}", "date": issue.isoformat(),
                                    "comp": inst.comp["id"] if inst.comp else None, **info,
                                    **({"bound": s.bound} if s.bound else {}),
                                    **({"missingAtIssue": missing} if missing else {})}
    for o in obs:
        f = _flows(ctx, inst, o.day)
        if f is None:
            continue
        hz, missing, info = m.hazard(o.day, True)  # type: ignore[call-arg]
        s = solve_spread(f, ctx.book.curve(o.day), o.price, hz, m.recovery)  # type: ignore[arg-type]
        return o.day, s.value, {"how": f"solved on the first observation ({o.source_name})", "date": o.day.isoformat(),
                                **info, **({"missingAtIssue": missing} if missing else {})}
    return None


def readings(ctx: Ctx, inst: Instrument, m: Model, lp: float, obs: list[Obs], days: list[date]) -> list[Reading]:
    index = {d: i for i, d in enumerate(days)}
    out = []
    for o in obs:
        if o.source == "dealer" and ctx.exclude_dealer:
            continue
        k = index.get(o.day)
        if k is None:
            prior = [d for d in days if d <= o.day]
            if not prior:
                continue
            k = index[prior[-1]]
        f = _flows(ctx, inst, o.day)
        if f is None:
            continue
        hz, _, _ = m.hazard(o.day)
        if hz is None:
            continue
        curve = ctx.book.curve(o.day)
        assert curve is not None
        s = solve_spread(f, curve, o.price, hz, m.recovery)
        dv = max(_spread_dv(f, ctx, o.day, hz, m.recovery, s.value), 1e-3)
        sd = o.noise / dv * 1e-4
        stale = o.stale and inst.kind != "bond"
        out.append(Reading(k, s.value - lp, sd**2, stale, f"{o.source} {o.day} {o.price:.3f}"))
    return out


def price(ctx: Ctx, inst: Instrument, ch: Chain | None, obs: list[Obs]) -> dict[str, Any]:
    """The 'price' model output of one instrument (types: see marden.models.price)."""
    missing = list(inst.terms_missing) + (list(ch.missing) if ch else [])
    out: dict[str, Any] = {"price": None, "spreadBp": None, "yieldPct": None, "recovery": None, "change1w": None,
                           "band": None, "inputs": [], "missing": missing}
    f_now = _flows(ctx, inst, ctx.as_of) if inst.terms else None
    if f_now is None:
        missing.append(f"instrument {inst.id}: terms, curve or SOFR for {ctx.as_of}" if inst.terms else "terms")
        return out
    if inst.issue_date is not None and inst.issue_date > ctx.as_of:  # a backtest never sees it before it existed
        missing.append(f"instrument {inst.id}: issued {inst.issue_date}, after {ctx.as_of}")
        return out
    if inst.issue_date is None and not obs:
        missing.append(f"instrument {inst.id}: no issue date and no observation public by {ctx.as_of}")
        return out
    m = model_for(ctx, inst, ch)
    cal = calibrate(ctx, inst, m, obs)
    curve = ctx.book.curve(ctx.as_of)
    assert curve is not None
    if cal is None:
        return _tier_price(ctx, inst, ch, f_now, out)
    cal_day, lp, cal_basis = cal
    days = ctx.book.business_days(cal_day, ctx.as_of) or [ctx.as_of]
    if days[-1] != ctx.as_of:
        days = [*days, ctx.as_of]
    rd = readings(ctx, inst, m, lp, [o for o in obs if o.day >= cal_day], days)
    p0 = CALIBRATED_SD**2 if m.mode == "facility" else 0.02**2
    filt = kalman(len(days), 0.0, p0, Q_RESID, rd)
    r, r_var = filt.at(len(days) - 1)
    hz, miss_now, info = m.hazard(ctx.as_of)
    missing.extend(miss_now)
    if hz is None:
        return out
    px = clean_price(f_now, curve, hz, m.recovery, lp + r)
    sd = math.sqrt(r_var + _tenant_var(info, ch))
    lo = clean_price(f_now, curve, hz, m.recovery, lp + r + Z80 * sd)
    hi = clean_price(f_now, curve, hz, m.recovery, lp + r - Z80 * sd)
    week = _week_ago(ctx, inst, m, lp, filt, days)
    rec = m.recovery if isinstance(m.recovery, float) else float(m.recovery(np.array([0.0]))[0])
    out.update({
        "price": round(px, 3), "spreadBp": _round(g_spread_bp(f_now, curve, px), 1),
        "yieldPct": _round(yield_to_maturity(f_now, px), 3), "recovery": round(rec, 3),
        "change1w": None if week is None else round(px - week, 3),
        "band": {"low": round(min(lo, hi), 3), "high": round(max(lo, hi), 3), "level": 0.8, "label": "model v0"},
    })
    out["basis"] = {
        "mode": m.mode, "calibration": {**cal_basis, "lpBp": round(lp * 1e4, 1)},
        "residualBp": round(r * 1e4, 1), "residualSdBp": round(math.sqrt(r_var) * 1e4, 1),
        "observationsUsed": len(filt.used), "lastObservation": filt.used[-1].label if filt.used else None,
        "today": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in info.items()},
        "chain": ch.doc() if ch else None, "terms": _terms_doc(inst),
        "defaultOddsToMaturity": round(_default_odds(hz, f_now.maturity_years), 4),
    }
    out["_week_ago_price"] = week
    return out


def _default_odds(hz: Any, years: float) -> float:
    """1 - S(T) under the day's intensity path (risk-neutral, uncalibrated)."""
    grid = np.linspace(0.0, max(years, 1e-6), 241)
    dt = np.diff(grid)
    lam = np.full(len(dt), float(hz)) if not callable(hz) else np.asarray(hz(grid[:-1] + dt / 2.0), dtype=float)
    return float(1.0 - math.exp(-float(np.sum(lam * dt))))


def _tenant_var(info: dict[str, Any], ch: Chain | None) -> float:
    """Spread variance from the tenant's (or issuer's) intensity uncertainty: (pass x lambda x LGD x sd)^2."""
    lam, sd = info.get("tenantHazard") or info.get("issuerHazard"), info.get("tenantLogSd") or info.get("issuerLogSd")
    if lam is None or sd is None:
        return 0.0
    return (0.5 * lam * (1 - RECOVERY) * sd) ** 2 if ch else 0.0


def _week_ago(ctx: Ctx, inst: Instrument, m: Model, lp: float, filt: Any, days: list[date]) -> float | None:
    target = ctx.as_of - timedelta(days=7)
    prior = [i for i, d in enumerate(days) if d <= target]
    if not prior:
        return None
    k = prior[-1]
    day = days[k]
    f = _flows(ctx, inst, day)
    hz, _, _ = m.hazard(day)
    if f is None or hz is None:
        return None
    r, _ = filt.at(k)
    return clean_price(f, ctx.book.curve(day), hz, m.recovery, lp + r)  # type: ignore[arg-type]


def _tier_price(ctx: Ctx, inst: Instrument, ch: Chain | None, f: Flows, out: dict[str, Any]) -> dict[str, Any]:
    from marden.models.price import instrument_tier

    tier = instrument_tier(inst, ch)
    curve = ctx.book.curve(ctx.as_of)
    spread = ctx.tiers.spread(tier, f.maturity_years) if (ctx.tiers and tier) else None
    if spread is None or curve is None:
        out["missing"].append(f"instrument {inst.id}: no issue comp, no observation and no tier curve for its tier")
        return out
    y = curve.par_yield(f.maturity_years) / 100.0 + spread / 1e4
    px = float(np.sum(f.amounts / (1 + y / 2) ** (2 * f.times))) - f.accrued
    err = ctx.tiers.loo_rmse if ctx.tiers else float("nan")
    lo_y, hi_y = y + Z80 * err / 1e4, y - Z80 * err / 1e4
    lo = float(np.sum(f.amounts / (1 + lo_y / 2) ** (2 * f.times))) - f.accrued
    hi = float(np.sum(f.amounts / (1 + hi_y / 2) ** (2 * f.times))) - f.accrued
    out.update({"price": round(px, 3), "spreadBp": round(spread, 1), "yieldPct": round(y * 100, 3),
                "recovery": None,
                "band": {"low": round(lo, 3), "high": round(hi, 3), "level": 0.8, "label": "model v0"}})
    out["basis"] = {"mode": "tier curve", "tier": tier, "terms": _terms_doc(inst), "chain": ch.doc() if ch else None,
                    "looRmseBp": round(err, 1)}
    out["missing"].append("issue comp and observations: price read off the tier curve (wide band)")
    return out


def _terms_doc(inst: Instrument) -> dict[str, Any] | None:
    t = inst.terms
    if t is None:
        return None
    return {"maturity": t.maturity.isoformat(), "couponType": t.coupon_type, "couponPct": t.coupon_pct,
            "marginBp": t.margin_bp, "frequency": t.frequency, "assumptions": list(t.assumptions),
            "issueDate": inst.issue_date.isoformat() if inst.issue_date else None,
            "issuePrice": inst.issue_price if inst.comp else None}


def _round(v: float | None, n: int) -> float | None:
    return None if v is None or math.isnan(v) else round(v, n)

