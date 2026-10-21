"""The year-by-year path of payer_capacity (spec 24 "The model" steps 1-2): committed outflows against cash
flow, and lease-adjusted leverage as signed leases commence.

Per fiscal year after the latest filed one (three years; a year is named by the calendar year it ends in,
and an edge's calendar-year schedule lands in the fiscal year ending that year):
- ocfB, capexB: trailing-twelve-month operating cash flow and capex (SEC XBRL) held flat: Marden collects
  no guidance yet, and the basis says so;
- leaseCommencingB: the totals of the payer's lease edges on the obligation map that start that year
  (attributed leases only, undiscounted: agencies capitalize the present value, so v0 is an upper bound);
- computeCommitmentsB: the payer's compute and chip purchase payments that year (the edges' schedules);
- fundingGapB = capex + compute payments + lease payments in force that year - operating cash flow;
- leaseAdjLeverage = (debt + lease liabilities today + leases commenced by then) / EBITDA (trailing,
  held flat).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.models import graph
from marden.models.credit_inputs import Figure, Financials
from marden.models.sourced import m_value

YEARS_AHEAD = 3
LEASE_TYPES = ("Lease",)
COMPUTE_TYPES = ("Compute purchase", "Chip purchase")


@dataclass
class Path:
    years: list[dict[str, Any]] = field(default_factory=list)
    leverage_today: float | None = None
    leverage_peak: float | None = None
    gap_3y: float | None = None
    missing: list[str] = field(default_factory=list)
    basis: dict[str, Any] = field(default_factory=dict)


def _b(x: float | None) -> float | None:
    return None if x is None else round(x / 1e9, 3)


def fiscal_end(fin: Financials) -> date | None:
    """The latest fiscal-year end with a filed 12-month operating cash flow or capex."""
    ends = [r["valid_from"] for f in ("operating_cash_flow_12m_usd", "capex_12m_usd") for r in fin.rows.get(f, [])]
    return max(ends) if ends else None


def path(fin: Financials, edges: list[graph.Edge], day: date, method: str) -> Path:
    p = Path()

    def m(value: Any) -> dict[str, Any]:
        return m_value(value, day, method, source_name="Marden payer capacity", uncalibrated=True)

    debt, leases, ebitda = fin.debt(), fin.leases(), fin.ebitda()
    ocf = fin.ttm("operating_cash_flow")
    capex = fin.ttm("capex") or fin.ttm("productive_asset_payments")
    for name, fig in (("debt", debt), ("lease liabilities", leases), ("EBITDA", ebitda), ("operating cash flow", ocf),
                      ("capex", capex)):
        if fig is None:
            p.missing.append(f"entity {fin.entity_id}: {name} (SEC XBRL)")
    base_liab = (debt.usd if debt else 0.0) + (leases.usd if leases else 0.0)
    if debt is not None and ebitda is not None and ebitda.usd > 0:
        p.leverage_today = round(base_liab / ebitda.usd, 2)
    elif ebitda is not None and ebitda.usd <= 0:
        p.missing.append(f"entity {fin.entity_id}: EBITDA is not positive (leverage not meaningful)")
    fy = fiscal_end(fin)
    p.basis = {"debt": _fig(debt), "leaseLiabilities": _fig(leases), "ebitda": _fig(ebitda), "ocf": _fig(ocf),
               "capex": _fig(capex), "fiscalYearEnd": fy.isoformat() if fy else None,
               "projection": "trailing twelve months held flat (no guidance collected)"}
    if fy is None:
        p.missing.append(f"entity {fin.entity_id}: a filed fiscal year (SEC XBRL)")
        return p
    own = [e for e in edges if e.payer == fin.entity_id and e.pays]
    unsized = [e.id for e in own if not e.schedule() and e.total is None]
    if unsized:
        p.missing.append(f"entity {fin.entity_id}: {len(unsized)} lease / compute edges not sized ({', '.join(unsized[:4])})")
    added, gaps, levs = 0.0, [], []
    for year in range(fy.year + 1, fy.year + 1 + YEARS_AHEAD):
        commencing = sum(e.total.usd for e in own if e.type in LEASE_TYPES and e.start == year and e.total)
        compute = sum(e.annual_usd(year) or 0.0 for e in own if e.type in COMPUTE_TYPES)
        rent = sum(e.annual_usd(year) or 0.0 for e in own if e.type in LEASE_TYPES)
        added += commencing
        gap = None
        if ocf is not None and capex is not None:
            gap = capex.usd + compute + rent - ocf.usd
            gaps.append(gap)
        lev = round((base_liab + added) / ebitda.usd, 2) if (debt and ebitda and ebitda.usd > 0) else None
        if lev is not None:
            levs.append(lev)
        p.years.append({
            "fiscalYear": f"FY{year}",
            "ocfB": m(_b(ocf.usd) if ocf else None),
            "capexB": m(_b(capex.usd) if capex else None),
            "leaseCommencingB": m(_b(commencing)),
            "computeCommitmentsB": m(_b(compute)),
            "fundingGapB": m(_b(gap)),
            "leaseAdjLeverage": m(lev),
        })
    if p.leverage_today is not None:
        p.leverage_peak = max([p.leverage_today, *levs])
    if len(gaps) == YEARS_AHEAD:
        p.gap_3y = round(sum(gaps) / 1e9, 3)
    return p


def _fig(f: Figure | None) -> dict[str, Any] | None:
    return None if f is None else {"b": _b(f.usd), "asOf": f.as_of.isoformat(), "basis": f.basis}
