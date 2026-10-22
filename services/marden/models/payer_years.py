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
The last three filed fiscal years carry their filed lease-adjusted leverage too: (debt + operating and
finance lease liabilities at the fiscal year end) / that year's EBITDA (operating income + D&A), all SEC
XBRL as filed; n/d when one of them is not filed for that year end.
A sum with an unsized or undated edge in it is n/d (null with a `missing` entry), never a partial sum read
as complete; the funding gap's known part is kept as `basis.fundingGap3yFloorB` (unknown payments only add).
`reasons` says in plain words why the projected leverage path or the 3-year funding gap is n/d.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.models import graph
from marden.models.credit_inputs import Figure, Financials
from marden.models.sourced import fact_value, m_value

YEARS_AHEAD = 3
FILED_YEARS = 3  # filed fiscal years shown before the projection (views/payer_capacity.FILED_YEARS)
LEASE_TYPES = ("Lease",)
COMPUTE_TYPES = ("Compute purchase", "Chip purchase")


@dataclass
class Path:
    years: list[dict[str, Any]] = field(default_factory=list)
    leverage_today: float | None = None
    leverage_peak: float | None = None
    gap_3y: float | None = None
    gap_3y_floor: float | None = None  # known components only, when unsized edges leave the gap n/d
    missing: list[str] = field(default_factory=list)
    basis: dict[str, Any] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)  # plain-words n/d reasons (leveragePath, fundingGap)


def _b(x: float | None) -> float | None:
    return None if x is None else round(x / 1e9, 3)


def fiscal_end(fin: Financials) -> date | None:
    """The latest fiscal-year end with a filed 12-month operating cash flow or capex."""
    ends = [r["valid_from"] for f in ("operating_cash_flow_12m_usd", "capex_12m_usd") for r in fin.rows.get(f, [])]
    return max(ends) if ends else None


def filed_fiscal_ends(fin: Financials) -> list[date]:
    """Fiscal-year ends with a filed 12-month operating cash flow or capex (annual reports only: a value whose
    companyfacts record names a quarterly `fp` is skipped), oldest first."""
    ends = set()
    for f in ("operating_cash_flow_12m_usd", "capex_12m_usd"):
        for r in fin.rows.get(f, []):
            try:
                record = json.loads(r.get("quote") or "{}")
            except ValueError:
                record = {}
            if not isinstance(record, dict) or record.get("fp") in (None, "FY"):
                ends.add(r["valid_from"])
    return sorted(ends)


def filed_leverage(fin: Financials, end: date) -> tuple[float | None, dict[str, Any]]:
    """(debt + lease liabilities at the fiscal year end) / that fiscal year's EBITDA, as filed."""
    debt, leases, ebitda = fin.debt(end), fin.leases(end), fin.ebitda_fy(end)
    basis = {"debt": _fig(debt), "leaseLiabilities": _fig(leases), "ebitda": _fig(ebitda)}
    if debt is None or leases is None or ebitda is None or ebitda.usd <= 0:
        return None, basis
    return round((debt.usd + leases.usd) / ebitda.usd, 2), basis


def _listed(labels: list[str]) -> str:
    return ", ".join(labels[:-1]) + f" and {labels[-1]}" if len(labels) > 1 else "".join(labels)


def path(
    fin: Financials, edges: list[graph.Edge], day: date, method: str, names: dict[str, str] | None = None
) -> Path:
    p = Path()
    names = names or {}
    payer = names.get(fin.entity_id, fin.entity_id)

    def m(value: Any) -> dict[str, Any]:
        return m_value(value, day, method, source_name="Marden payer capacity", uncalibrated=True)

    def edge_names(ids: list[str]) -> str:
        by_id = {e.id: e for e in edges}
        return _listed(sorted({names.get(by_id[i].payee, by_id[i].payee) for i in ids if i in by_id}))

    # Leverage reads one balance sheet: debt and lease liabilities at the same period end, over the twelve
    # months of EBITDA to that end. Cash flow and capex stay at the newest twelve months.
    bs_end = fin.balance_sheet_end()
    debt, leases = fin.debt(bs_end), fin.leases(bs_end)
    ebitda = fin.ebitda(bs_end) if bs_end is not None else fin.ebitda()
    ocf = fin.ttm("operating_cash_flow")
    capex = fin.ttm("capex") or fin.ttm("productive_asset_payments")
    for name, fig in (
        ("debt", debt),
        ("lease liabilities", leases),
        ("EBITDA", ebitda),
        ("operating cash flow", ocf),
        ("capex", capex),
    ):
        if fig is None:
            p.missing.append(f"entity {fin.entity_id}: {name} (SEC XBRL)")
    # Lease-adjusted leverage needs both debt and lease liabilities: a missing one is n/d, never 0.
    base_liab = debt.usd + leases.usd if debt is not None and leases is not None and bs_end is not None else None
    if debt is not None and leases is not None and bs_end is None:
        p.missing.append(f"entity {fin.entity_id}: debt and lease liabilities filed for the same period end")
    if base_liab is not None and ebitda is not None and ebitda.usd > 0:
        p.leverage_today = round(base_liab / ebitda.usd, 2)
    elif ebitda is not None and ebitda.usd <= 0:
        p.missing.append(f"entity {fin.entity_id}: EBITDA is not positive (leverage not meaningful)")
    filed = filed_fiscal_ends(fin)[-FILED_YEARS:]
    filed_basis = {}
    for end in filed:
        lev, filed_basis[f"FY{end.year}"] = filed_leverage(fin, end)
        ocf_fy = fin.fiscal_year("operating_cash_flow", end)
        capex_fy = fin.fiscal_year("capex", end) or fin.fiscal_year("productive_asset_payments", end)
        p.years.append(
            {
                "fiscalYear": f"FY{end.year}",
                "ocfB": ocf_fy.sourced_b() if ocf_fy else fact_value(None, as_of=end, tag="F"),
                "capexB": capex_fy.sourced_b() if capex_fy else fact_value(None, as_of=end, tag="F"),
                # commitments and the gap are projections; a filed year shows its filed cash flow only
                "leaseCommencingB": m(None),
                "computeCommitmentsB": m(None),
                "fundingGapB": m(None),
                "leaseAdjLeverage": m(lev),
            }
        )
    fy = fiscal_end(fin)
    p.basis = {
        "debt": _fig(debt),
        "leaseLiabilities": _fig(leases),
        "ebitda": _fig(ebitda),
        "ocf": _fig(ocf),
        "capex": _fig(capex),
        "fiscalYearEnd": fy.isoformat() if fy else None,
        "projection": "trailing twelve months held flat (no guidance collected)",
        "filedLeverage": filed_basis,
    }
    if fy is None:
        p.missing.append(f"entity {fin.entity_id}: a filed fiscal year (SEC XBRL)")
        p.reasons["leveragePath"] = p.reasons["fundingGap"] = f"No fiscal year filed by {payer} in SEC XBRL"
        return p
    own = [e for e in edges if e.payer == fin.entity_id and e.pays]
    unsized = [e.id for e in own if not e.schedule() and e.total is None]
    if unsized:
        p.missing.append(
            f"entity {fin.entity_id}: {len(unsized)} lease / compute edges not sized ({', '.join(unsized[:4])})"
        )
    # An edge whose amount, start or term is unknown leaves that year's sum unknown (n/d, never 0): the
    # known part is kept in the basis as a floor (the unknown payments can only add to it).
    undated_leases = [e.id for e in own if e.type in LEASE_TYPES and (e.total is None or e.start is None)]
    unscheduled = {t: [e.id for e in own if e.type in types and not e.schedule()]
                   for t, types in (("lease", LEASE_TYPES), ("compute", COMPUTE_TYPES))}
    for what, ids in (("leases with no total or start (commencing n/d)", undated_leases),
                      ("leases with no payment schedule (rent n/d)", unscheduled["lease"]),
                      ("compute / chip contracts with no payment schedule (payments n/d)", unscheduled["compute"])):
        if ids:
            p.missing.append(f"entity {fin.entity_id}: {len(ids)} {what}: {', '.join(ids)}")
    added: float | None = 0.0
    gaps: list[float | None] = []
    floors: list[float] = []
    levs = []
    for year in range(fy.year + 1, fy.year + 1 + YEARS_AHEAD):
        known_commencing = sum(e.total.usd for e in own if e.type in LEASE_TYPES and e.start == year and e.total)
        known_compute = sum(e.annual_usd(year) or 0.0 for e in own if e.type in COMPUTE_TYPES)
        known_rent = sum(e.annual_usd(year) or 0.0 for e in own if e.type in LEASE_TYPES)
        commencing = None if undated_leases else known_commencing
        compute = None if unscheduled["compute"] else known_compute
        rent = None if unscheduled["lease"] else known_rent
        added = None if added is None or commencing is None else added + commencing
        gap = None
        if ocf is not None and capex is not None:
            floor = capex.usd + known_compute + known_rent - ocf.usd
            floors.append(floor)
            gap = floor if compute is not None and rent is not None else None
        gaps.append(gap)
        lev = None
        if base_liab is not None and added is not None and ebitda is not None and ebitda.usd > 0:
            lev = round((base_liab + added) / ebitda.usd, 2)
        if lev is not None:
            levs.append(lev)
        p.years.append(
            {
                "fiscalYear": f"FY{year}",
                "ocfB": m(_b(ocf.usd) if ocf else None),
                "capexB": m(_b(capex.usd) if capex else None),
                "leaseCommencingB": m(_b(commencing)),
                "computeCommitmentsB": m(_b(compute)),
                "fundingGapB": m(_b(gap)),
                "leaseAdjLeverage": m(lev),
            }
        )
    if p.leverage_today is not None and len(levs) == YEARS_AHEAD:
        p.leverage_peak = max([p.leverage_today, *levs])
    if len(gaps) == YEARS_AHEAD and all(g is not None for g in gaps):
        p.gap_3y = round(sum(g for g in gaps if g is not None) / 1e9, 3)
    elif len(floors) == YEARS_AHEAD:
        p.gap_3y_floor = round(sum(floors) / 1e9, 3)
        p.basis["fundingGap3yFloorB"] = p.gap_3y_floor
    _explain(p, payer, fy, own, undated_leases, unscheduled, edge_names, {
        "debt": debt, "lease liabilities": leases, "EBITDA": ebitda, "operating cash flow": ocf, "capex": capex,
    })
    return p


def _explain(
    p: Path, payer: str, fy: date, own: list[graph.Edge], undated: list[str], unscheduled: dict[str, list[str]],
    edge_names: Any, figures: dict[str, Any],
) -> None:
    """Plain-words reasons for an n/d projected leverage path or 3-year funding gap (the inputs that are not
    disclosed, by name)."""
    span = f"FY{fy.year + 1}\u2013FY{fy.year + YEARS_AHEAD}"
    leases = [e for e in own if e.type in LEASE_TYPES]
    if p.leverage_peak is None:
        absent = [k for k in ("debt", "lease liabilities", "EBITDA") if figures[k] is None]
        if absent:
            p.reasons["leveragePath"] = f"{span} n/d: {payer}\u2019s {_listed(absent)} not filed in SEC XBRL"
        elif undated:
            which = (
                f"{len(undated)} of {payer}\u2019s {len(leases)} leases on the obligation map have"
                if len(undated) < len(leases)
                else f"{payer}\u2019s {'lease' if len(leases) == 1 else f'{len(leases)} leases'} on the obligation "
                f"map {'has' if len(leases) == 1 else 'have'}"
            )
            p.reasons["leveragePath"] = (
                f"{span} n/d: {which} no disclosed size or start year ({edge_names(undated)}), so leases "
                "commencing by year cannot be summed"
            )
        elif figures["EBITDA"] is not None and figures["EBITDA"].usd <= 0:
            p.reasons["leveragePath"] = f"{span} n/d: {payer}\u2019s EBITDA is not positive"
    if p.gap_3y is None:
        absent = [k for k in ("operating cash flow", "capex") if figures[k] is None]
        unknown = unscheduled["lease"] + unscheduled["compute"]
        if absent:
            p.reasons["fundingGap"] = f"n/d: {payer}\u2019s {_listed(absent)} not filed in SEC XBRL"
        elif unknown:
            floor = (
                f"; the scheduled payments alone leave a gap of at least ${p.gap_3y_floor:.1f}B"
                if p.gap_3y_floor is not None and p.gap_3y_floor > 0
                else ""
            )
            p.reasons["fundingGap"] = (
                f"n/d: no disclosed payment schedule for {len(unknown)} contracts {payer} owes "
                f"({edge_names(unknown)}){floor}"
            )


def _fig(f: Figure | None) -> dict[str, Any] | None:
    return None if f is None else {"b": _b(f.usd), "asOf": f.as_of.isoformat(), "basis": f.basis}
