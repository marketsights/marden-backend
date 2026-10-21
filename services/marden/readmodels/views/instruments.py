"""Read model `instruments` ('all'): GET /api/instruments, InstrumentRow[] (spec 06).

Every V1 instrument (instrument.in_v1) with its size, maturity, Marden price and 1w change (model
'price', else the model's own 'marden' price_obs), the yield where it traded (latest observed trade,
exchange, quote or dealer price that carries a yield), the mark gap (model 'mark_gap'; 'no fund'
when no N-PORT or BDC filing lists it) and the status of the chain it sits on (model 'status' on its
site). n/d values are null. `instrument_row` is shared with instrument_detail, so the list and the
detail page show the same numbers.
"""

from __future__ import annotations

from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.context import known_facts
from marden.readmodels.registry import builder
from marden.readmodels.views.instrument_inputs import (
    OBSERVED_KINDS,
    daily_series,
    fact_sourced,
    holder_lines,
    instrument_identity,
    latest_output,
    model_sourced,
    nd,
    num,
    output_field,
    price_rows,
    price_sourced,
    sourced_number,
    week_change,
)
from marden.readmodels.views.instrument_text import (
    billions,
    display_name,
    maturity_text,
    segment_rank,
    type_label,
)

CHAIN_STATUSES = ("Broken", "Strained", "Holding", "No build")
GAP_WORDS = ("no fund", "traded", "private")


def chain_status(conn: Conn, inst: Row) -> str | None:
    """The status of the chain the instrument sits on: model 'status' of its site (n/d without one)."""
    if not inst.get("site_slug"):
        return None
    out = latest_output(conn, "status", inst["site_slug"], "site")
    status = output_field(out, "status")
    if isinstance(status, dict):
        status = status.get("value")
    return status if status in CHAIN_STATUSES else None


def size_b(facts: dict[str, Row]) -> dict[str, Any]:
    row = facts.get("amount_usd") or facts.get("commitment_usd")
    value = billions(row["value"]) if row else None
    return fact_sourced(row, value) if row and value is not None else nd("F")  # type: ignore[return-value]


def marden_prices(conn: Conn, instrument_id: str) -> list[Row]:
    """The Marden daily price series (price_obs kind 'marden', one point per day)."""
    return daily_series(price_rows(conn, instrument_id, ("marden",)))


def marden_price(price_out: Row | None, series: list[Row]) -> dict[str, Any]:
    got = sourced_number(price_out, "price", "mardenPrice")
    if got is not None and got["value"] is not None:
        return got
    if series:
        return price_sourced(series[-1], float(series[-1]["price"]))
    return got or nd("M")


def change_1w(price_out: Row | None, series: list[Row], price: dict[str, Any]) -> dict[str, Any]:
    """Model 'price' change1w, else the change of the Marden daily series over a week. When the
    price exists but no change does (fewer than 8 days priced), n/d under the price's method."""
    got = sourced_number(price_out, "change1w", "change_1w")
    if got is not None and got["value"] is not None:
        return got
    found = week_change(series) or got
    if found is not None:
        return found
    missing = nd("M")
    if price.get("methodVersion"):
        missing["methodVersion"] = price["methodVersion"]
    return missing


def traded_yield(observed: list[Row]) -> dict[str, Any] | None:
    """Yield from the latest observation that carries one (R/F where traded; spec 06 'Yield')."""
    with_yield = [r for r in observed if num(r["yield_pct"]) is not None]
    if not with_yield:
        return None
    last = with_yield[-1]
    return price_sourced(last, f"{float(last['yield_pct']):.2f}%")


def gap_rows(gap_out: Row | None) -> list[dict[str, Any]]:
    rows = output_field(gap_out, "holders", "rows")
    if isinstance(rows, dict):
        rows = rows.get("rows")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def mark_gap(gap_out: Row | None, price: dict[str, Any], has_marks: bool) -> Any:
    """Marden price minus the average filed fund mark, in points (model 'mark_gap'). Without the model
    output: 'no fund' when no filing lists the instrument, else n/d."""
    raw = output_field(gap_out, "markGap", "gap", "gapPts")
    if isinstance(raw, str) and raw in GAP_WORDS:
        return raw
    if gap_out is not None and raw is not None:
        got = model_sourced(gap_out, raw)
        if got is not None and (got["value"] is None or num(got["value"]) is not None):
            got["value"] = None if got["value"] is None else round(float(got["value"]), 4)
            return got
    rows = [r for r in gap_rows(gap_out) if num(r.get("gapPts")) is not None]
    if gap_out is not None and rows and price.get("value") is not None:
        avg = sum(float(r["gapPts"]) for r in rows) / len(rows)
        return model_sourced(gap_out, round(avg, 4))
    if gap_out is None and not has_marks:
        return "no fund"
    return None


def instrument_row(conn: Conn, inst: Row, facts: dict[str, Row]) -> tuple[dict[str, Any], dict[str, Any]]:
    """InstrumentRow, plus what instrument_detail reuses (outputs, prices, holders)."""
    iid = inst["id"]
    price_out = latest_output(conn, "price", iid, "instrument")
    gap_out = latest_output(conn, "mark_gap", iid, "instrument")
    series = marden_prices(conn, iid)
    observed = price_rows(conn, iid, OBSERVED_KINDS)
    marks = price_rows(conn, iid, ("fund_mark",))
    lines = holder_lines(conn, iid)
    seniority = facts.get("seniority")
    price = marden_price(price_out, series)
    row = {
        "id": iid,
        "name": display_name(inst),
        "type": type_label(inst, seniority["value"] if seniority else None),
        "sizeB": size_b(facts),
        "matures": maturity_text(facts),
        "mardenPrice": price,
        "yieldPct": traded_yield(observed),
        "change1w": change_1w(price_out, series, price),
        "markGap": mark_gap(gap_out, price, bool(lines or marks)),
        "chainStatus": chain_status(conn, inst),
    }
    ctx = {
        "price_out": price_out,
        "gap_out": gap_out,
        "series": series,
        "observed": observed,
        "marks": marks,
        "lines": lines,
    }
    return row, ctx


INSTRUMENT_FACTS = (
    "amount_usd",
    "amount_basis",
    "commitment_usd",
    "drawn_usd",
    "maturity",
    "maturity_text",
    "seniority",
)


@builder("instruments", many=True)
def build_instruments(conn: Conn):
    """GET /api/instruments: every V1 instrument, by segment (spec 00a order) then size."""
    rows = []
    for inst in instrument_identity(conn, v1_only=True):
        facts = known_facts(conn, "instrument", inst["id"], INSTRUMENT_FACTS)
        row, _ = instrument_row(conn, inst, facts)
        rows.append(row)
    rows.sort(key=lambda r: (segment_rank(r["id"]), -(r["sizeB"]["value"] or 0), r["name"]))
    yield "all", rows
