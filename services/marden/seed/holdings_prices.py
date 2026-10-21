"""Fund-mark prices from linked holding lines (docs/methods/price-sources.md sections 2-4).

N-PORT and BDC marks are the only seeded price anchors, all ``kind='fund_mark'``:

- one row per linked holder line: price = 100 x value / balance (units PA, USD), tag F, source_name
  ``N-PORT: <fund> (<period>)`` or ``BDC 10-Q: <BDC> (<period>)``, source_url = the filing URL. This is the
  dedupe contract shared with the instruments loader (seed/instruments_nport.py), so a line it already
  priced is not written twice;
- per instrument, filing period and cohort (N-PORT / BDC): the median across holders (each holder's
  lines combined as 100 x sum of value / sum of balance), tag F, ``N-PORT median of N funds (<period>)``
  or ``BDC 10-Q median of N funds (<period>)``, written when N >= 2 (with one holder the holder's own row is
  the observation);
- per CUSIP and period for instruments held under several CUSIPs: ``N-PORT median of N funds (<period>,
  CUSIP <cusip>)``;
- BDC delayed-draw lines whose unfunded commitment is known: the adjusted price
  100 x (FV + U) / (F + U), tag M (raw F mark kept).

Never priced: unfunded-commitment lines, non-USD N-PORT lines, negative values, BDC unit errors, insurer
NAIC Schedule D lines (held for legal review), unlinked lines.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

from marden.seed.holdings_archive import cik_from_url
from marden.seed.holdings_lines import HoldingLine
from marden.seed.instruments_nport import PRICE_QUANTUM, bdc_source_name, fund_mark_price, nport_source_name

ADJUSTED_METHOD = "seed-holdings-bdc-unfunded-adjusted@2026-10-21"


@dataclass(frozen=True)
class PriceRow:
    instrument_id: str
    obs_date: date
    price: Decimal
    tag: str
    source_name: str
    source_url: str | None
    relpath: str
    method_version: str | None = None
    kind_note: str = "holder"  # holder | median | cusip_median | adjusted


@dataclass(frozen=True)
class UnfundedCommitment:
    """A delayed-draw commitment whose unfunded part a filing states (price-sources.md section 2)."""

    bdc_cik: str
    bkey: str
    maturity: str
    commitment_usd: Decimal
    unfunded_usd: Decimal
    evidence: str


# Palmer Square BDC 10-Q at 30 Jun 2026, CoreWeave DDTL V, footnote 13: "Of the $5,000,000 commitment ...
# $3,224,194 was unfunded" (docs/methods/price-sources.md section 2; filed par 1,775,806, FV 1,883,231).
KNOWN_UNFUNDED = (
    UnfundedCommitment("1794776", "coreweave financing", "2031-11", Decimal(5_000_000), Decimal(3_224_194),
                       "Palmer Square BDC 10-Q (2026-06-30) footnote 13, per docs/methods/price-sources.md"),
)
PAR_ROUNDING_USD = Decimal(1000)  # fundpos carries par in $ thousands


def _q(value: Decimal) -> Decimal:
    return value.quantize(PRICE_QUANTUM, rounding=ROUND_HALF_EVEN)


def holder_prices(lines: list[HoldingLine]) -> list[PriceRow]:
    out: list[PriceRow] = []
    for ln in lines:
        if not ln.priceable:
            continue
        assert ln.instrument_id is not None and ln.mark is not None
        name = (nport_source_name(ln.fund_name, ln.period_end) if ln.source == "nport"
                else bdc_source_name(ln.fund_name, ln.period_end))
        out.append(PriceRow(ln.instrument_id, ln.period_end, ln.mark, "F", name, ln.filing_url, ln.relpath))
    return out


def _median_of_holders(lines: list[HoldingLine]) -> tuple[Decimal, int] | None:
    by_fund: dict[str, list[HoldingLine]] = defaultdict(list)
    for ln in lines:
        by_fund[ln.fund_id or ln.fund_key].append(ln)
    marks = []
    for fund_lines in by_fund.values():
        par = sum((ln.par for ln in fund_lines if ln.par is not None), Decimal(0))
        value = sum((ln.value for ln in fund_lines if ln.value is not None), Decimal(0))
        mark = fund_mark_price(value, par)
        if mark is not None:
            marks.append(mark)
    if len(marks) < 2:
        return None
    return _q(Decimal(statistics.median(marks))), len(marks)


def median_prices(lines: list[HoldingLine]) -> list[PriceRow]:
    groups: dict[tuple[str, str, date], list[HoldingLine]] = defaultdict(list)
    for ln in lines:
        if ln.priceable:
            assert ln.instrument_id is not None
            groups[(ln.instrument_id, ln.cohort, ln.period_end)].append(ln)
    out: list[PriceRow] = []
    for (iid, cohort, period), group in sorted(groups.items()):
        relpath = min(ln.relpath for ln in group)
        med = _median_of_holders(group)
        if med is not None:
            out.append(PriceRow(iid, period, med[0], "F", f"{cohort} median of {med[1]} funds ({period.isoformat()})",
                                None, relpath, kind_note="median"))
        cusips = sorted({ln.cusip for ln in group if ln.cusip})
        if cohort != "N-PORT" or len(cusips) < 2:
            continue
        for cusip in cusips:
            sub = [ln for ln in group if ln.cusip == cusip]
            med = _median_of_holders(sub)
            if med is not None:
                out.append(PriceRow(
                    iid, period, med[0], "F", f"N-PORT median of {med[1]} funds ({period.isoformat()}, CUSIP {cusip})",
                    None, min(ln.relpath for ln in sub), kind_note="cusip_median"))
    return out


def adjusted_prices(lines: list[HoldingLine]) -> tuple[list[PriceRow], list[str]]:
    """Unfunded-commitment-adjusted BDC delayed-draw marks (M). Returns (rows, notes on known commitments
    that matched no line)."""
    out: list[PriceRow] = []
    notes: list[str] = []
    for c in KNOWN_UNFUNDED:
        hits = [ln for ln in lines if ln.source == "bdc" and ln.priceable and ln.bkey == c.bkey
                and ln.maturity == c.maturity and cik_from_url(ln.filing_url) == c.bdc_cik]
        funded = c.commitment_usd - c.unfunded_usd
        hits = [ln for ln in hits if ln.par is not None and abs(ln.par - funded) <= PAR_ROUNDING_USD]
        if not hits:
            notes.append(f"no BDC line matches the known commitment: {c.evidence}")
            continue
        for ln in hits:
            assert ln.instrument_id is not None and ln.value is not None and ln.par is not None
            price = _q(Decimal(100) * (ln.value + c.unfunded_usd) / (ln.par + c.unfunded_usd))
            name = f"{bdc_source_name(ln.fund_name, ln.period_end)}, adjusted for the unfunded commitment"
            out.append(PriceRow(ln.instrument_id, ln.period_end, price, "M", name, ln.filing_url, ln.relpath,
                                method_version=ADJUSTED_METHOD, kind_note="adjusted"))
    return out, notes
