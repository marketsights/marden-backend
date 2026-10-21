"""In-process re-pricing with shocked inputs, for the stress and contagion models (spec 22 "Stress: shock
applied link by link, then cushion and price re-run").

`Book.build(conn, day)` loads what the price engine loads for a run on `day` (marden.models.price.run_day:
Treasury curves, instruments and comps, observations, the issuers' bond-implied intensity curves, tier
fit, chain signals, GPU half-lives) and resolves each instrument's chain once. `book.reprice(id, shock)`
then prices one instrument twice with the price engine's own functions (price_facility: the model for its
chain, the calibration at issue or first observation, the Kalman-filtered residual):

- now: exactly the engine's price on the day;
- after: the same calibration and residual, with the shocked inputs on the as-of day only (a shock never
  moves the history the liquidity premium was calibrated on): each entity's intensity times its multiplier
  (`Shock.hazard_mult`: tenant, obligor, guarantor and issuer legs), each site's completion (the cushion P50
  rent start the chain reads) later by `Shock.completion_months`, and `Shock.spread_bp` added to the spread.
Instruments the engine prices off the tier curve are re-priced through the same intensity legs: the
spread that reproduces the tier price under today's intensities is held, and the shocked intensities applied.

The book is cached per as-of day in the process (`cached_book`) so custom requests after the first run in
seconds; `BOOK_TTL_SECONDS` bounds how stale an in-memory book may be.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from dateutil.relativedelta import relativedelta

from marden.core.db import Conn
from marden.models import price_marks, price_signals, price_tiers
from marden.models.price import HISTORY_START, _unsupported, cds_issuers, targets
from marden.models.price_bonds import solve_spread
from marden.models.price_chain import Chain, gpu_half_lives, resolve
from marden.models.price_curve import load_curves
from marden.models.price_facility import (
    CALIBRATED_SD,
    Q_RESID,
    Ctx,
    _flows,
    _tier_price,
    calibrate,
    clean_price,
    model_for,
    readings,
)
from marden.models.price_issuer import IssuerCurve, build, straight_bonds
from marden.models.price_kalman import run as kalman
from marden.models.price_marks import Obs
from marden.models.price_terms import Instrument, attach, load_comps, load_instruments
from marden.readmodels.context import business_today

BOOK_TTL_SECONDS = 6 * 3600
_CACHE: dict[date, tuple[float, Book]] = {}


@dataclass
class Shock:
    hazard_mult: dict[str, float] = field(default_factory=dict)
    completion_months: dict[str, float] = field(default_factory=dict)
    spread_bp: float = 0.0


class ShockedCurve:
    """An issuer curve whose intensity on `day` is multiplied by `mult` (other days unchanged)."""

    def __init__(self, cur: IssuerCurve, mult: float, day: date) -> None:
        self._cur, self._mult, self._day = cur, mult, day

    def hazard(self, d: date) -> tuple[float, float] | None:
        got = self._cur.hazard(d)
        if got is None or d != self._day:
            return got
        return got[0] * self._mult, got[1]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cur, name)


@dataclass
class Book:
    day: date
    ctx: Ctx
    insts: dict[str, Instrument]
    obs: dict[str, list[Obs]]
    curves: dict[str, IssuerCurve]
    chains: dict[str, Chain | None]
    issuer_bonds: set[str]

    @classmethod
    def build(cls, conn: Conn, day: date) -> Book:
        book = load_curves(conn, day, since=HISTORY_START)
        if book.curve(day) is None:
            raise ValueError(f"no Treasury curve public on or within a week before {day}")
        insts = load_instruments(conn, targets(conn))
        attach(insts, load_comps(conn, business_today()))  # as marden.models.price.run_day
        by_issuer = straight_bonds(insts)
        obs = price_marks.load(conn, day, list(insts))
        tickers = {r["id"]: r["ticker"] for r in conn.execute("SELECT id, ticker FROM entity").fetchall()}
        curves = {e: build(conn, day, e, bonds, obs, book, tickers.get(e)) for e, bonds in sorted(by_issuer.items())}
        corp = cds_issuers(conn, by_issuer, curves)
        tiers = price_tiers.fit(conn, day, load_comps(conn, day), book)
        slugs = sorted({i.site_slug for i in insts.values() if i.site_slug})
        ctx = Ctx(conn, day, book, {e: c for e, c in curves.items() if e in corp}, tiers,
                  {s: price_signals.from_records(conn, day, s) for s in slugs}, False)
        halves = gpu_half_lives(conn, day)
        issuer_bonds = {b.id for e in corp for b in by_issuer.get(e, [])}
        chains = {iid: None if iid in issuer_bonds else resolve(conn, inst, day, lambda e: e in corp, halves)
                  for iid, inst in insts.items()}
        return cls(day, ctx, insts, obs, ctx.curves, chains, issuer_bonds)

    def hazard(self, entity_id: str) -> float | None:
        cur = self.curves.get(entity_id)
        got = cur.hazard(self.day) if cur is not None else None
        return got[0] if got else None

    def _shocked(self, inst: Instrument, ch: Chain | None, shock: Shock) -> tuple[Ctx, Chain | None, list[str]]:
        curves = {e: ShockedCurve(c, shock.hazard_mult.get(e, 1.0), self.day) if e in shock.hazard_mult else c
                  for e, c in self.curves.items()}
        missing: list[str] = []
        ch_s = ch
        months = shock.completion_months.get(inst.site_slug or "", 0.0)
        if ch is not None and months:
            if ch.completion is None:
                missing.append(f"instrument {inst.id}: no completion date to move (cushion P50)")
            else:
                ch_s = replace(ch, completion=ch.completion + relativedelta(months=round(months)))
        return replace(self.ctx, curves=curves), ch_s, missing  # type: ignore[arg-type]

    def reprice(self, iid: str, shock: Shock) -> dict[str, Any]:
        """{now, after, mode, missing} for one instrument (now/after null when it cannot be priced)."""
        out: dict[str, Any] = {"now": None, "after": None, "mode": None, "missing": []}
        inst = self.insts.get(iid)
        if inst is None:
            out["missing"].append(f"instrument {iid}: not in the price engine's universe")
            return out
        if (why := _unsupported(inst)) is not None:
            out["missing"].append(why)
            return out
        ch = self.chains.get(iid)
        out["missing"] += list(inst.terms_missing) + (list(ch.missing) if ch else [])
        f_now = _flows(self.ctx, inst, self.day) if inst.terms else None
        if f_now is None or (inst.issue_date is not None and inst.issue_date > self.day):
            out["missing"].append(f"instrument {iid}: terms, curve or issue date for {self.day}")
            return out
        obs = self.obs.get(iid, [])
        m = model_for(self.ctx, inst, ch)
        curve = self.ctx.book.curve(self.day)
        assert curve is not None
        cal = calibrate(self.ctx, inst, m, obs)
        hz, miss, _ = m.hazard(self.day)
        out["missing"] += miss
        if hz is None:
            return out
        if cal is None:
            tier = _tier_price(self.ctx, inst, ch, f_now, {"missing": []})
            if tier.get("price") is None:
                out["missing"].append(f"instrument {iid}: no calibration and no tier price")
                return out
            spread = solve_spread(f_now, curve, tier["price"], hz, m.recovery).value
            now, out["mode"] = float(tier["price"]), "tier curve"
        else:
            cal_day, lp, _ = cal
            days = self.ctx.book.business_days(cal_day, self.day) or [self.day]
            if days[-1] != self.day:
                days = [*days, self.day]
            rd = readings(self.ctx, inst, m, lp, [o for o in obs if o.day >= cal_day], days)
            filt = kalman(len(days), 0.0, CALIBRATED_SD**2 if m.mode == "facility" else 0.02**2, Q_RESID, rd)
            spread = lp + filt.at(len(days) - 1)[0]
            now, out["mode"] = clean_price(f_now, curve, hz, m.recovery, spread), m.mode
        ctx_s, ch_s, miss_s = self._shocked(inst, ch, shock)
        out["missing"] += miss_s
        m_s = model_for(ctx_s, inst, ch_s)
        hz_s, _, _ = m_s.hazard(self.day)
        if hz_s is None:
            return {**out, "now": round(now, 3)}
        after = clean_price(f_now, curve, hz_s, m_s.recovery, spread + shock.spread_bp / 1e4)
        return {**out, "now": round(now, 3), "after": round(after, 3)}


def remember(book: Book) -> Book:
    """Make `book` the process's cached book for its day (a fresh nightly or triggered run replaces it)."""
    _CACHE.clear()
    _CACHE[book.day] = (time.time(), book)
    return book


def cached_book(conn: Conn, day: date) -> Book:
    hit = _CACHE.get(day)
    if hit is not None and time.time() - hit[0] < BOOK_TTL_SECONDS:
        return hit[1]
    return remember(Book.build(conn, day))
