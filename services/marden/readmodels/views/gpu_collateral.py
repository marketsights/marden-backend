"""Read model `gpu_collateral` (instrument.id): GET /api/instruments/:id/collateral, GpuCollateral
(spec 19 "Collateral" card on Instrument detail).

Built for GPU-backed facilities only (an instrument with a 'gpu_collateral' model output or GPU
collateral facts); others have no document. Each value comes from the 'gpu_collateral' model output
when it carries it, else from the instrument's facts:

- model: the 'gpu_model' fact (else the collateral record's chip);
- units, value today, LTV today and by quarter: the `gpu_collateral` fact (M, the archive's GPU
  loan-to-value table: base unit count, conservative chip value, balance / chip value);
- balance: the filed drawn amount (`drawn_usd`, F) when it is the balance the LTV uses, else the
  collateral record's balance;
- recovery if the customer walks: `recovery_gpu` (M, conservative-moderate chip-only recovery);
- deployment (delivered vs energized): the 'gpu_deployment' model output for this facility, omitted
  until the model has run.
"""

from __future__ import annotations

from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.context import known_facts
from marden.readmodels.registry import builder
from marden.readmodels.views.instrument_inputs import (
    all_instrument_ids,
    fact_sourced,
    latest_output,
    model_sourced,
    nd,
    num,
    output_field,
    sourced_number,
)
from marden.readmodels.views.recovery_text import gpu_recovery_text

GPU_FACTS = ("gpu_collateral", "gpu_model", "recovery_gpu", "drawn_usd")
DEPLOYMENT_SOURCED = (
    "unitsFinanced",
    "unitsDelivered",
    "unitsEnergized",
    "idleUnits",
    "idleShare",
    "monthsIdle",
    "idleInterestM",
    "valueLostWhileIdleM",
    "ltvToday",
    "ltvIfOnSchedule",
)
DEPLOYMENT_STATUSES = ("Earning", "Partly idle", "Idle", "n/d")


def quarter_label(month: str) -> str | None:
    """'2026-10' -> "Q4 '26"."""
    try:
        year, mon = int(month[:4]), int(month[5:7])
    except (TypeError, ValueError):
        return None
    if not 1 <= mon <= 12:
        return None
    return f"Q{(mon - 1) // 3 + 1} '{year % 100:02d}"


def ltv_by_quarter(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Balance / conservative chip value per scheduled month, one point per quarter (the last one)."""
    by_quarter: dict[str, float] = {}
    for p in record.get("ltv_by_quarter") or []:
        if not isinstance(p, dict):
            continue
        balance, chips, label = (
            num(p.get("balance_usd")),
            num(p.get("chips_conservative_usd")),
            quarter_label(p.get("month")),
        )
        if balance is None or not chips or label is None:
            continue
        by_quarter[label] = round(balance / chips, 4)
    return [{"quarter": q, "ltv": v} for q, v in by_quarter.items()]


def _from_output(out: Row | None, key: str, scale: float = 1.0) -> dict[str, Any] | None:
    got = sourced_number(out, key)
    if got is not None and got["value"] is not None and scale != 1.0:
        got["value"] = round(got["value"] * scale, 4)
    return got if got is not None and got["value"] is not None else None


def _quarters_from_output(out: Row | None) -> list[dict[str, Any]] | None:
    raw = output_field(out, "ltvByQuarter")
    if not isinstance(raw, list):
        return None
    rows = [
        {"quarter": r["quarter"], "ltv": float(r["ltv"])}
        for r in raw
        if isinstance(r, dict) and isinstance(r.get("quarter"), str) and num(r.get("ltv")) is not None
    ]
    return rows or None


def _recovery_from_output(out: Row | None) -> dict[str, Any] | None:
    raw = output_field(out, "recoveryIfCustomerWalks", "recovery")
    if raw is None or out is None:
        return None
    got = model_sourced(out, raw)
    if got is None or got["value"] is None:
        return None
    value = got["value"]
    if num(value) is not None:
        n = float(value)
        got["value"] = f"{round(n * 100 if n <= 1 else n)}c"
    elif not isinstance(value, str):
        return None
    return got


def deployment(conn: Conn, instrument_id: str) -> dict[str, Any] | None:
    """GpuDeployment for this facility from the 'gpu_deployment' model (its own output, or its row in
    the page output for 'all'); None until the model has run or when the output lacks a field."""
    out = latest_output(conn, "gpu_deployment", instrument_id, "instrument")
    body: Any = out["output"] if out is not None else None
    if out is None:
        out = latest_output(conn, "gpu_deployment", "all")
        facilities = output_field(out, "facilities")
        body = (
            next((f for f in facilities if isinstance(f, dict) and f.get("id") == instrument_id), None)
            if isinstance(facilities, list)
            else None
        )
    if out is None or not isinstance(body, dict):
        return None
    fields: dict[str, Any] = {}
    for key in DEPLOYMENT_SOURCED:
        got = model_sourced(out, body.get(key))
        if got is None:
            return None
        fields[key] = got
    assumptions = body.get("assumptions")
    if not isinstance(assumptions, dict) or num(assumptions.get("pue")) is None:
        return None
    kw, coupon = model_sourced(out, assumptions.get("kwPerUnit")), model_sourced(out, assumptions.get("couponPct"))
    if kw is None or coupon is None or body.get("status") not in DEPLOYMENT_STATUSES:
        return None
    if body.get("scope") not in ("facility", "site") or not isinstance(body.get("name"), str):
        return None
    return {
        "scope": body["scope"],
        "id": str(body.get("id") or instrument_id),
        "name": body["name"],
        "model": body.get("model") if isinstance(body.get("model"), str) else None,
        **fields,
        "status": body["status"],
        "missing": [str(m) for m in body.get("missing") or []],
        "evidence": [e for e in body.get("evidence") or [] if isinstance(e, dict)],
        "assumptions": {
            "kwPerUnit": kw,
            "pue": float(assumptions["pue"]),
            "couponPct": coupon,
            "methodVersion": str(assumptions.get("methodVersion") or kw.get("methodVersion") or ""),
        },
    }


def _balance(record_row: Row, record: dict[str, Any], drawn: Row | None) -> dict[str, Any]:
    balance = num(record.get("balance_usd"))
    if drawn is not None and balance is not None and num(drawn["value"]) == balance:
        return fact_sourced(drawn, round(balance / 1e9, 4))  # type: ignore[return-value]
    return fact_sourced(record_row, round(balance / 1e9, 4)) if balance is not None else nd("F")  # type: ignore[return-value]


@builder("gpu_collateral", keys=all_instrument_ids)
def build_gpu_collateral(conn: Conn, key: str) -> dict[str, Any] | None:
    facts = known_facts(conn, "instrument", key, GPU_FACTS)
    out = latest_output(conn, "gpu_collateral", key, "instrument")
    row = facts.get("gpu_collateral")
    record: dict[str, Any] = row["value"] if row is not None and isinstance(row["value"], dict) else {}
    model = output_field(out, "model")
    if not isinstance(model, str):
        model_fact = facts.get("gpu_model")
        model = model_fact["value"] if model_fact and isinstance(model_fact["value"], str) else record.get("chip")
    if (out is None and row is None) or not isinstance(model, str) or not model:
        return None
    units = (record.get("units") or {}) if isinstance(record.get("units"), dict) else {}
    chips_today = num(record.get("chips_value_conservative_usd"))
    ltv_pct = num(record.get("ltv_pct"))
    rec_row = facts.get("recovery_gpu")
    rec_text = gpu_recovery_text(rec_row["value"]) if rec_row and isinstance(rec_row["value"], dict) else None
    return {
        "instrumentId": key,
        "model": model,
        "units": _from_output(out, "units")
        or (fact_sourced(row, num(units.get("base"))) if row and num(units.get("base")) is not None else nd("M")),
        "valueTodayB": _from_output(out, "valueTodayB")
        or (fact_sourced(row, round(chips_today / 1e9, 4)) if row and chips_today is not None else nd("M")),
        "balanceB": _from_output(out, "balanceB")
        or (_balance(row, record, facts.get("drawn_usd")) if row else nd("F")),
        "ltvToday": _from_output(out, "ltvToday")
        or (fact_sourced(row, round(ltv_pct / 100, 4)) if row and ltv_pct is not None else nd("M")),
        "ltvByQuarter": _quarters_from_output(out) or ltv_by_quarter(record),
        "recoveryIfCustomerWalks": _recovery_from_output(out)
        or (fact_sourced(rec_row, rec_text) if rec_row and rec_text else nd("M")),
        **({"deployment": dep} if (dep := deployment(conn, key)) is not None else {}),
    }
