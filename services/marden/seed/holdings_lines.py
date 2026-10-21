"""Records the holdings loader parses from the archive: one ``HoldingLine`` per filed position line.

Pure data and formatting helpers (no database or file access), so the rules can be unit-tested on real rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any


def dec(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def canon(value: Any) -> str:
    d = dec(value)
    return "" if d is None else format(d.normalize(), "f")


def nport_line_key(code: str, units: Any, balance: Any, value: Any) -> str:
    """Stable per filing line: the line's identifier (or name), units, balance and value as filed. The same
    line read from a w2 file and an agg file gets the same key."""
    return f"{code}|{units or ''}|{canon(balance)}|{canon(value)}"


def bdc_title(borrower: str | None, lien: Any, spread_bp: Any, maturity: Any) -> str:
    parts = [borrower or "borrower n/d"]
    if lien:
        parts.append(str(lien))
    if spread_bp is not None:
        parts.append(f"{spread_bp} bp")
    if maturity:
        parts.append(f"matures {maturity}")
    return ", ".join(parts)


def naic_title(desc: Any, designation: Any) -> str:
    text = " ".join(str(desc or "").split()) or "description n/d"
    return f"NAIC Schedule D: {text}" + (f" (NAIC {designation})" if designation else "")


@dataclass(frozen=True)
class FundSpec:
    """A holder: an N-PORT series (parent = its registrant), a BDC, or an insurer (parent = its group)."""

    kind: str
    name: str
    cik: str | None
    parent_name: str | None
    parent_cik: str | None
    meta: dict[str, Any]


@dataclass(frozen=True)
class NavRecord:
    fund_key: str
    nav_usd: Decimal
    nav_per_share: Decimal | None
    as_of: date
    url: str


@dataclass
class HoldingLine:
    source: str  # 'nport' | 'bdc' | 'naic'
    relpath: str  # seed file the line was read from
    fund_key: str
    fund_name: str  # series / BDC / insurer name as filed
    accession: str
    filing_url: str
    filing_type: str
    period_end: date
    filed_at: date | None
    line_key: str
    issuer_name: str | None
    title: str | None
    cusip: str | None
    isin: str | None
    codes: tuple[str, ...]  # strong identifiers (CUSIP, ISIN, LoanX, Bloomberg loan id): join lines and instruments
    weak_codes: tuple[str, ...]  # other filer codes: only looked up against instrument identifiers
    units: str | None
    par: Decimal | None
    value: Decimal | None
    usd: bool
    raw_mark: Decimal | None  # 100 x value / balance before plausibility rules
    pct_nav: Decimal | None
    maturity: str | None  # ISO date (N-PORT) or YYYY-MM (BDC)
    coupon_kind: str | None  # 'fixed' | 'floating' | None
    coupon_rate: float | None  # percent
    asset_cat: str | None
    unfunded: bool
    linkable: bool
    mark: Decimal | None = None  # holding.mark_price; set by ``finalize_mark``
    non_accrual: bool | None = None
    fund_net_assets: Decimal | None = None
    spread_bp: float | None = None  # BDC: spread (or fixed coupon) in bp
    lien: str | None = None
    bkey: str | None = None
    mark_override: Decimal | None = None
    fund_id: str | None = None  # the holder's entity id, set once entities exist
    instrument_id: str | None = None
    link_method: str | None = None
    notes: list[str] = field(default_factory=list)

    def finalize_mark(self) -> None:
        """holding.mark_price: BDC lines keep the plausibility-checked mark; N-PORT lines need par in USD
        (non-USD lines carry valUSD over a foreign-currency balance) and a non-negative value."""
        if self.source == "bdc":
            self.mark = self.mark_override
        elif self.raw_mark is not None and self.usd and self.raw_mark >= 0:
            self.mark = self.raw_mark
        else:
            self.mark = None

    @property
    def priceable(self) -> bool:
        """Becomes a fund_mark price_obs: a linked N-PORT or BDC line with a mark that is not an unfunded
        commitment. NAIC Schedule D lines are held for legal review (price-sources.md section 3)."""
        return (self.instrument_id is not None and self.mark is not None and not self.unfunded
                and self.source in ("nport", "bdc"))

    @property
    def cohort(self) -> str:
        return {"nport": "N-PORT", "bdc": "BDC 10-Q", "naic": "NAIC Schedule D"}[self.source]

    @property
    def facility_key(self) -> tuple[Any, ...]:
        return (self.bkey, self.lien, self.spread_bp, self.maturity)
