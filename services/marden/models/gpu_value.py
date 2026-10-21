"""GPU price index and value per unit (spec 19 "The model (v0)"): pure functions on a gpu_inputs.Market.

1. Index: per capture day, model, term and region, the median of the providers' published prices with the
   25th / 75th percentiles and the provider count (GpuPricePoint). Every captured price is an on-demand
   list price with no region stated, so term = 'on-demand' and region = 'all'.
2. Decline as newer generations ship: spec 19 fits it from older models' price paths; Marden holds no
   rental price history yet (the Silicon Data series are a licensed product and not loaded), so v0 fits
   it across models on the newest capture day: ln(median price) = c + b x age since first volume
   shipment, least squares over every model with a price and a launch date; annual decline = 1 - e^b.
3. Value per unit = PV of rent over the unit's remaining useful life, rent declining continuously at b:
       V = u x 8760 x P x e^{b s} x A(T),   A(T) = (1 - e^{-(rho - b) T}) / (rho - b),  rho = ln(1 + r)
   P the model's index price rolled from its capture day to the valuation day at b (the fitted line
   c + b x age for a model with no captured price), s the years ahead of the valuation day, T the
   remaining life (useful life - unit age - s), r the Treasury par yield at T plus the median issue
   spread of GPU loans (the tier rate).
4. u, the share of on-demand list rent the owner realizes (utilization x contract discounts, net of
   operating costs; spec 19's "expected utilization"), is not assumed: it is implied by the new-unit
   prices. For each model with an index price, a launch date and a published new price N, a unit new at
   launch is worth N: u_m = N / (8760 x P e^{-b x age} x A(L)); u = the median u_m.
Assumptions are locked here and in docs/methods/gpu-collateral-v0.1.md; outputs carry 'model v0' until
the values are back-tested against at least 50 resale prices per model (spec 19 Acceptance).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np

from marden.models.gpu_inputs import ListPrice, Market

HOURS_PER_YEAR = 8760.0
HAIRCUT = 0.20  # liquidation haircut on collateral: time to resell, removal, transport (locked v0)
TERM, REGION = "on-demand", "all"
MIN_FIT_MODELS = 3
DAYS_PER_YEAR = 365.25


def years(a: date, b: date) -> float:
    return (b - a).days / DAYS_PER_YEAR


def _pct(values: list[float], q: float) -> float:
    return float(np.percentile(np.array(values, dtype=float), q))


def index_points(prices: list[ListPrice]) -> list[dict[str, Any]]:
    """GpuPricePoint[] for every capture day and model, oldest first."""
    groups: dict[tuple[date, str], dict[str, float]] = defaultdict(dict)
    for p in prices:
        groups[(p.captured, p.model)][p.provider] = p.usd_hr  # one price per provider and day
    out = []
    for (day, model), by_provider in sorted(groups.items()):
        values = sorted(by_provider.values())
        out.append({"date": day.isoformat(), "model": model, "term": TERM, "region": REGION,
                    "medianUsdHr": round(_pct(values, 50), 4), "p25": round(_pct(values, 25), 4),
                    "p75": round(_pct(values, 75), 4), "nProviders": len(values)})
    return out


@dataclass
class Fit:
    """The cross-sectional decline and the implied realized share of list rent."""

    slope: float  # per year, ln price
    intercept: float
    n: int
    models: list[str]
    r2: float
    captured: date
    life: float | None = None  # the useful life the realization was implied with
    realization: float | None = None  # u: median over models of the implied share of list rent
    implied: dict[str, float] = field(default_factory=dict)  # model -> u_m

    @property
    def annual_decline(self) -> float:
        return 1.0 - math.exp(self.slope)


def latest_medians(market: Market) -> dict[str, tuple[float, date]]:
    """Each model's newest index median and its capture day."""
    out: dict[str, tuple[float, date]] = {}
    for pt in index_points(market.prices):
        out[pt["model"]] = (pt["medianUsdHr"], date.fromisoformat(pt["date"]))
    return out


def discount_rate(market: Market, years_left: float) -> float | None:
    """Tier rate: UST par yield at the remaining life plus the median GPU-loan issue spread (decimal)."""
    ust, spread = market.ust_yield(max(years_left, 1 / 12)), market.gpu_loan_spread_bp()
    if ust is None or spread is None:
        return None
    return (ust + spread / 100.0) / 100.0


def annuity(slope: float, rate: float, years_left: float) -> float:
    """A(T): PV of a rent of 1 per year declining continuously at `slope`, over `years_left`."""
    if years_left <= 0:
        return 0.0
    k = math.log1p(rate) - slope
    return years_left if abs(k) < 1e-12 else (1.0 - math.exp(-k * years_left)) / k


def _annuity_at(market: Market, slope: float, years_left: float) -> float | None:
    rate = discount_rate(market, max(years_left, 0.0))
    return None if rate is None else annuity(slope, rate, years_left)


def fit_decline(market: Market, life: float | None = None) -> Fit | None:
    """Decline across models on the newest capture day (None with fewer than three models), and, with a
    useful life, the realized share of list rent implied by the new-unit prices."""
    medians = latest_medians(market)
    if not medians:
        return None
    day = max(d for _, d in medians.values())
    xs, ys, names = [], [], []
    for model, (price, captured) in sorted(medians.items()):
        chip = market.chips.get(model)
        launch = chip.launch if chip else None
        if captured != day or launch is None or launch > day:
            continue
        xs.append(years(launch, day))
        ys.append(math.log(price))
        names.append(model)
    if len(xs) < MIN_FIT_MODELS:
        return None
    slope, intercept = np.polyfit(np.array(xs), np.array(ys), 1)
    if slope >= 0:
        return None
    pred = intercept + slope * np.array(xs)
    ss_res = float(np.sum((np.array(ys) - pred) ** 2))
    ss_tot = float(np.sum((np.array(ys) - np.mean(ys)) ** 2))
    fit = Fit(float(slope), float(intercept), len(xs), names, 1 - ss_res / ss_tot if ss_tot else 0.0, day, life)
    if life is not None:
        a_life = _annuity_at(market, fit.slope, life)
        for model, age in zip(names, xs, strict=True):
            new = market.chips[model].new_price
            if new and a_life:
                launch_rent = medians[model][0] * math.exp(-fit.slope * age)
                fit.implied[model] = new / (HOURS_PER_YEAR * launch_rent * a_life)
        if fit.implied:
            fit.realization = float(np.median(list(fit.implied.values())))
    return fit


def rent_on(market: Market, fit: Fit, model: str, day: date) -> tuple[float, str] | None:
    """The model's rent on `day`: its index price rolled from the capture day at the decline, else the
    fitted line at its age. Returns (USD per GPU-hour, basis)."""
    got = latest_medians(market).get(model)
    if got is not None:
        price, captured = got
        return price * math.exp(fit.slope * years(captured, day)), "index"
    chip = market.chips.get(model)
    if chip is None or chip.launch is None or chip.launch > day:
        return None
    return math.exp(fit.intercept + fit.slope * years(chip.launch, fit.captured)) * math.exp(
        fit.slope * years(fit.captured, day)), "fitted line"


def unit_value(market: Market, fit: Fit, model: str, day: date, life_years: float, unit_age_years: float,
               ahead_years: float = 0.0) -> float | None:
    """Value per unit `ahead_years` after `day` for a unit `unit_age_years` old on `day` (USD)."""
    rent = rent_on(market, fit, model, day)
    left = life_years - unit_age_years - ahead_years
    a = _annuity_at(market, fit.slope, left)
    if rent is None or a is None or fit.realization is None:
        return None
    return fit.realization * HOURS_PER_YEAR * rent[0] * math.exp(fit.slope * ahead_years) * a


def value_by_age(market: Market, fit: Fit, model: str, day: date, life_years: float) -> list[tuple[int, float]]:
    """Value of a unit of `model` bought new at launch, at each whole year of age 0..life (USD), with the
    rent of each age on today's curve."""
    chip = market.chips.get(model)
    rent = rent_on(market, fit, model, day)
    if chip is None or chip.launch is None or rent is None or fit.realization is None:
        return []
    age_today = years(chip.launch, day)
    out = []
    for age in range(0, math.ceil(life_years) + 1):
        a = _annuity_at(market, fit.slope, life_years - age)
        if a is None:
            return []
        out.append((age, fit.realization * HOURS_PER_YEAR * rent[0] * math.exp(fit.slope * (age - age_today)) * a))
    return out


def resale_check(market: Market, fit: Fit, model: str, day: date, life_years: float) -> dict[str, Any] | None:
    """The newest resale observation against the model value, on that date, of a unit bought at launch."""
    rows = market.resale.get(model) or []
    chip = market.chips.get(model)
    if not rows or chip is None or chip.launch is None:
        return None
    row = rows[-1]
    observed = float(row["value"])
    when = row["valid_from"]
    v = unit_value(market, fit, model, when, life_years, years(chip.launch, when))
    return {"date": when.isoformat(), "resaleUsd": observed, "modelUsd": None if v is None else round(v),
            "modelOverResale": None if v is None or observed <= 0 else round(v / observed, 3),
            "condition": None, "sourceUrl": row.get("source_url")}
