"""Tier curves (docs/methods/pricing-v0.1.md "Tier curves"): comparable spreads by tier from deal pricing at
issue, moving with the HY market.

    spread over Treasuries = tier level + b x (ICE BofA HY OAS - its sample mean) + c x NS(T),
    NS(T) = (1 - exp(-T/tau)) / (T/tau), tau = 3y (Nelson-Siegel short-end loading)

fit by ridge regression on the issue comps public on the run's as-of day, issued from SAMPLE_START on
(the methodology's sample starts May 2025). Floating deals convert to a Treasury-equivalent spread with
SOFR minus the Treasury par yield at the deal's tenor on the issue date; fixed deals carry the yield-over-
Treasury spread at issue, as stated (F/R) or else computed in code from the coupon, issue price and tenor
(marden.models.issue_spreads; the legacy archive's own computed spreads, tag M, are never used).
The ridge shrinks each tier level toward the pooled mean (RIDGE_LEVEL deal
equivalents) and b toward the methodology's 33bp per point (RIDGE_HY).

The HY input (pricing-v0.1 "Inputs when FRED is unavailable"): the ICE BofA US High Yield OAS (series
ice_bofa_us_hy_oas, FRED, unlicensed-commercial, model input only) on or within a week before each day; on a
day it has no value, the fund OAS of USHY (series etf_oas_ushy, collectors/etf_oas: iShares, tracks the ICE
BofA US High Yield Constrained Index), recorded in the inputs as a proxy. The ICE series wins whenever it has
a value. When every deal has an HY value at issue, b and the sample mean are fitted as above ('fitted').
When some deal has none (a proxy history only starts in October 2026) but the day has one, the levels are
fitted without the market term and the curve moves at the methodology's 33bp per point against the
methodology's stated sample-mean HY of 2.89% ('fixed'). With no HY value for the day the market term is
dropped and the curve says so.

Comps are put in the six tiers by fixed rules on their stored type, tenant tier and rating (`tier_of`);
data-center ABS/CMBS are not one of the six tiers and are left out. The fit reports the residual SD and
the leave-one-out RMSE (the error band for untraded facilities).
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np

from marden.core.db import Conn
from marden.models import pit
from marden.models.issue_spreads import issue_spread
from marden.models.price_curve import CurveBook
from marden.models.status_text import BBB_MINUS, NOTCH, NOTCH_MOODYS

TIERS = ("IG project bond", "Backstopped senior", "Contract-backed loan", "Construction loan", "Junior chip paper",
         "Neocloud bond")
SAMPLE_START = date(2025, 5, 1)
TAU = 3.0
HY_SERIES = "ice_bofa_us_hy_oas"
HY_PROXY = "etf_oas_ushy"  # collectors/etf_oas: USHY fund OAS, used only on a day the ICE series has no value
HY_PROXY_METHOD = "fund OAS of USHY, which tracks the ICE BofA US High Yield Constrained Index"
HY_COEF_PRIOR = 33.0  # bp per 1 point of HY OAS (methodology)
HY_MEAN_STATED = 2.89  # pct; pricing-v0.1 "Tier curves": levels are "bp at sample-mean HY spread 2.89%"
HY_MAX_LAG_DAYS = 7
RIDGE_LEVEL = 1.0
RIDGE_HY = 4.0
RIDGE_NS = 1.0
FIT_YEARS = (0.5, 1, 2, 3, 4, 5, 6, 7, 8, 10)
_JUNIOR = re.compile(r"junior|subordinat|mezz|second lien|2L\b|holdco|\bPIK\b|preferred", re.I)


def ns(t: float) -> float:
    x = max(t, 1e-6) / TAU
    return (1.0 - math.exp(-x)) / x


def rating_notch(rating: str | None) -> int | None:
    """Notch of a comp's stored rating ('A (S&P)', 'Ba2 (Moody's)'): its leading grade."""
    if not rating:
        return None
    grade = rating.split("(")[0].strip().split(" ")[0]
    return NOTCH.get(grade) or NOTCH_MOODYS.get(grade)


def tier_of(type_code: str | None, tenant_tier: str | None, rating: str | None, text: str = "") -> str | None:
    """The methodology tier of a deal, by fixed rules (None = not one of the six tiers)."""
    if type_code == "dc_abs":
        return None
    if type_code == "dc_construction_loan":
        return "Construction loan"
    if type_code == "gpu_lease" or _JUNIOR.search(text or ""):
        return "Junior chip paper"
    if tenant_tier == "back":
        return "Backstopped senior"
    if type_code in ("gpu_loan", "gpu_abs"):
        return "Junior chip paper" if tenant_tier in (None, "nd") else "Contract-backed loan"
    if type_code == "dc_project_bond":
        notch = rating_notch(rating)
        if notch is not None and notch <= BBB_MINUS and tenant_tier in ("aa", "ig", "orcl", "div"):
            return "IG project bond"
        if tenant_tier in ("aa", "ig", "orcl"):  # sub-IG notes on a strong lessee's lease
            return "Backstopped senior"
        return "Neocloud bond"
    return None


@dataclass(frozen=True)
class Deal:
    id: str
    tier: str
    issue_date: date
    years: float
    spread_bp: float  # Treasury-equivalent spread at issue
    hy: float | None
    basis: str
    hy_proxy: bool = False  # hy is the USHY fund OAS, not the ICE series


@dataclass
class TierFit:
    as_of: date
    deals: list[Deal]
    levels: dict[str, float]  # bp at the sample-mean HY
    hy_coef: float | None
    ns_coef: float
    hy_mean: float | None
    hy_now: float | None
    residual_sd: float
    loo_rmse: float
    excluded: list[dict[str, Any]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    hy_mode: str | None = None  # 'fitted' | 'fixed' | None (no market term)
    hy_input: dict[str, Any] | None = None  # the day's HY input as recorded in the model's inputs

    def spread(self, tier: str, years: float, hy: float | None = None) -> float | None:
        if tier not in self.levels:
            return None
        s = self.levels[tier] + self.ns_coef * ns(years)
        h = self.hy_now if hy is None else hy
        if self.hy_coef is not None and h is not None and self.hy_mean is not None:
            s += self.hy_coef * (h - self.hy_mean)
        return s


@dataclass(frozen=True)
class HyPoint:
    day: date
    value: float  # percent
    proxy: bool
    source_name: str | None
    source_url: str | None


@dataclass
class HyInputs:
    """The ICE BofA HY OAS and its USHY fund-OAS proxy by observation date, as public on the run day."""

    ice: dict[date, HyPoint] = field(default_factory=dict)
    proxy: dict[date, HyPoint] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.ice or self.proxy)

    def at(self, day: date, max_days: int = HY_MAX_LAG_DAYS) -> HyPoint | None:
        """The ICE value on or within `max_days` before `day`; when it has none, the proxy's; else None."""
        return _on_or_before(self.ice, day, max_days) or _on_or_before(self.proxy, day, max_days)


def load_hy(conn: Conn, as_of: date) -> HyInputs:
    out = HyInputs()
    for r in pit.fact_rows(conn, as_of, "series", [HY_SERIES, HY_PROXY], ["value"]):
        v = r["value"]
        if isinstance(v, int | float) and not isinstance(v, bool):
            proxy = r["subject_id"] == HY_PROXY
            (out.proxy if proxy else out.ice).setdefault(
                r["valid_from"], HyPoint(r["valid_from"], float(v), proxy, r.get("source_name"), r.get("source_url")))
    return out


def hy_series(conn: Conn, as_of: date) -> dict[date, float]:
    """The ICE BofA HY OAS alone (no proxy), percent by observation date."""
    return {d: p.value for d, p in load_hy(conn, as_of).ice.items()}


def _on_or_before(series: dict[date, HyPoint], day: date, max_days: int = HY_MAX_LAG_DAYS) -> HyPoint | None:
    found = [d for d in series if d <= day and (day - d).days <= max_days]
    return series[max(found)] if found else None


def hy_input(pt: HyPoint) -> dict[str, Any]:
    """The HY value a run used, as an entry of the model's inputs (never displayed: display_allowed false)."""
    doc: dict[str, Any] = {
        "key": "hyOasPct", "label": "ICE BofA US High Yield OAS" + (" (proxy: USHY fund OAS)" if pt.proxy else ""),
        "value": round(pt.value, 4), "unit": "%", "tag": "R", "asOf": pt.day.isoformat(),
        "sourceName": pt.source_name, "sourceUrl": pt.source_url, "series": HY_PROXY if pt.proxy else HY_SERIES,
        "proxy": pt.proxy, "displayAllowed": False,
    }
    if pt.proxy:
        doc.update(proxyLabel="proxy", proxyFor=HY_SERIES, method=HY_PROXY_METHOD)
    return doc


def deals(comps: Sequence[dict[str, Any]], book: CurveBook, hy: HyInputs,
          as_of: date) -> tuple[list[Deal], list[dict[str, Any]]]:
    out: list[Deal] = []
    excluded: list[dict[str, Any]] = []
    for c in comps:
        if c["issue_date"] < SAMPLE_START or c["issue_date"] > as_of:
            continue
        tier = tier_of(c["type_code"], c["tier"], c["rating"], f"{c.get('name') or ''} {c['rate']}")
        if tier is None:
            excluded.append({"id": c["id"], "why": f"type {c['type_code']} is not one of the six tiers"})
            continue
        years = c["tenor_years"]
        if years is None or years <= 0:
            excluded.append({"id": c["id"], "why": "no tenor"})
            continue
        got, basis = issue_spread(c["issue_spread_bp"], c.get("issue_spread_tag"), rate_text=c["rate"],
                                  coupon_pct=c["coupon_pct"], issue_price=c["issue_price"], tenor_years=years,
                                  curve=None if c["benchmark"] == "SOFR" else book.curve(c["issue_date"]))
        if got is None:
            excluded.append({"id": c["id"], "why": f"no spread at issue: {basis}"})
            continue
        spread = got
        if c["benchmark"] == "SOFR":
            curve, sofr = book.curve(c["issue_date"]), book.sofr_on(c["issue_date"])
            if curve is None or sofr is None:
                excluded.append({"id": c["id"], "why": "no Treasury curve or SOFR on the issue date"})
                continue
            adj = (sofr - curve.par_yield(years)) * 100.0
            spread += adj
            basis = f"SOFR margin {c['issue_spread_bp']:.0f}bp {adj:+.0f}bp (SOFR - UST {years:g}y)"
        at_issue = hy.at(c["issue_date"])
        out.append(Deal(c["id"], tier, c["issue_date"], float(years), spread,
                        None if at_issue is None else at_issue.value, basis,
                        at_issue is not None and at_issue.proxy))
    return out, excluded


def _design(ds: Sequence[Deal], use_hy: bool, hy_mean: float) -> np.ndarray:
    rows = []
    for d in ds:
        row = [1.0 if d.tier == t else 0.0 for t in TIERS]
        row.append(ns(d.years))
        if use_hy:
            row.append((d.hy or hy_mean) - hy_mean)
        rows.append(row)
    return np.array(rows)


def _solve(x: np.ndarray, y: np.ndarray, use_hy: bool, pooled: float) -> np.ndarray:
    k = len(TIERS)
    pen = np.zeros(x.shape[1])
    prior = np.zeros(x.shape[1])
    pen[:k] = RIDGE_LEVEL
    prior[:k] = pooled
    pen[k] = RIDGE_NS
    if use_hy:
        pen[k + 1] = RIDGE_HY
        prior[k + 1] = HY_COEF_PRIOR
    a = x.T @ x + np.diag(pen)
    b = x.T @ y + pen * prior
    return np.linalg.solve(a, b)


def fit(conn: Conn, as_of: date, comps: Sequence[dict[str, Any]], book: CurveBook) -> TierFit:
    hy = load_hy(conn, as_of)
    ds, excluded = deals(comps, book, hy, as_of)
    return fit_deals(as_of, ds, excluded, hy)


def fit_deals(as_of: date, ds: list[Deal], excluded: list[dict[str, Any]], hy: HyInputs) -> TierFit:
    """The ridge fit of the tiers on `ds`, with the market term 'fitted', 'fixed' or dropped (module doc)."""
    now = hy.at(as_of)
    use_hy = bool(ds) and all(d.hy is not None for d in ds)
    mode = "fitted" if use_hy else ("fixed" if now is not None else None)
    missing: list[str] = []
    if not hy:
        missing.append(f"series {HY_SERIES} (ICE BofA HY OAS via FRED) and its proxy {HY_PROXY} (USHY fund OAS): "
                       "the curve does not move with HY")
    elif now is None:
        missing.append(f"series {HY_SERIES} and {HY_PROXY}: no value within {HY_MAX_LAG_DAYS} days before "
                       f"{as_of.isoformat()}; market term dropped")
    elif not use_hy and hy.ice:
        missing.append(f"series {HY_SERIES}: no value on some issue dates; market term at {HY_COEF_PRIOR:g}bp per "
                       f"point against the methodology's {HY_MEAN_STATED}% sample mean, not fitted")
    if len(ds) < 3:
        return TierFit(as_of, ds, {}, None, 0.0, None, None, float("nan"), float("nan"), excluded,
                       [*missing, f"only {len(ds)} priced comps in the six tiers"])
    hy_mean = float(np.mean([d.hy for d in ds])) if use_hy else 0.0
    x = _design(ds, use_hy, hy_mean)
    y = np.array([d.spread_bp for d in ds])
    pooled = float(np.mean(y))
    beta = _solve(x, y, use_hy, pooled)
    resid = y - x @ beta
    loo = []
    for i in range(len(ds)):
        keep = np.arange(len(ds)) != i
        b_i = _solve(x[keep], y[keep], use_hy, float(np.mean(y[keep])))
        loo.append(float(y[i] - x[i] @ b_i))
    k = len(TIERS)
    present = {d.tier for d in ds}
    levels = {t: float(beta[i]) for i, t in enumerate(TIERS) if t in present}
    dof = max(len(ds) - len(present) - 1 - int(use_hy), 1)
    if mode == "fitted":
        coef, mean = float(beta[k + 1]), hy_mean
    elif mode == "fixed":
        coef, mean = HY_COEF_PRIOR, HY_MEAN_STATED
    else:
        coef = mean = None
    return TierFit(as_of, ds, levels, coef, float(beta[k]), mean, None if now is None or mode is None else now.value,
                   float(math.sqrt(np.sum(resid**2) / dof)), float(math.sqrt(np.mean(np.square(loo)))), excluded,
                   missing, mode, None if now is None or mode is None else hy_input(now))


def curve_doc(tf: TierFit, book: CurveBook, points: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """CurveTier[] (types.ts): per tier its instrument points and the fitted yield curve."""
    curve = book.curve(tf.as_of)
    out = []
    for tier in TIERS:
        fit_pts = []
        if curve is not None and tier in tf.levels:
            for years in FIT_YEARS:
                s = tf.spread(tier, years)
                if s is not None:
                    fit_pts.append({"years": years, "yieldPct": round(curve.par_yield(years) + s / 100.0, 3)})
        pts = sorted(points.get(tier, []), key=lambda p: p["years"])
        if fit_pts or pts:
            out.append({"tier": tier, "points": pts, "fit": fit_pts})
    return out


HY_BASIS = {
    "fitted": "b fitted (ridge toward 33bp per point) on each deal's HY OAS at issue, against the deals' mean",
    "fixed": (f"b = {HY_COEF_PRIOR:g}bp per point (methodology), against the methodology's stated sample-mean HY "
              f"of {HY_MEAN_STATED}% (pricing-v0.1 Tier curves): the deals' HY at issue is not available, so the "
              "levels are fitted without the market term and taken to sit at that mean"),
}


def fit_doc(tf: TierFit) -> dict[str, Any]:
    """The fit's basis for the curve output and the method page."""
    return {
        "levelsBp": {t: round(v, 1) for t, v in tf.levels.items()},
        "deals": {t: sum(1 for d in tf.deals if d.tier == t) for t in TIERS},
        "hyCoefBpPerPt": None if tf.hy_coef is None else round(tf.hy_coef, 1),
        "hyMeanPct": tf.hy_mean, "hyNowPct": tf.hy_now, "hyMode": tf.hy_mode,
        "hyBasis": HY_BASIS.get(tf.hy_mode or ""),
        "inputs": [tf.hy_input] if tf.hy_input else [],
        "hyProxyDeals": sum(1 for d in tf.deals if d.hy_proxy),
        "nsCoefBp": round(tf.ns_coef, 1), "tauYears": TAU,
        "residualSdBp": None if math.isnan(tf.residual_sd) else round(tf.residual_sd, 1),
        "looRmseBp": None if math.isnan(tf.loo_rmse) else round(tf.loo_rmse, 1),
        "sample": [{"id": d.id, "tier": d.tier, "issueDate": d.issue_date.isoformat(), "years": d.years,
                    "spreadBp": round(d.spread_bp, 1), "basis": d.basis} for d in tf.deals],
        "excluded": tf.excluded,
    }
