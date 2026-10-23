"""The Marden model price of one instrument on one day (pricing-v0.1 "The model" and "Calibration").

Two modes:
- issuer bond: a straight bond of an issuer with a bond-implied curve (price_issuer). Its intensity on a
  day is the issuer curve at the bond's maturity, no liquidity premium, recovery the issuer's unsecured
  recovery from its balance-sheet waterfall (price v0.3; the curve is solved at the same recovery), else
  40% (the convention, with why).
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
from marden.models.price_chain import (
    Chain,
    IssuerRecovery,
    Legs,
    hazard_fn,
    recovery_basis,
    recovery_doc,
    recovery_fn,
)
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
RECOVERY_STEP = 0.10  # recoverySensitivity: the price change per 10c of recovery


@dataclass
class Ctx:
    conn: Conn
    as_of: date
    book: CurveBook
    curves: dict[str, IssuerCurve]
    tiers: TierFit | None
    signals: dict[str, list[sig.Signal]] = field(default_factory=dict)
    exclude_dealer: bool = False
    issuer_recoveries: dict[str, IssuerRecovery] = field(default_factory=dict)

    def issuer_recovery(self, entity: str | None) -> IssuerRecovery | None:
        return self.issuer_recoveries.get(entity or "")

    def bond_recovery(self, entity: str | None) -> float:
        """The recovery an issuer's bonds are priced (and its curve solved) at."""
        cur = self.curves.get(entity or "")
        return cur.recovery if cur is not None else RECOVERY


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

        return Model("issuer bond", issuer_hazard, ctx.bond_recovery(inst.issuer))

    seen: dict[tuple[date, bool], tuple[Callable[[np.ndarray], np.ndarray], list[str], dict[str, Any]]] = {}

    def chain_hazard(day: date, backfill: bool = False) -> tuple[Callable[[np.ndarray], np.ndarray], list[str],
                                                                  dict[str, Any]]:
        # cached per day: the recovery sensitivity re-runs the calibration and the filter on the same days
        if (day, backfill) not in seen:
            lg, missing, info = legs(ctx, ch, day, backfill)
            seen[(day, backfill)] = hazard_fn(ch, lg, day), missing, info
        hz, missing, info = seen[(day, backfill)]
        return hz, list(missing), dict(info)

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
                           "band": None, "inputs": [], "missing": missing, "recoveryBasis": None}
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
    days, filt = _filtered(ctx, inst, m, obs, cal_day, lp)
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
    # Default odds need a credit leg: with no tenant, obligor or guarantor intensity the hazard holds only
    # the completion terms and the credit risk sits in the calibrated spread, so the odds are n/d, never 0.
    credit_leg = m.mode != "facility" or any(f"{r}Hazard" in info for r in ("tenant", "obligor", "guarantor"))
    if not credit_leg:
        missing.append(f"instrument {inst.id}: default odds n/d (no tenant, obligor or guarantor bond-implied "
                       "intensity; credit risk is carried by the spread at issue)")
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
        "defaultOddsToMaturity": round(_default_odds(hz, f_now.maturity_years), 4) if credit_leg else None,
    }
    issuer = ctx.issuer_recovery(inst.issuer) if ch is None else None
    out["recoveryBasis"] = recovery_basis(ch, issuer)
    out["recoveryInputs"] = recovery_doc(ch, rec, _version(), RECOVERY, issuer)
    out["recoverySensitivity"] = _recovery_sensitivity(ctx, inst, ch, m, obs, f_now, hz, lp + r, rec, px)
    out["_week_ago_price"] = week
    return out


def _version() -> str:
    from marden.models.price import VERSION

    return VERSION


def _filtered(ctx: Ctx, inst: Instrument, m: Model, obs: list[Obs], cal_day: date, lp: float) -> tuple[list[date], Any]:
    """The business days from calibration to the as-of day and the residual filter over them."""
    days = ctx.book.business_days(cal_day, ctx.as_of) or [ctx.as_of]
    if days[-1] != ctx.as_of:
        days = [*days, ctx.as_of]
    rd = readings(ctx, inst, m, lp, [o for o in obs if o.day >= cal_day], days)
    p0 = CALIBRATED_SD**2 if m.mode == "facility" else 0.02**2
    return days, kalman(len(days), 0.0, p0, Q_RESID, rd)


def _recovery_sensitivity(ctx: Ctx, inst: Instrument, ch: Chain | None, m: Model, obs: list[Obs], f_now: Flows,
                          hz: Any, spread: float, rec0: float, px: float) -> dict[str, Any]:
    """Price change per 10c of recovery (dP/dR x 0.10), points, by central difference of +-5c (one-sided at the
    0c / 100c bounds). `per10cPts`: the whole model re-run with the shifted recovery, so the liquidity
    premium re-solves to the issue price; `holdingSpreadPer10cPts`: today's price at today's spread."""
    curve = ctx.book.curve(ctx.as_of)
    assert curve is not None
    half = RECOVERY_STEP / 2
    up, down = (min(half, 1.0 - rec0), -min(half, rec0))
    if up - down < 1e-6:
        return {"per10cPts": None, "holdingSpreadPer10cPts": None, "why": "recovery at a bound"}
    if up < half:
        down = -min(RECOVERY_STEP - up, rec0)
    elif -down < half:
        up = min(RECOVERY_STEP + down, 1.0 - rec0)
    width = up - down

    def shifted(s: float) -> Any:
        return recovery_fn(ch, s) if ch is not None else min(max(rec0 + s, 0.0), 1.0)

    hold = [clean_price(f_now, curve, hz, shifted(s), spread) for s in (down, up)]
    out: dict[str, Any] = {"holdingSpreadPer10cPts": round((hold[1] - hold[0]) / width * RECOVERY_STEP, 3),
                           "stepCents": round(width * 100, 1), "pricedAt": round(px, 3)}
    if m.mode != "facility" or ch is None:
        out.update({"per10cPts": None, "why": f"issuer bond: its default intensity is solved from the issuer's bond "
                                               f"prices at the same {rec0:.0%} recovery, so the model price does not "
                                               "move with it; holdingSpread is at today's intensity"})
        return out
    full: list[float] = []
    for s in (down, up):
        m_s = Model(m.mode, m.hazard, shifted(s))
        cal = calibrate(ctx, inst, m_s, obs)
        if cal is None:
            out.update({"per10cPts": None, "why": "no calibration"})
            return out
        days, filt = _filtered(ctx, inst, m_s, obs, cal[0], cal[1])
        full.append(clean_price(f_now, curve, hz, m_s.recovery, cal[1] + filt.at(len(days) - 1)[0]))
    out.update({"per10cPts": round((full[1] - full[0]) / width * RECOVERY_STEP, 3),
                "how": "price re-run with the recovery 5c lower and higher (one-sided at a bound), liquidity "
                       "premium re-solved at calibration and residual re-filtered; per 10c"})
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
    out["recoveryBasis"] = None
    out["recoveryInputs"] = {"basis": None, "why": "price read off the tier curve: no recovery is used"}
    out["recoverySensitivity"] = {"per10cPts": 0.0, "holdingSpreadPer10cPts": 0.0,
                                  "why": "price read off the tier curve: recovery does not enter it"}
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

