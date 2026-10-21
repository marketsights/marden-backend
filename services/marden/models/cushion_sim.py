"""10,000 simulated schedules per site (spec 22 "Cushion"), numpy only, seeded from the inputs hash so the
same inputs always give the same output.

Months are floats (month index + fraction). For each pending milestone with a planned window [lo, hi]:
the plan P is uniform over the window (the part not yet past; a window wholly past means the milestone is
overdue and P is the run date), the slip ratio r is a smoothed bootstrap draw from the slip history
(Silverman bandwidth; a ratio is not allowed below -0.9) and the simulated finish is
max(P + r x max(P - today, 3 months), today). Done milestones are fixed at their month. A milestone no source
dates is not simulated and adds no constraint. Each milestone then finishes no earlier than its
predecessors (finish = max(own, predecessors); no lags are assumed between milestones). Rent start is the
critical-path finish of 'rent_start'.

Driver: in each simulation the milestone that set the rent-start date (followed back through the
predecessors) is charged with that run's delay against the plan (window midpoints, no slip); the driver is
the spec 03 category (Power / Build / Delivery / Permits) of the milestone charged the most months, or of
the milestone that binds most often when nothing is late.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from marden.models import cushion_dates as cd
from marden.models.cushion_history import MIN_HORIZON, SlipHistory, month_float
from marden.models.cushion_milestones import CATEGORY, DRIVER, KEYS, Milestone

N_SIMS = 10_000
MIN_RATIO = -0.9


@dataclass
class SimResult:
    rent: np.ndarray
    finish: dict[str, np.ndarray]
    simulated: list[str]
    slip_basis: dict[str, str]
    driver: str
    binding_share: dict[str, float]
    delay_months: dict[str, float]
    unsimulated: list[str] = field(default_factory=list)


def smoothed_bootstrap(ratios: list[float], n: int, rng: np.random.Generator) -> np.ndarray:
    values = np.asarray(ratios, dtype=float)
    draws = values[rng.integers(0, len(values), n)]
    if len(values) > 1:
        sd = float(values.std(ddof=1))
        q75, q25 = np.percentile(values, [75, 25])
        spread = min(sd, (q75 - q25) / 1.34) if q75 > q25 else sd
        bandwidth = 0.9 * spread * len(values) ** -0.2
        if bandwidth > 0:
            draws = draws + bandwidth * rng.standard_normal(n)
    return np.maximum(draws, MIN_RATIO)


def _done_month(ms: Milestone) -> float | None:
    return ms.done.hi + 0.5 if ms.done is not None else None


def simulate(milestones: list[Milestone], as_of: date, history: SlipHistory, slug: str, seed: int,
             n: int = N_SIMS) -> SimResult | None:
    rng = np.random.default_rng(seed)
    today = month_float(as_of)
    by_key = {m.key: m for m in milestones if m.applicable}
    own: dict[str, np.ndarray | None] = {}
    plan: dict[str, float | None] = {}
    basis: dict[str, str] = {}
    unsimulated: list[str] = []
    for key in KEYS:
        ms = by_key.get(key)
        if ms is None:
            continue
        if ms.is_done:
            done = _done_month(ms)
            own[key] = None if done is None else np.full(n, done)
            plan[key] = done
            continue
        if ms.planned is None:
            own[key], plan[key] = None, None
            if key not in ("rent_start",):
                unsimulated.append(key)
            continue
        lo, hi = float(ms.planned.lo), float(ms.planned.hi + 1)
        lo_eff = max(lo, today)
        hi_eff = max(hi, lo_eff)
        planned = rng.uniform(lo_eff, hi_eff, n) if hi_eff > lo_eff else np.full(n, lo_eff)
        plan[key] = (lo_eff + hi_eff) / 2
        ratios, basis[key] = history.pool(CATEGORY[key], slug)
        if not ratios:
            own[key] = None
            unsimulated.append(key)
            continue
        slip = smoothed_bootstrap(ratios, n, rng)
        own[key] = np.maximum(planned + slip * np.maximum(planned - today, MIN_HORIZON), today)

    finish: dict[str, np.ndarray] = {}
    arg: dict[str, tuple[list[str], np.ndarray]] = {}
    plan_finish: dict[str, float] = {}
    for key in KEYS:
        ms = by_key.get(key)
        if ms is None:
            continue
        own_k = own.get(key)
        if ms.is_done:
            if own_k is not None:
                finish[key], arg[key] = own_k, ([key], np.zeros(n, dtype=int))
                plan_finish[key] = plan[key]  # type: ignore[assignment]
            continue
        names, parts, plans = [], [], []
        if own_k is not None:
            names.append(key)
            parts.append(own_k)
            plans.append(plan[key])
        for p in ms.preds:
            if p in finish:
                names.append(p)
                parts.append(finish[p])
                plans.append(plan_finish[p])
        if not parts:
            continue
        stack = np.vstack(parts)
        finish[key] = stack.max(axis=0)
        arg[key] = (names, stack.argmax(axis=0))
        plan_finish[key] = max(x for x in plans if x is not None)
    if "rent_start" not in finish:
        return None

    binding = _binding(arg, "rent_start", n)
    delay = np.maximum(finish["rent_start"] - plan_finish["rent_start"], 0.0)
    delay_by: dict[str, float] = {}
    share: dict[str, float] = {}
    counts = Counter(binding.tolist())
    for key, count in counts.items():
        share[key] = count / n
        delay_by[key] = float(delay[binding == key].sum() / n)
    late = {k: v for k, v in delay_by.items() if v > 1e-9}
    top = max(late, key=late.get) if late else max(share, key=share.get)  # type: ignore[arg-type]
    simulated = [k for k in KEYS if k in by_key and not by_key[k].is_done and own.get(k) is not None]
    return SimResult(finish["rent_start"], finish, simulated, basis, DRIVER[top],
                     {k: round(v, 4) for k, v in sorted(share.items())},
                     {k: round(v, 3) for k, v in sorted(delay_by.items())}, unsimulated)


def _binding(arg: dict[str, tuple[list[str], np.ndarray]], root: str, n: int) -> np.ndarray:
    """For each simulation, the milestone whose own date set the root's finish."""
    cur = np.full(n, root, dtype=object)
    for _ in range(len(KEYS) + 1):
        changed = False
        for key, (names, idx) in arg.items():
            mask = (cur == key) & (idx != 0) if names[0] == key else (cur == key)
            if not mask.any():
                continue
            for i, name in enumerate(names):
                if name == key:
                    continue
                sel = mask & (idx == i)
                if sel.any():
                    cur[sel] = name
                    changed = True
        if not changed:
            break
    return cur


def months_of(values: np.ndarray) -> dict[str, str]:
    p10, p50, p90 = np.percentile(values, [10, 50, 90])
    return {"p10": cd.ym(int(np.floor(p10))), "p50": cd.ym(int(np.floor(p50))), "p90": cd.ym(int(np.floor(p90)))}


def odds_after(values: np.ndarray, month: int) -> float:
    """Share of simulated dates after the end of `month`."""
    return float(np.mean(values >= month + 1))
