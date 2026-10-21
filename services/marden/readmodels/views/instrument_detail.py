"""Read model `instrument_detail` (instrument.id): GET /api/instruments/:id, its price-history and
holders cuts, the holders export and watchlist items (InstrumentDetail, spec 07).

The row part is `instruments.instrument_row`, so the page agrees with the list. Then:
- fundMarks: model 'mark_gap' `fundMarks`, else the range of the holders' own filed marks (price_obs
  kind 'fund_mark', tag F, not a cross-fund median) on the latest filing date;
- holders: model 'mark_gap' rows (gap and NAV hit need the Marden price); without the output, the
  count and par of the funds whose latest filing lists the instrument, and no rows;
- priceHistory: the Marden daily price (price_obs kind 'marden');
- recovery: model 'recovery', else the price output's recovery, else the recovery facts (M);
- stress: model 'stress' rows; terms: the instrument's facts; payers: end-user share facts;
- marketCheck: observed prices with their sources (dealer quote, last trade, exchange quote, the
  N-PORT median) and the price model's spread, band and CDS inputs.
"""

from __future__ import annotations

import re
from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.context import known_facts
from marden.readmodels.registry import builder
from marden.readmodels.views.instrument_inputs import (
    all_instrument_ids,
    fact_sourced,
    holder_totals,
    instrument_identity,
    is_median,
    iso,
    latest_output,
    model_sourced,
    nd,
    num,
    output_field,
    price_sourced,
)
from marden.readmodels.views.instrument_text import (
    DASH,
    facts_line,
    price_text,
    recovery_from_facts,
    recovery_from_output,
    terms,
)
from marden.readmodels.views.instruments import gap_rows, instrument_row

HOLDER_FILINGS = ("N-PORT", "10-Q")
_MEDIAN_N = re.compile(r"median of (\d+) funds")


def fund_marks(gap_out: Row | None, lines: list[Row]) -> dict[str, Any] | None:
    """Funds' last marks: low, high, which filings and their date (n/d when no fund files a mark).

    Without the 'mark_gap' output: the holders' latest filed lines (holder_lines) that carry a mark,
    on the newest report date at least two funds share (the median rule of the N-PORT loaders), else
    the newest date."""
    given = output_field(gap_out, "fundMarks")
    if isinstance(given, dict) and num(given.get("low")) is not None and num(given.get("high")) is not None:
        return {
            "low": float(given["low"]),
            "high": float(given["high"]),
            "filing": str(given.get("filing") or ""),
            "date": str(given.get("date") or ""),
        }
    marked = [r for r in lines if num(r["mark_price"]) is not None]
    if not marked:
        return None
    funds_on: dict[Any, set[str]] = {}
    for r in marked:
        funds_on.setdefault(r["period_end"], set()).add(r["fund_entity_id"])
    shared = [d for d, funds in funds_on.items() if len(funds) >= 2]
    when = max(shared) if shared else max(funds_on)
    on_day = [r for r in marked if r["period_end"] == when]
    prices = [float(r["mark_price"]) for r in on_day]
    filings = sorted({r["filing_type"] for r in on_day})
    return {
        "low": round(min(prices), 4),
        "high": round(max(prices), 4),
        "filing": " and ".join(filings),
        "date": iso(when),
    }


def _holder(raw: dict[str, Any]) -> dict[str, Any] | None:
    """One Holder row from the mark_gap output (rows missing a required value are dropped)."""
    numbers = {k: num(raw.get(k)) for k in ("parMm", "lastMark", "gapPts", "navHitMm", "shareOfNav")}
    if any(v is None for v in numbers.values()) or raw.get("filing") not in HOLDER_FILINGS:
        return None
    if not all(isinstance(raw.get(k), str) and raw[k] for k in ("name", "filingUrl", "filingDate")):
        return None
    return {
        "name": raw["name"],
        "filing": raw["filing"],
        "filingUrl": raw["filingUrl"],
        "filingDate": raw["filingDate"],
        **numbers,
    }


def holders(gap_out: Row | None, lines: list[Row]) -> dict[str, Any]:
    rows = [h for h in (_holder(r) for r in gap_rows(gap_out)) if h is not None]
    rows.sort(key=lambda h: (-abs(h["navHitMm"]), h["name"]))
    count, par = holder_totals(lines)
    if gap_out is not None:
        given_count, given_par = num(output_field(gap_out, "count")), num(output_field(gap_out, "parMm"))
        count = int(given_count) if given_count is not None else (len(rows) or count)
        par = given_par if given_par is not None else (round(sum(h["parMm"] for h in rows), 3) if rows else par)
    return {"count": count, "parMm": par, "rows": rows}


def next_marks_public(gap_out: Row | None) -> str | None:
    value = output_field(gap_out, "nextMarksPublic")
    if isinstance(value, dict):
        value = value.get("value")
    return value if isinstance(value, str) and value else None


def recovery(conn: Conn, instrument_id: str, price_out: Row | None, facts: dict[str, Row]) -> dict[str, Any]:
    """Recovery if default, cents per dollar (M)."""
    rec_out = latest_output(conn, "recovery", instrument_id, "instrument")
    for out in (rec_out, price_out):
        if out is None or not isinstance(out["output"], dict):
            continue
        body = out["output"]
        raw = (
            body
            if out is rec_out and any(k in body for k in ("base", "low", "high"))
            else output_field(out, "recovery", "recoveryCents", "value", "cents")
        )
        if isinstance(raw, dict) and "tag" in raw and "value" in raw:
            got = model_sourced(out, raw)
            if got is not None and got["value"] is not None:
                got["value"] = recovery_from_output(got["value"])
                if got["value"]:
                    return got
            continue
        text = recovery_from_output(raw)
        if text:
            return model_sourced(out, text)  # type: ignore[return-value]
    return recovery_from_facts(facts) or nd("M")


def stress_rows(conn: Conn, instrument_id: str) -> list[dict[str, Any]]:
    """Stress results (model 'stress'): scenario, price after, change, holder NAV hit."""
    out = latest_output(conn, "stress", instrument_id, "instrument")
    if out is None:
        return []
    body = out["output"]
    raw = body if isinstance(body, list) else output_field(out, "rows", "scenarios", "results", "stress")
    rows: list[dict[str, Any]] = []
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict) or not isinstance(r.get("scenario"), str):
            continue
        values = {k: num(r.get(k)) for k in ("priceAfter", "change", "holderNavHitMm")}
        if all(v is not None for v in values.values()):
            rows.append({"scenario": r["scenario"], **values})
    return rows


def payers(facts: dict[str, Row]) -> list[dict[str, Any]]:
    """Who pays the coupon: the end user's share of what repays the instrument (fact end_user_share)."""
    row = facts.get("end_user_share")
    if row is None or not isinstance(row["value"], dict):
        return []
    name, share = row["value"].get("end_user"), num(row["value"].get("share"))
    if not isinstance(name, str) or share is None:
        return []
    return [{"name": name, "share": fact_sourced(row, share)}]


def _latest(rows: list[Row], kind: str) -> Row | None:
    hits = [r for r in rows if r["kind"] == kind and num(r["price"]) is not None]
    return hits[-1] if hits else None


def _median(marks: list[Row]) -> Row | None:
    medians = [r for r in marks if is_median(r) and r["tag"].strip() == "F" and num(r["price"]) is not None]
    if not medians:
        return None
    last = max(r["obs_date"] for r in medians)

    def funds(r: Row) -> int:
        m = _MEDIAN_N.search(r["source_name"])
        return int(m.group(1)) if m else 0

    return max((r for r in medians if r["obs_date"] == last), key=funds)


def market_check(price_out: Row | None, observed: list[Row], marks: list[Row]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for label, kind in (
        ("Dealer quote", "dealer"),
        ("Last trade", "trade"),
        ("Exchange quote", "exchange"),
        ("Quote", "quote"),
    ):
        row = _latest(observed, kind)
        if row is not None:
            text = price_text(row["price"])
            if num(row["yield_pct"]) is not None:
                text = f"{text} (yield {float(row['yield_pct']):.2f}%)"
            out[label] = price_sourced(row, text)
    median = _median(marks)
    if median is not None:
        out["Fund marks (median)"] = price_sourced(median, price_text(median["price"]))
    if price_out is not None:
        spread = num(output_field(price_out, "spreadBp"))
        if spread is not None:
            out["Marden spread"] = model_sourced(price_out, f"{spread:.0f}bp")  # type: ignore[assignment]
        band = output_field(price_out, "band")
        if isinstance(band, dict) and num(band.get("low")) is not None and num(band.get("high")) is not None:
            out["Price band"] = model_sourced(price_out, f"{float(band['low']):.2f}{DASH}{float(band['high']):.2f}")  # type: ignore[assignment]
        inputs = output_field(price_out, "inputs")
        if isinstance(inputs, list):  # the price model's inputs list: [{key, label, value, unit, ...}]
            inputs = {i["key"]: i.get("value") for i in inputs if isinstance(i, dict) and isinstance(i.get("key"), str)}
        if isinstance(inputs, dict):
            for label, keys, fmt in (
                ("Issuer CDS (bond-implied)", ("issuerCdsBp", "cdsBp", "bondImpliedCdsBp"), "{:.0f}bp"),
                ("Tenant CDS (bond-implied)", ("tenantCdsBp",), "{:.0f}bp"),
                ("Implied default odds", ("impliedDefaultOdds", "defaultOdds5y", "pd5y"), "pct"),
            ):
                value = next((num(inputs[k]) for k in keys if num(inputs.get(k)) is not None), None)
                if value is not None:
                    text = f"{value * 100:.1f}%" if fmt == "pct" else fmt.format(value)
                    out[label] = model_sourced(price_out, text)  # type: ignore[assignment]
    return out


def price_event(key: str, doc: dict[str, Any]) -> dict[str, Any] | None:
    """StreamEvent when a priced instrument's page changes: open screens refetch that instrument.
    An instrument the price model has not priced sends none (a StreamEvent price needs its method)."""
    price, change = doc["mardenPrice"], doc["change1w"]
    if any(s["tag"] == "M" and not s.get("methodVersion") for s in (price, change)):
        return None
    return {"type": "price", "instrumentId": key, "price": price, "change1w": change}


@builder("instrument_detail", keys=all_instrument_ids, event=price_event)
def build_instrument_detail(conn: Conn, key: str) -> dict[str, Any] | None:
    found = instrument_identity(conn, [key])
    if not found:
        return None
    inst = found[0]
    facts = known_facts(conn, "instrument", key)
    row, ctx = instrument_row(conn, inst, facts)
    return {
        **row,
        "facts": facts_line(facts),
        "fundMarks": fund_marks(ctx["gap_out"], ctx["lines"]),
        "nextMarksPublic": next_marks_public(ctx["gap_out"]),
        "recovery": recovery(conn, key, ctx["price_out"], facts),
        "priceHistory": [{"date": iso(r["obs_date"]), "price": round(float(r["price"]), 4)} for r in ctx["series"]],
        "holders": holders(ctx["gap_out"], ctx["lines"]),
        "stress": stress_rows(conn, key),
        "terms": terms(inst, facts),
        "payers": payers(facts),
        "marketCheck": market_check(ctx["price_out"], ctx["observed"], ctx["marks"]),
    }
