"""Read model `instrument_detail` (instrument.id): GET /api/instruments/:id, its price-history and
holders cuts, the holders export and watchlist items (InstrumentDetail, spec 07).

The row part is `instruments.instrument_row`, so the page agrees with the list. Then:
- fundMarks: model 'mark_gap' `fundMarks`, else the range of the holders' own filed marks (price_obs
  kind 'fund_mark', tag F, not a cross-fund median) on the latest filing date;
- holders: model 'mark_gap' rows (gap and NAV hit need the Marden price); without the output, the
  count and par of the funds whose latest filing lists the instrument, and no rows;
- priceHistory: the Marden daily price (price_obs kind 'marden');
- recovery: model 'recovery', else the price output's assumed recovery, else the recovery facts (M);
- stress: model 'stress' rows; terms: the instrument's facts; payers: end-user share facts;
- marketCheck: the issuer's (or the SPV's parent's) bond-implied CDS named for the company, the
  default odds it implies, the tenant's CDS (the price model's inputs), observed
  prices with their sources (dealer quote, last trade, exchange quote, the N-PORT median) and the price
  model's spread and band. Prices one decimal, yields one decimal with % (spec 00 Formats).
"""

from __future__ import annotations

import re
from datetime import timedelta
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
from marden.readmodels.views.instrument_text import DASH, facts_line, price_text, terms, yield_text
from marden.readmodels.views.instruments import gap_rows, instrument_row
from marden.readmodels.views.recovery_text import recovery_from_facts, recovery_from_output

HOLDER_FILINGS = ("N-PORT", "10-Q")
_MEDIAN_N = re.compile(r"median of (\d+) funds")


def marks_of_holders(gap_out: Row | None) -> dict[str, Any] | None:
    """The range of the last mark each holder filed (the 'mark_gap' holder rows), dated by the newest
    filing among them: the marks the mark gap averages (spec 07 Rules), so the KPI strip, the chart
    and the holders table show the same funds. None without holder rows."""
    rows = [h for h in (_holder(r) for r in gap_rows(gap_out)) if h is not None]
    if not rows:
        return None
    marks = [h["lastMark"] for h in rows]
    return {
        "low": round(min(marks), 4),
        "high": round(max(marks), 4),
        "filing": " and ".join(sorted({h["filing"] for h in rows})),
        "date": max(h["filingDate"] for h in rows),
    }


def fund_marks(gap_out: Row | None, lines: list[Row]) -> dict[str, Any] | None:
    """Funds' last marks: low, high, which filings and the newest filing date (n/d when no fund files
    a mark).

    With the 'mark_gap' holder rows: the range of every holder's last mark (marks_of_holders); the
    model's own `fundMarks` covers only the newest filing date, which leaves out holders whose
    latest mark is older but still in the gap. Without rows: the model's `fundMarks`, else the
    holders' latest filed lines (holder_lines) that carry a mark, on the newest report date at least
    two funds share (the median rule of the N-PORT loaders), else the newest date."""
    from_rows = marks_of_holders(gap_out)
    if from_rows is not None:
        return from_rows
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
    """Recovery if default, cents per dollar (M); the price model's is its default: 'assumed 55c'."""
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
                got["value"] = recovery_from_output(got["value"], assumed=out is price_out)
                if got["value"]:
                    return got
            continue
        text = recovery_from_output(raw, assumed=out is price_out)
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


YIELD_BEFORE = "Yield 3 months ago"
YIELD_LOOKBACK = timedelta(days=91)


def yield_before(observed: list[Row]) -> dict[str, Any] | None:
    """The traded yield about three months before the latest one (spec 07 KPI strip: "Yield + level
    at a prior date"): the last observation carrying a yield on or before the latest yield's date
    less 91 days, dated by its own observation. None when the history is shorter."""
    with_yield = [r for r in observed if num(r["yield_pct"]) is not None]
    if not with_yield:
        return None
    cutoff = with_yield[-1]["obs_date"] - YIELD_LOOKBACK
    before = [r for r in with_yield if r["obs_date"] <= cutoff]
    if not before:
        return None
    row = before[-1]
    return price_sourced(row, yield_text(row["yield_pct"]))


# The price model's CDS inputs (marden.models.price `_inputs`): the issuer's own bond-implied CDS, the
# obligor's (the parent behind a borrower SPV: CoreWeave for its DDTLs) and the tenant's, each labelled
# '<entity id> bond-implied CDS'.
CDS_INPUTS = (("issuerCdsBp", "cdsBp", "bondImpliedCdsBp", "obligorCdsBp"), ("tenantCdsBp",))
ODDS_INPUTS = ("impliedDefaultOdds", "defaultOdds5y", "pd5y")
_CDS_LABEL = re.compile(r"^(\S+) bond-implied CDS$")
_ODDS_YEARS = re.compile(r"\((\d+(?:\.\d+)?)y\b")


def _inputs(price_out: Row | None) -> dict[str, dict[str, Any]]:
    """The price model's inputs by key: [{key, label, value, unit, ...}] (or a plain {key: value})."""
    raw = output_field(price_out, "inputs")
    if isinstance(raw, dict):
        return {k: {"value": v} for k, v in raw.items()}
    if not isinstance(raw, list):
        return {}
    return {i["key"]: i for i in raw if isinstance(i, dict) and isinstance(i.get("key"), str)}


def cds_entities(price_out: Row | None) -> list[str]:
    """Entity ids named by the price model's CDS inputs ('coreweave bond-implied CDS' -> 'coreweave')."""
    ids = []
    for item in _inputs(price_out).values():
        m = _CDS_LABEL.match(str(item.get("label") or ""))
        if m:
            ids.append(m.group(1))
    return ids


def _cds_row(price_out: Row, item: dict[str, Any], names: dict[str, str], prefix: str) -> tuple[str, dict[str, Any]]:
    m = _CDS_LABEL.match(str(item.get("label") or ""))
    who = names.get(m.group(1), m.group(1)) if m else ""
    label = f"{prefix}{who} CDS (bond-implied)".replace("  ", " ").strip()
    return label, model_sourced(price_out, f"{float(item['value']):,.0f}bp")  # type: ignore[return-value]


def market_check(
    price_out: Row | None, observed: list[Row], marks: list[Row], names: dict[str, str] | None = None
) -> dict[str, dict[str, Any]]:
    """Market check (spec 07 Layout 4): the issuer's CDS ('CoreWeave CDS (bond-implied)'), the default
    odds it implies, the tenant's CDS ('Tenant Oracle CDS (bond-implied)'), then the observed prices, the
    funds' median mark and the price model's spread and band. CDS levels are the price model's
    bond-implied inputs, named for their entity; `names` maps entity ids to display names
    (cds_entities). The document is JSONB, which keeps no key order: the screen orders the rows."""
    names = names or {}
    out: dict[str, dict[str, Any]] = {}
    inputs = _inputs(price_out) if price_out is not None else {}

    def first(keys: tuple[str, ...]) -> dict[str, Any] | None:
        return next((inputs[k] for k in keys if k in inputs and num(inputs[k].get("value")) is not None), None)

    issuer, tenant, odds = first(CDS_INPUTS[0]), first(CDS_INPUTS[1]), first(ODDS_INPUTS)
    if price_out is not None and issuer is not None:
        label, value = _cds_row(price_out, issuer, names, "")
        out[label] = value
    if price_out is not None and odds is not None:
        years = _ODDS_YEARS.search(str(odds.get("label") or ""))
        text = f"{float(odds['value']) * 100:.1f}%" + (f" over {years.group(1)} years" if years else "")
        out["Default odds implied by CDS"] = model_sourced(price_out, text)  # type: ignore[assignment]
    if price_out is not None and tenant is not None:
        label, value = _cds_row(price_out, tenant, names, "Tenant ")
        out.setdefault(label, value)
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
                text = f"{text} (yield {yield_text(row['yield_pct'])})"
            out[label] = price_sourced(row, text)
    prior = yield_before(observed)
    if prior is not None:
        out[YIELD_BEFORE] = prior
    median = _median(marks)
    if median is not None:
        out["Fund marks (median)"] = price_sourced(median, price_text(median["price"]))
    if price_out is not None:
        spread = num(output_field(price_out, "spreadBp"))
        if spread is not None:
            out["Marden spread"] = model_sourced(price_out, f"{spread:,.0f}bp")  # type: ignore[assignment]
        band = output_field(price_out, "band")
        if isinstance(band, dict) and num(band.get("low")) is not None and num(band.get("high")) is not None:
            out["Price band"] = model_sourced(price_out, f"{price_text(band['low'])}{DASH}{price_text(band['high'])}")  # type: ignore[assignment]
    return out


def entity_names(conn: Conn, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = conn.execute("SELECT id, name FROM entity WHERE id = ANY(%s)", (ids,)).fetchall()
    return {r["id"]: r["name"] for r in rows}


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
        "marketCheck": market_check(
            ctx["price_out"], ctx["observed"], ctx["marks"], entity_names(conn, cds_entities(ctx["price_out"]))
        ),
    }
