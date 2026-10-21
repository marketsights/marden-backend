"""Parse SEC XBRL companyfacts (data.sec.gov/api/xbrl/companyfacts/CIK##########.json) into entity facts.

companyfacts holds every non-dimensional fact a filer tagged with a standard taxonomy (us-gaap,
ifrs-full, dei, srt, ...): per concept and unit, one record per (filing, period) with `start` (durations),
`end`, `val`, `accn`, `fy`, `fp`, `form`, `filed` and sometimes `frame`. Company extension concepts and
dimensional facts are not in it, so values the filers tag only on an axis (PP&E by class on most
neoclouds, and every "lease not yet commenced" amount: us-gaap has no non-dimensional element for it)
never appear here; those stay with the extraction agents.

Rules:
- `FIELDS` maps each Marden field to an ordered list of concepts that are the same quantity (a US GAAP
  name and its IFRS twin). Within one filing the first listed concept it reported wins; across filings
  every value counts (a switch from IFRS to US GAAP re-presents prior periods). quote_loc names the
  concept. Look-alikes that filers report side by side with different values (Revenues vs revenue from
  contracts with customers, PP&E payments vs productive-asset payments) are separate fields.
- Durations become `<base>_<n>m_usd` for 3, 6, 9 and 12-month periods (13/14-week quarters and 52/53-week
  years included); stub periods (e.g. 2 or 7 months) are skipped and counted. Instants become
  `<base>_usd`; share counts carry no `_usd`.
- Only periodic reports count: 10-K, 10-Q, 10-KT, 10-QT, 20-F, 40-F, 6-K and their amendments (not S-1,
  8-K or proxy data). Only the field's own unit (USD or shares): other currencies are skipped.
- Each distinct value of a (field, period) is one observation, attributed to the earliest filing that
  reported it. A restatement is a second value of the same period from the restating filing, so writing
  observations oldest filing first leaves the latest filed value on top in `fact_current`.
- `public_date` per period end: the filing date of the first filing that reported a value for it.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.collectors.sec_index_parse import base_form, cik10, normalize_cik

API = "https://data.sec.gov/api/xbrl/companyfacts"
DURATION, INSTANT = "duration", "instant"
USD, SHARES = "USD", "shares"
PERIODIC_FORMS = frozenset({"10-K", "10-Q", "10-KT", "10-QT", "20-F", "40-F", "6-K"})
MONTHS = (3, 6, 9, 12)
DAYS_PER_MONTH = 30.4375
MONTH_TOLERANCE_DAYS = 12
PUBLIC_DATE = "public_date"


def companyfacts_url(cik: str) -> str:
    return f"{API}/CIK{cik10(cik)}.json"


@dataclass(frozen=True)
class FieldSpec:
    base: str
    kind: str
    unit: str
    concepts: tuple[str, ...]  # 'us-gaap:Name', 'ifrs-full:Name', 'dei:Name' in priority order

    def name(self, months: int | None = None) -> str:
        suffix = "" if self.unit == SHARES else "_usd"
        if self.kind == DURATION:
            return f"{self.base}_{months}m{suffix}"
        return f"{self.base}{suffix}"


def _spec(base: str, kind: str, unit: str, *concepts: str) -> FieldSpec:
    return FieldSpec(base, kind, unit, concepts)


FIELDS: tuple[FieldSpec, ...] = (
    # Concepts share a field only when they are the same quantity (a US GAAP name and its IFRS twin);
    # look-alikes that filers report side by side with different values get their own field.
    _spec("operating_cash_flow", DURATION, USD, "us-gaap:NetCashProvidedByUsedInOperatingActivities",
          "ifrs-full:CashFlowsFromUsedInOperatingActivities"),
    _spec("operating_cash_flow_continuing", DURATION, USD,
          "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    _spec("capex", DURATION, USD, "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
          "ifrs-full:PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"),
    # Amazon and NVIDIA tag their capex line this way (PP&E plus intangibles for NVIDIA).
    _spec("productive_asset_payments", DURATION, USD, "us-gaap:PaymentsToAcquireProductiveAssets"),
    _spec("revenue", DURATION, USD, "us-gaap:Revenues", "ifrs-full:Revenue"),
    # Earnings lines for EBITDA (payer_capacity: lease-adjusted leverage = (debt + leases) / EBITDA).
    # Filers tag depreciation alone (Oracle, Microsoft, Alphabet) or depreciation and amortization in one
    # line (Amazon, Meta, CoreWeave): separate fields, with intangible amortization on its own.
    _spec("operating_income", DURATION, USD, "us-gaap:OperatingIncomeLoss",
          "ifrs-full:ProfitLossFromOperatingActivities"),
    _spec("depreciation_amortization", DURATION, USD, "us-gaap:DepreciationDepletionAndAmortization",
          "us-gaap:DepreciationAndAmortization", "ifrs-full:DepreciationAndAmortisationExpense"),
    _spec("depreciation", DURATION, USD, "us-gaap:Depreciation", "ifrs-full:DepreciationExpense"),
    _spec("amortization_intangibles", DURATION, USD, "us-gaap:AmortizationOfIntangibleAssets",
          "ifrs-full:AmortisationExpense"),
    _spec("contract_revenue", DURATION, USD, "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
          "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax", "ifrs-full:RevenueFromContractsWithCustomers"),
    _spec("cash", INSTANT, USD, "us-gaap:CashAndCashEquivalentsAtCarryingValue", "ifrs-full:CashAndCashEquivalents"),
    # Total debt only where the filer tags a total; the long-term debt pieces are their own fields.
    _spec("total_debt", INSTANT, USD, "us-gaap:DebtLongtermAndShorttermCombinedAmount", "ifrs-full:Borrowings"),
    _spec("long_term_debt", INSTANT, USD, "us-gaap:LongTermDebt"),  # includes current maturities
    _spec("long_term_debt_current", INSTANT, USD, "us-gaap:LongTermDebtCurrent"),
    _spec("long_term_debt_noncurrent", INSTANT, USD, "us-gaap:LongTermDebtNoncurrent"),
    _spec("debt_current", INSTANT, USD, "us-gaap:DebtCurrent"),
    _spec("short_term_borrowings", INSTANT, USD, "us-gaap:ShortTermBorrowings", "ifrs-full:ShorttermBorrowings"),
    _spec("commercial_paper", INSTANT, USD, "us-gaap:CommercialPaper"),
    _spec("operating_lease_liability", INSTANT, USD, "us-gaap:OperatingLeaseLiability"),
    _spec("operating_lease_liability_current", INSTANT, USD, "us-gaap:OperatingLeaseLiabilityCurrent"),
    _spec("operating_lease_liability_noncurrent", INSTANT, USD, "us-gaap:OperatingLeaseLiabilityNoncurrent"),
    _spec("finance_lease_liability", INSTANT, USD, "us-gaap:FinanceLeaseLiability"),
    _spec("finance_lease_liability_current", INSTANT, USD, "us-gaap:FinanceLeaseLiabilityCurrent"),
    _spec("finance_lease_liability_noncurrent", INSTANT, USD, "us-gaap:FinanceLeaseLiabilityNoncurrent"),
    _spec("lease_liability", INSTANT, USD, "ifrs-full:LeaseLiabilities"),  # IFRS 16: one lease liability
    _spec("construction_in_progress", INSTANT, USD, "us-gaap:ConstructionInProgressGross",
          "ifrs-full:ConstructionInProgress"),
    _spec("ppe_net", INSTANT, USD, "us-gaap:PropertyPlantAndEquipmentNet", "ifrs-full:PropertyPlantAndEquipment"),
    _spec("ppe_gross", INSTANT, USD, "us-gaap:PropertyPlantAndEquipmentGross"),
    _spec("ppe_and_finance_lease_rou_net", INSTANT, USD,
          "us-gaap:PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization"),
    _spec("purchase_obligation", INSTANT, USD, "us-gaap:PurchaseObligation"),
    _spec("unrecorded_purchase_obligation", INSTANT, USD,
          "us-gaap:UnrecordedUnconditionalPurchaseObligationBalanceSheetAmount"),
    _spec("long_term_purchase_commitment", INSTANT, USD, "us-gaap:LongTermPurchaseCommitmentAmount"),
    # Backlog (spec 24 "Concentration": a payer's share of the payee's backlog).
    _spec("remaining_performance_obligation", INSTANT, USD, "us-gaap:RevenueRemainingPerformanceObligation"),
    _spec("shares_outstanding", INSTANT, SHARES, "dei:EntityCommonStockSharesOutstanding"),  # cover page date
    _spec("common_shares_outstanding", INSTANT, SHARES, "us-gaap:CommonStockSharesOutstanding",
          "ifrs-full:NumberOfSharesOutstanding"),
)


@dataclass(frozen=True)
class Record:
    concept: str
    unit: str
    start: date | None
    end: date
    value: int | float
    accession: str
    form: str
    filed: date
    fy: int | None
    fp: str | None
    raw: str  # the record as it stands in the file (compact JSON): the fact's quote

    @property
    def order(self) -> tuple[date, str]:
        return (self.filed, self.accession)


@dataclass(frozen=True)
class CompanyFacts:
    cik: str
    name: str
    concepts: dict[str, dict[str, list[dict[str, Any]]]]  # 'taxonomy:Concept' -> unit -> raw records

    def accessions_filed_since(self, day: date) -> list[str]:
        """Accessions of every record filed on or after `day` (any concept, unit or form)."""
        out = set()
        for units in self.concepts.values():
            for rows in units.values():
                out.update(r["accn"] for r in rows if str(r.get("filed", "")) >= day.isoformat() and r.get("accn"))
        return sorted(out)

    def latest_filed(self) -> date | None:
        dates = [r.get("filed") for units in self.concepts.values() for rows in units.values() for r in rows]
        return max((_iso(d) for d in dates if d), default=None)


def _iso(value: Any) -> date:
    return date.fromisoformat(str(value)[:10])


def parse_companyfacts(content: bytes | str) -> CompanyFacts:
    """Raises ValueError when the body is not a companyfacts document."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not JSON: {exc}") from None
    if not isinstance(data, dict) or "facts" not in data or not isinstance(data["facts"], dict):
        raise ValueError("not a companyfacts document (no 'facts' object)")
    cik = normalize_cik(data.get("cik"))
    if cik is None:
        raise ValueError(f"companyfacts has no valid cik: {data.get('cik')!r}")
    concepts: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for taxonomy, items in data["facts"].items():
        for concept, body in (items or {}).items():
            units = body.get("units") if isinstance(body, dict) else None
            if isinstance(units, dict):
                concepts[f"{taxonomy}:{concept}"] = {u: rows for u, rows in units.items() if isinstance(rows, list)}
    return CompanyFacts(cik, " ".join(str(data.get("entityName") or "").split()), concepts)


def _record(concept: str, unit: str, raw: dict[str, Any]) -> Record | None:
    try:
        value = raw["val"]
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        start = raw.get("start")
        fy = raw.get("fy")
        return Record(
            concept=concept, unit=unit, start=_iso(start) if start else None, end=_iso(raw["end"]), value=value,
            accession=str(raw["accn"]), form=str(raw["form"]), filed=_iso(raw["filed"]),
            fy=int(fy) if isinstance(fy, int) else None, fp=raw.get("fp") or None,
            raw=json.dumps(raw, separators=(",", ":"), ensure_ascii=False),
        )
    except (KeyError, TypeError, ValueError):
        return None


def months(start: date, end: date) -> int | None:
    """3, 6, 9 or 12 for a period of about that many months (start and end inclusive), else None."""
    days = (end - start).days + 1
    n = round(days / DAYS_PER_MONTH)
    if n in MONTHS and abs(days - n * DAYS_PER_MONTH) <= MONTH_TOLERANCE_DAYS:
        return n
    return None


@dataclass(frozen=True)
class Observation:
    field: str
    value: Any
    unit: str | None
    valid_from: date
    record: Record  # the earliest filing that reported this value (for public_date: that period's first)

    def quote_loc(self) -> str:
        r = self.record
        return f"{r.concept} [{r.unit}]; {r.form} {r.accession} filed {r.filed.isoformat()}"


@dataclass
class Selection:
    observations: list[Observation] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)
    concepts: Counter = field(default_factory=Counter)  # concept -> observations taken from it

    @property
    def values(self) -> int:
        return sum(1 for o in self.observations if o.field != PUBLIC_DATE)


def _field_for(spec: FieldSpec, rec: Record) -> tuple[str | None, str | None]:
    """(field name, skip reason)."""
    if spec.kind == INSTANT:
        return (spec.name(), None) if rec.start is None else (None, "not_instant")
    if rec.start is None:
        return None, "no_start"
    n = months(rec.start, rec.end)
    return (spec.name(n), None) if n else (None, "stub_period")


def _qualifies(rec: Record, since: date, today: date) -> str | None:
    if base_form(rec.form) not in PERIODIC_FORMS:
        return "form"
    if rec.end < since:
        return "before_since"
    if rec.end > today or rec.filed > today:
        return "future"
    return None


def _nominal_gap(rec: Record) -> float:
    days = (rec.end - rec.start).days + 1 if rec.start else 0
    n = months(rec.start, rec.end) if rec.start else None
    return abs(days - n * DAYS_PER_MONTH) if n else 0.0


def _period_records(ranked: list[tuple[int, Record]], sel: Selection) -> list[Record]:
    """One (field, period end)'s records: per filing only its first listed concept, one period start."""
    best: dict[str, int] = {}
    for rank, rec in ranked:
        best[rec.accession] = min(rank, best.get(rec.accession, rank))
    recs = [rec for rank, rec in ranked if rank == best[rec.accession]]
    sel.skipped["shadowed"] += len(ranked) - len(recs)
    if len({r.start for r in recs}) > 1:  # two periods of the same length end the same day: keep the closest fit
        keep = min(recs, key=lambda r: (_nominal_gap(r), -(r.start.toordinal() if r.start else 0))).start
        sel.skipped["period_conflict"] += sum(1 for r in recs if r.start != keep)
        recs = [r for r in recs if r.start == keep]
    return recs


def select(cf: CompanyFacts, *, since: date, today: date) -> Selection:
    """The observations to write for one filer (see module docstring), oldest filing first."""
    sel = Selection()
    for spec in FIELDS:
        periods: dict[tuple[str, date], list[tuple[int, Record]]] = defaultdict(list)  # (field, end) -> records
        for rank, concept in enumerate(spec.concepts):
            for unit, rows in cf.concepts.get(concept, {}).items():
                if unit != spec.unit:
                    sel.skipped["other_unit"] += len(rows)
                    continue
                for raw in rows:
                    rec = _record(concept, unit, raw)
                    reason = "malformed" if rec is None else _qualifies(rec, since, today)
                    name = None
                    if rec is not None and reason is None:
                        name, reason = _field_for(spec, rec)
                    if reason or rec is None or name is None:
                        sel.skipped[reason or "malformed"] += 1
                        continue
                    periods[(name, rec.end)].append((rank, rec))
        for (name, end), ranked in periods.items():
            first: dict[Any, Record] = {}
            for rec in sorted(_period_records(ranked, sel), key=lambda r: r.order):
                first.setdefault(rec.value, rec)
            for value, rec in first.items():
                sel.observations.append(Observation(name, value, spec.unit, end, rec))
                sel.concepts[rec.concept] += 1
    earliest: dict[date, Record] = {}
    for obs in sel.observations:
        if obs.valid_from not in earliest or obs.record.order < earliest[obs.valid_from].order:
            earliest[obs.valid_from] = obs.record
    for end, rec in earliest.items():
        sel.observations.append(Observation(PUBLIC_DATE, rec.filed.isoformat(), None, end, rec))
    sel.observations.sort(key=lambda o: (o.record.order, o.valid_from, o.field, o.field == PUBLIC_DATE))
    return sel
