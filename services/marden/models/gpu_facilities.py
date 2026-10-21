"""GPU-backed facilities and their collateral value (spec 19 steps 3-5), shared by gpu_model (facilities
per model), gpu_collateral, gpu_deployment and recovery.

A facility is an instrument with a `gpu_model` or `gpu_collateral` fact. Inputs, read point in time:
- model: `gpu_model` (a pool naming several models, 'H100/H200', is valued at its lowest-valued model);
- units: the `gpu_collateral` record's base unit count (the archive's estimate from the financed GPU cost
  and per-GPU prices, tag M, sourced to the credit agreement), passed through with its own tag. The
  record sizes a facility as if fully drawn; a delayed-draw facility draws as GPUs are bought, so while
  the filed drawn balance is below the record's balance the units financed so far are scaled by
  drawn / record balance (then tag M, this model);
- pools: facilities with the same collateral record (one security package, e.g. IREN's DDTL and USPP
  notes) are one pool: claims pari passu, LTV and recovery on the pool's total balance;
- unit age: the record's purchase month (else the model's first volume shipment);
- balance: the filed drawn amount (`drawn_usd`, F), else the record's balance; the balance path by quarter
  is the record's amortization schedule, scaled to the balance used;
- useful life: the borrower's (or its parent's) filed depreciation policy, else the median policy;
- coupon: `coupon_rate_pct`.
Value today = units x value per unit (gpu_value) x (1 - haircut); LTV = balance / value; recovery if the
customer walks = collateral value / balance (capped at 100c), over a default today to a year out, after
senior claims (none known ahead of these first-lien SPV facilities).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.core.db import Conn, Row
from marden.models import pit
from marden.models.gpu_inputs import Market, chip_key, ship_date
from marden.models.gpu_value import HAIRCUT, Fit, unit_value, years
from marden.models.sourced import num

FACILITY_FIELDS = ("gpu_model", "gpu_collateral", "drawn_usd", "commitment_usd", "amount_usd", "coupon_rate_pct",
                   "maturity")
DASH = "\u2013"
DEFAULT_WINDOW_QUARTERS = 4  # recovery range: customer walks today ... a year out


@dataclass
class Facility:
    instrument_id: str
    name: str
    site_slug: str | None
    entity_ids: list[str]
    facts: dict[str, Row] = field(default_factory=dict)

    @property
    def record(self) -> dict[str, Any]:
        row = self.facts.get("gpu_collateral")
        return row["value"] if row is not None and isinstance(row["value"], dict) else {}

    @property
    def model_text(self) -> str | None:
        row = self.facts.get("gpu_model")
        text = row["value"] if row is not None and isinstance(row["value"], str) else self.record.get("chip")
        return text if isinstance(text, str) and text else None

    @property
    def models(self) -> list[str]:
        parts = [chip_key(p) for p in (self.model_text or "").replace(",", "/").split("/")]
        return [p for p in parts if p]

    @property
    def units(self) -> float | None:
        units = self.record.get("units")
        return num(units.get("base")) if isinstance(units, dict) else None

    @property
    def pool_key(self) -> str:
        loan = self.record.get("loan")
        return loan if isinstance(loan, str) and loan else self.instrument_id

    def units_financed(self, pool_balance: float | None) -> tuple[float | None, bool]:
        """Units financed so far for the pool's drawn balance, and whether they were scaled."""
        units, sized_on = self.units, num(self.record.get("balance_usd"))
        if units is None or pool_balance is None or not sized_on or pool_balance >= sized_on:
            return units, False
        return units * pool_balance / sized_on, True

    def purchase(self, market: Market, model: str) -> date | None:
        month = self.record.get("purchase_month")
        bought = ship_date(month) if isinstance(month, str) else None
        chip = market.chips.get(model)
        return bought or (chip.launch if chip else None)

    def balance(self) -> tuple[float | None, Row | None]:
        drawn = self.facts.get("drawn_usd")
        if drawn is not None and (v := num(drawn["value"])):
            return v, drawn
        v = num(self.record.get("balance_usd"))
        return v, self.facts.get("gpu_collateral") if v else None

    def schedule(self, balance: float) -> list[tuple[str, float]]:
        """(month 'YYYY-MM', balance) from the record's amortization path, scaled to `balance`."""
        path: list[tuple[str, float]] = []
        for p in self.record.get("ltv_by_quarter") or []:
            month, owed = (p.get("month"), num(p.get("balance_usd"))) if isinstance(p, dict) else (None, None)
            if isinstance(month, str) and owed is not None:
                path.append((month, owed))
        if not path or not path[0][1]:
            return []
        scale = balance / path[0][1]
        return [(m, b * scale) for m, b in path]


def load(conn: Conn, day: date, ids: list[str] | None = None) -> list[Facility]:
    rows = pit.fact_rows(conn, day, "instrument", ids, list(FACILITY_FIELDS))
    by_id: dict[str, dict[str, list[Row]]] = {}
    for r in rows:
        by_id.setdefault(r["subject_id"], {}).setdefault(r["field"], []).append(r)
    gpu_ids = sorted(i for i, f in by_id.items() if "gpu_model" in f or "gpu_collateral" in f)
    if not gpu_ids:
        return []
    meta = {r["id"]: r for r in conn.execute(
        """SELECT i.id, i.name, i.site_slug, i.issuer_entity_id, e.parent_id FROM instrument i
             LEFT JOIN entity e ON e.id = i.issuer_entity_id WHERE i.id = ANY(%s)""", (gpu_ids,)).fetchall()}
    out = []
    for iid in gpu_ids:
        m = meta.get(iid)
        if m is None:
            continue
        facts = {f: best for f, rs in by_id[iid].items() if (best := pit.current_of(rs)) is not None}
        entities = [e for e in (m["issuer_entity_id"], m["parent_id"]) if e]
        out.append(Facility(iid, m["name"], m["site_slug"], entities, facts))
    return out


def quarter_label(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    return f"Q{(mon - 1) // 3 + 1} '{year % 100:02d}"


def _month_years(day: date, month: str) -> float:
    return years(day, date(int(month[:4]), int(month[5:7]), 1))


@dataclass
class Valuation:
    model: str | None
    units: float | None
    units_scaled: bool
    pool: list[str]
    pool_balance: float | None
    life: float | None
    life_row: Row | None
    unit_age: float | None
    per_unit: float | None
    value: float | None  # after haircut, USD
    balance: float | None
    balance_row: Row | None
    ltv: float | None
    ltv_by_quarter: list[dict[str, Any]]
    recovery_low: float | None
    recovery_high: float | None
    missing: list[str]


def pools(facilities: list[Facility]) -> dict[str, list[Facility]]:
    out: dict[str, list[Facility]] = {}
    for f in facilities:
        out.setdefault(f.pool_key, []).append(f)
    return out


def value_facility(f: Facility, market: Market, fit: Fit | None, day: date,
                   pool: list[Facility] | None = None) -> Valuation:
    missing: list[str] = []
    balance, balance_row = f.balance()
    members = pool or [f]
    member_balances = [m.balance()[0] for m in members]
    pool_balance = sum(b for b in member_balances if b) if all(member_balances) else None
    units, scaled = f.units_financed(pool_balance)
    life_row = market.life_of(f.entity_ids)
    life = num(life_row["value"]) if life_row else market.median_life()
    if not f.models:
        missing.append("GPU model of the collateral")
    if units is None:
        missing.append("GPU units financed")
    if balance is None or pool_balance is None:
        missing.append("facility balance (drawn amount)" + ("" if balance is None else " of a pool member"))
    if fit is None:
        missing.append("GPU rental index with at least three models (decline fit)")
    if life is None:
        missing.append("useful life (depreciation policy)")
    best: tuple[float, str, float] | None = None
    if fit is not None and life is not None:
        for candidate in f.models:
            bought = f.purchase(market, candidate)
            if bought is None:
                missing.append(f"{candidate} purchase or launch date")
                continue
            v = unit_value(market, fit, candidate, day, life, years(bought, day))
            if v is None:
                missing.append(f"{candidate} index price or discount rate on or before the day")
            elif best is None or v < best[0]:
                best = (v, candidate, years(bought, day))
    per_unit: float | None = best[0] if best else None
    model: str | None = best[1] if best else (f.models[0] if f.models else None)
    age: float | None = best[2] if best else None
    value = per_unit * units * (1 - HAIRCUT) if per_unit is not None and units is not None else None
    ltv = pool_balance / value if value and pool_balance else None
    quarters: list[dict[str, Any]] = []
    rec: list[float] = []
    if value and pool_balance and units and model and age is not None and life is not None and fit is not None:
        sched = f.schedule(pool_balance) or [(day.strftime("%Y-%m"), pool_balance)]
        seen: dict[str, float] = {}
        for k, (month, bal) in enumerate(sched):
            ahead = max(_month_years(day, month), 0.0)
            v = unit_value(market, fit, model, day, life, age, ahead)
            if v is None or v <= 0:
                continue
            coll = v * units * (1 - HAIRCUT)
            seen[quarter_label(month)] = round(bal / coll, 4)
            if k <= DEFAULT_WINDOW_QUARTERS:
                rec.append(min(1.0, coll / bal))
        quarters = [{"quarter": q, "ltv": v} for q, v in seen.items()]
    return Valuation(model, units, scaled, [m.instrument_id for m in members], pool_balance, life, life_row, age,
                     per_unit, value, balance, balance_row, ltv, quarters, min(rec) if rec else None,
                     max(rec) if rec else None, missing)


def range_text(low: float | None, high: float | None) -> str | None:
    """Cents per dollar, 'low-high' joined by an en dash, or one number when they round alike."""
    if low is None or high is None:
        return None
    lo, hi = round(min(low, high) * 100), round(max(low, high) * 100)
    return f"{lo}" if lo == hi else f"{lo}{DASH}{hi}"
