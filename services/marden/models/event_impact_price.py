"""Counterfactual Marden prices for the event impact model (docs/methods/event-impact-v0.1.md).

One `DayModel` holds the price engine's inputs for one pricing day, built exactly as `price.run_day` builds
them (the Treasury and SOFR book, every target instrument with its issue comp, the observations, the issuer
curves of the corporate CDS issuers, the tier fit, the GPU half-lives). It then prices any instrument with
the engine's own `price_facility.price`, either as the engine does that day or with one input changed:

- `signals`: the site's power and permit signals with some records left out (price_signals);
- `completion`: the site's projected completion replaced by an earlier cushion projection (price_chain);
- `drop_obs`: observations left out (a dealer quote).

Everything else is the same day's input, so the difference between the two prices is what that input is
worth on that day. The setup is mirrored from `price.run_day`; `stored` reads the engine's own stored price
for the day so a reader can check the base price matches it.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.core.db import Conn
from marden.models import pit, price_marks, price_signals, price_tiers, runs
from marden.models.price import HISTORY_START, _unsupported, cds_issuers, targets
from marden.models.price_chain import Chain, gpu_half_lives, resolve
from marden.models.price_curve import CurveBook, load_curves
from marden.models.price_facility import Ctx, price
from marden.models.price_issuer import IssuerCurve, build, straight_bonds
from marden.models.price_marks import Obs
from marden.models.price_signals import Signal
from marden.models.price_terms import Instrument, attach, load_comps, load_instruments
from marden.readmodels.context import business_today

ObsFilter = Callable[[Obs], bool]


@dataclass
class DayModel:
    """The price engine's inputs on one day, ready to price an instrument with one input changed."""

    conn: Conn
    day: date
    book: CurveBook
    insts: dict[str, Instrument]
    curves: dict[str, IssuerCurve]
    corp: set[str]
    by_issuer: dict[str, list[Instrument]]
    tiers: price_tiers.TierFit | None
    obs: dict[str, list[Obs]]
    halves: dict[str, float]
    signals: dict[str, list[Signal]] = field(default_factory=dict)
    _chains: dict[str, Chain | None] = field(default_factory=dict)

    def site_signals(self, slug: str) -> list[Signal]:
        if slug not in self.signals:
            self.signals[slug] = price_signals.from_records(self.conn, self.day, slug)
        return self.signals[slug]

    def chain(self, iid: str) -> Chain | None:
        """The payment chain the engine resolves for the instrument that day (None for an issuer bond)."""
        if iid not in self._chains:
            inst = self.insts[iid]
            bonds = self.by_issuer.get(inst.issuer or "", [])
            issuer_bond = inst.issuer in self.corp and any(b.id == iid for b in bonds)
            self._chains[iid] = None if issuer_bond else resolve(self.conn, inst, self.day, lambda e: e in self.corp,
                                                                 self.halves)
        return self._chains[iid]

    def price(self, iid: str, *, signals: Sequence[Signal] | None = None, completion: date | str | None = "same",
              drop_obs: ObsFilter | None = None) -> dict[str, Any]:
        """The engine's price output for `iid` on the day; `signals` replaces the site's signals, `completion`
        (a date or None) the projected completion, `drop_obs` leaves observations out."""
        inst = self.insts.get(iid)
        if inst is None:
            return {"price": None, "missing": [f"instrument {iid}: not in the price engine's universe"]}
        why = _unsupported(inst)
        if why:
            return {"price": None, "missing": [why]}
        ch = self.chain(iid)
        if ch is not None and completion != "same":
            ch = copy.copy(ch)
            ch.completion = completion  # type: ignore[assignment]
        sigs = {}
        if ch is not None and ch.signals_site:
            sigs[ch.signals_site] = list(signals) if signals is not None else self.site_signals(ch.signals_site)
        ctx = Ctx(self.conn, self.day, self.book, {e: c for e, c in self.curves.items() if e in self.corp}, self.tiers,
                  sigs, False)
        obs = [o for o in self.obs.get(iid, []) if drop_obs is None or not drop_obs(o)]
        try:
            out = price(ctx, inst, ch, obs)
        except (ValueError, ZeroDivisionError) as exc:  # as price.run_day: bad terms never stop the run
            return {"price": None, "missing": [f"instrument {iid}: {type(exc).__name__}: {exc}"]}
        out.pop("_week_ago_price", None)
        return out

    def stored(self, iid: str) -> float | None:
        """The engine's own stored price for exactly this day, if it ran that day."""
        row = runs.output_on(self.conn, "price", "instrument", iid, self.day)
        px = row["output"].get("price") if row is not None and isinstance(row["output"], dict) else None
        return float(px) if isinstance(px, int | float) else None


def priceable(day: date) -> str | None:
    """Why the engine cannot price on `day` (None when it can)."""
    if day < HISTORY_START:
        return f"before the price engine's history (Treasury curves from {HISTORY_START:%b %Y})"
    if day > business_today():
        return "in the future"
    return None


def day_model(conn: Conn, day: date) -> DayModel:
    """The engine's inputs on `day`, built as `price.run_day` builds them."""
    book = load_curves(conn, day, since=HISTORY_START)
    if book.curve(day) is None:
        raise ValueError(f"no Treasury curve public on or within a week before {day}")
    insts = load_instruments(conn, targets(conn))
    with pit.unaudited():
        attach(insts, load_comps(conn, business_today()))
    by_issuer = straight_bonds(insts)
    obs = price_marks.load(conn, day, list(insts))
    tickers = {r["id"]: r["ticker"] for r in conn.execute("SELECT id, ticker FROM entity").fetchall()}
    curves = {e: build(conn, day, e, bonds, obs, book, tickers.get(e)) for e, bonds in sorted(by_issuer.items())}
    corp = cds_issuers(conn, by_issuer, curves)
    tiers = price_tiers.fit(conn, day, load_comps(conn, day), book)
    return DayModel(conn, day, book, insts, curves, corp, by_issuer, tiers, obs, gpu_half_lives(conn, day))
