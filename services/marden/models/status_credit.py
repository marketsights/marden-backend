"""The rate-neutral reading of a price, for status rules L1 and L2 (docs/methods/status-rules-v0.1.md).

From status v0.5 the reading that trips L1 / L2 is the one on market evidence (`quote_test`: a trade,
an exchange or dealer quote, or a fund mark; its spread is the quote's G-spread on its own day's Treasury
curve); the same reading on the Marden price (`credit_test`) is shown as a note and never trips a rule.

A chain status is a credit signal. A fixed-rate instrument's price also falls when Treasury yields rise
while its credit is unchanged, so for a fixed coupon L1 and L2 read the Marden spread, not the price:

- reference day: the instrument's issue date (its issue comp: the price output's `basis.terms.issueDate`),
  else the first day Marden priced it (its earliest stored `price` output with a spread, on or before the
  run day);
- reference spread: the Marden spread that day, on the price model's own measure (`spreadBp`: yield minus
  the Treasury par yield at the same maturity). At issue it is the issue price's spread on that day's
  Treasury curve (the price model solves its liquidity premium so it reprices the issue price that day);
  on a first Marden price it is that output's `spreadBp`;
- credit price: the price output's cash flows, on the output's own day, priced at the reference day's
  Treasury par yield (at the remaining maturity) plus the Marden spread of the output; reference price:
  the same with the reference spread. Spread-only loss = reference price - credit price, in points per
  100 par: (spread now - spread at reference) x spread duration, revalued in full.

L2 fires on a loss of LOSS_RED_POINTS or more (the spread move alone would take a par-issued instrument
below 95), L1 (status v0.3) on a loss of LOSS_AMBER_POINTS up to LOSS_RED_POINTS (it would take it to 98 or
below). A floating-rate loan has no rate duration (its coupon resets with SOFR), so its price is already a
credit price and L1 / L2 read the market price itself (`status_rules.lenders`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from marden.models.price_bonds import Terms, clean_at_yield, flows, g_spread_bp
from marden.models.price_curve import CurveBook

LOSS_RED_POINTS = 5.0  # 100 - 95: L2's line for a par-issued instrument
LOSS_AMBER_POINTS = 2.0  # 100 - 98: L1's line (a loss of 2 up to 5 points)
ONE_BP_PCT = 0.01


@dataclass(frozen=True)
class Reference:
    day: date
    basis: str  # 'issue' | 'first Marden price'
    spread_bp: float
    price: float | None = None  # the issue price, at issue

    @property
    def at(self) -> str:
        return f"at issue {self.day.isoformat()}" if self.basis == "issue" else \
            f"on the first Marden price {self.day.isoformat()}"

    @property
    def since(self) -> str:
        return f"since issue {self.day.isoformat()}" if self.basis == "issue" else \
            f"since the first Marden price {self.day.isoformat()}"


@dataclass(frozen=True)
class CreditTest:
    ref: Reference
    spread_bp: float  # the Marden spread of the price output
    credit_price: float  # that spread on the reference day's Treasury curve
    ref_price: float  # the reference spread on the same curve
    spread_duration: float  # modified duration at the credit price's yield (years)

    @property
    def loss(self) -> float:
        """Points lost on the spread move alone (negative when the spread tightened)."""
        return self.ref_price - self.credit_price

    def doc(self) -> dict[str, Any]:
        return {"referenceDate": self.ref.day.isoformat(), "referenceBasis": self.ref.basis,
                "referenceSpreadBp": round(self.ref.spread_bp, 1), "spreadBp": round(self.spread_bp, 1),
                "creditPrice": round(self.credit_price, 3), "referencePrice": round(self.ref_price, 3),
                "spreadLossPts": round(self.loss, 3), "spreadDuration": round(self.spread_duration, 2)}


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _terms(output: dict[str, Any]) -> dict[str, Any]:
    basis = output.get("basis")
    terms = basis.get("terms") if isinstance(basis, dict) else None
    return terms if isinstance(terms, dict) else {}


def coupon_type(output: Any) -> str | None:
    """'fixed' | 'floating' from the price output's terms (None when it carries none)."""
    ctype = _terms(output).get("couponType") if isinstance(output, dict) else None
    return ctype if ctype in ("fixed", "floating") else None


def fixed_terms(instrument_id: str, output: dict[str, Any]) -> Terms | None:
    t = _terms(output)
    coupon, maturity = _num(t.get("couponPct")), t.get("maturity")
    if t.get("couponType") != "fixed" or coupon is None or not isinstance(maturity, str):
        return None
    try:
        day = date.fromisoformat(maturity[:10])
    except ValueError:
        return None
    return Terms(instrument_id, day, "fixed", coupon, None, int(_num(t.get("frequency")) or 2))


def issue_reference(terms: Terms, output: dict[str, Any], book: CurveBook) -> Reference | None:
    """The issue price's Marden spread on the issue day (the issue comp's date and price)."""
    t = _terms(output)
    issue_price = _num(t.get("issuePrice"))
    if not isinstance(t.get("issueDate"), str) or issue_price is None:
        return None
    try:
        day = date.fromisoformat(t["issueDate"][:10])
    except ValueError:
        return None
    curve = book.curve(day)
    if curve is None or terms.maturity <= day:
        return None
    spread = g_spread_bp(flows(terms, curve, day, None), curve, issue_price)
    return None if spread is None else Reference(day, "issue", spread, issue_price)


def credit_test(instrument_id: str, output: dict[str, Any], day: date, book: CurveBook,
                first: tuple[date, float] | None) -> tuple[CreditTest | None, str | None]:
    """The rate-neutral reading of one fixed-rate price output dated `day`, or (None, what is missing).
    `first`: (day, spreadBp) of the instrument's first stored Marden price."""
    terms = fixed_terms(instrument_id, output)
    spread = _num(output.get("spreadBp"))
    if terms is None or spread is None:
        return None, f"fixed-rate terms and Marden spread of {instrument_id}"
    if terms.maturity <= day:
        return None, f"{instrument_id} matured on {terms.maturity.isoformat()}"
    ref = issue_reference(terms, output, book)
    if ref is None and first is not None and first[0] <= day:
        ref = Reference(first[0], "first Marden price", first[1])
    if ref is None:
        return None, f"spread at issue or on a first Marden price of {instrument_id}"
    curve = book.curve(ref.day)
    if curve is None:
        return None, f"Treasury curve on {ref.day.isoformat()} (reference day of {instrument_id})"
    f = flows(terms, curve, day, None)
    rate = curve.par_yield(f.maturity_years)  # the reference day's risk-free rate at today's maturity
    y_credit = rate + spread * ONE_BP_PCT
    credit = clean_at_yield(f, y_credit)
    ref_price = clean_at_yield(f, rate + ref.spread_bp * ONE_BP_PCT)
    dv = clean_at_yield(f, y_credit - ONE_BP_PCT) - clean_at_yield(f, y_credit + ONE_BP_PCT)
    duration = dv / (2 * ONE_BP_PCT / 100.0) / (credit + f.accrued)
    return CreditTest(ref, spread, credit, ref_price, duration), None


def quote_test(instrument_id: str, output: dict[str, Any], quote_day: date, quote_price: float, book: CurveBook,
               first: tuple[date, float] | None) -> tuple[CreditTest | None, str | None]:
    """The rate-neutral reading of a market quote (status v0.5): the quote's spread over the Treasury par
    yield at the same maturity on the quote's day (its G-spread, the price model's `spreadBp` measure),
    read against the reference spread like a price output's (`credit_test`, on the quote's day). The
    instrument's terms and issue comp come from its price output."""
    terms = fixed_terms(instrument_id, output)
    if terms is None:
        return None, f"fixed-rate terms of {instrument_id}"
    curve = book.curve(quote_day)
    if curve is None:
        return None, f"Treasury curve on {quote_day.isoformat()} (market quote of {instrument_id})"
    if terms.maturity <= quote_day:
        return None, f"{instrument_id} matured on {terms.maturity.isoformat()}"
    spread = g_spread_bp(flows(terms, curve, quote_day, None), curve, quote_price)
    if spread is None:
        return None, f"spread of the market quote of {instrument_id} ({quote_price:.2f} on {quote_day.isoformat()})"
    return credit_test(instrument_id, {**output, "spreadBp": spread}, quote_day, book, first)
