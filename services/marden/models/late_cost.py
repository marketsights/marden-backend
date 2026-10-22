"""Cost of each month late (spec 04 Schedule cushion card): what one more month before rent starts costs the
financing, in parts, each from stated terms (Marden model value, tag M, 'late cost v0.1').

    interest  = for each instrument that funds the site: amount x all-in rate / 12
                amount: drawn (outstanding) when stated, else the instrument's amount (a bond is funded at issue);
                rate: a fixed coupon as stated; a floating one as SOFR (the New York Fed's rate stored in
                Marden, series `sofr`, the newest fixing public by the as-of day) + the stated spread
    rent      = the lease's monthly rent (annual rent / 12, else contract value / term in months: the cushion
                model's rent input) for each month rent start slips past its due date
    total     = interest + rent

An instrument with no stated rate or amount is left out and named in `excluded` (the total covers the rest).
Who bears each part is shown only as a source states it: the site's `structure_term` facts on completion
support, reserves and delay terms (borrower SPV, sponsor completion guarantee, tenant delay credits or
abatement), quoted with their tag and source; otherwise `bearer` is null and the card says it is not public.
Every input is a stored fact as public on the as-of day (marden.models.pit), so a past --as-of is a backtest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.core.db import Conn, Row
from marden.models.pit import current_of, fact_rows

VERSION = "late cost v0.1"
SOURCE_NAME = "Marden cushion model"
INSTRUMENT_FIELDS = ("amount_usd", "drawn_usd", "commitment_usd", "coupon_type", "coupon_rate_pct",
                     "coupon_spread_bp", "coupon_benchmark", "coupon", "status")
NOT_OUTSTANDING = frozenset({"repaid", "proposed", "cancelled", "canceled", "redeemed"})
BEARER_KEYS = ("Completion support", "Reserves", "Termination rights", "Delay", "Rent abatement", "Guarantee")
_DELAY = re.compile(r"delay|late delivery|abatement|rent credit|liquidated|penalt", re.I)


@dataclass
class Line:
    instrument: str
    name: str
    amount: float
    amount_basis: str
    rate_pct: float
    rate_basis: str
    monthly: float
    rows: list[Row] = field(default_factory=list)


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def sofr_on(conn: Conn, as_of: date) -> Row | None:
    """The newest SOFR fixing public by as_of (series `sofr`, field `value`, percent)."""
    rows = [r for r in fact_rows(conn, as_of, "series", ["sofr"], ["value"]) if _num(r["value"]) is not None]
    return max(rows, key=lambda r: r["valid_from"]) if rows else None


def interest_line(inst: Row, facts: dict[str, list[Row]], sofr: Row | None) -> tuple[Line | None, str | None]:
    """(the instrument's monthly interest, None) or (None, why it is left out)."""
    cur = {f: current_of(rows) for f, rows in facts.items()}
    name = inst["name"]
    status = str((cur.get("status") or {}).get("value") or "").lower()
    if status in NOT_OUTSTANDING:
        return None, f"{name}: {status}"
    amount_row = cur.get("drawn_usd") or cur.get("amount_usd")
    amount = _num(amount_row["value"]) if amount_row else None
    if not amount:
        return None, f"{name}: amount not public"
    basis = "drawn" if amount_row is cur.get("drawn_usd") else "amount"
    ctype = str((cur.get("coupon_type") or {}).get("value") or "").lower()
    rate_row, spread_row = cur.get("coupon_rate_pct"), cur.get("coupon_spread_bp")
    bench = str((cur.get("coupon_benchmark") or {}).get("value") or "")
    coupon_text = str((cur.get("coupon") or {}).get("value") or "")
    rows = [amount_row]
    if ctype != "floating" and rate_row is not None and _num(rate_row["value"]) is not None:
        rate, how = float(rate_row["value"]), f"fixed {float(rate_row['value']):g}%"
        rows.append(rate_row)
    elif spread_row is not None and _num(spread_row["value"]) is not None:
        sofr_stated = "sofr" in bench.lower() or (not bench and ("sofr" in coupon_text.lower() or not coupon_text))
        if not sofr_stated:
            return None, f"{name}: floating over {bench or 'an unstated benchmark'} (only SOFR is stored)"
        if sofr is None:
            return None, f"{name}: no SOFR fixing stored by the as-of day"
        spread = float(spread_row["value"])
        rate = float(sofr["value"]) + spread / 100
        how = f"SOFR {float(sofr['value']):.2f}% ({sofr['valid_from']:%b %-d, %Y}) + {spread:g} bp"
        rows += [spread_row, sofr]
    else:
        return None, f"{name}: rate not public"
    commit = _num((cur.get("commitment_usd") or {}).get("value"))
    if basis == "drawn" and commit and commit > amount:
        basis = f"drawn of ${commit / 1e9:.2f}B committed"
    return Line(inst["id"], name, amount, basis, round(rate, 4), how, amount * rate / 100 / 12, rows), None


def bearer_lines(site_facts: dict[str, list[Row]]) -> list[dict[str, Any]]:
    """Who bears the cost, as the site's structure terms state it (completion support, reserves, delay terms)."""
    out = []
    for r in site_facts.get("structure_term", []):
        v = r["value"] if isinstance(r["value"], dict) else {}
        key, text = str(v.get("key") or ""), str(v.get("short") or v.get("text") or "")
        if not text:
            continue
        if key in ("Completion support", "Reserves") or (_DELAY.search(text) and key in BEARER_KEYS):
            out.append({"key": key, "text": text, "row": r})
    order = {k: i for i, k in enumerate(BEARER_KEYS)}
    out.sort(key=lambda b: (order.get(b["key"], 99), b["text"]))
    return out


def debt_part(conn: Conn, slug: str, as_of: date) -> dict[str, Any]:
    """The site's interest lines, the instruments left out (and why), who bears what, and the SOFR fixing used."""
    insts = conn.execute("SELECT id, name, kind FROM instrument WHERE site_slug = %s ORDER BY id", (slug,)).fetchall()
    by_inst: dict[str, dict[str, list[Row]]] = {}
    for r in fact_rows(conn, as_of, "instrument", [i["id"] for i in insts], INSTRUMENT_FIELDS) if insts else []:
        by_inst.setdefault(r["subject_id"], {}).setdefault(r["field"], []).append(r)
    sofr = sofr_on(conn, as_of)
    lines, excluded = [], []
    for inst in insts:
        line, why = interest_line(inst, by_inst.get(inst["id"], {}), sofr)
        if line is not None:
            lines.append(line)
        elif why:
            excluded.append(why)
    site_facts: dict[str, list[Row]] = {}
    for r in fact_rows(conn, as_of, "site", [slug], ["structure_term"]):
        site_facts.setdefault(r["field"], []).append(r)
    return {"lines": lines, "excluded": excluded, "bearers": bearer_lines(site_facts), "sofr": sofr,
            "instruments": len(insts)}


def assemble(debt: dict[str, Any], *, rent_monthly: float | None, rent_done: bool) -> dict[str, Any]:
    """{total, interest, rent, why}: the parts that apply (rent only while rent start is still ahead)."""
    if rent_done:
        return {"total": None, "interest": None, "rent": None,
                "why": "Rent has started, so there is no rent start left to be late"}
    lines = debt["lines"]
    interest = sum(ln.monthly for ln in lines) if lines else None
    rent = rent_monthly if rent_monthly and not rent_done else None
    parts = [v for v in (interest, rent) if v]
    total = sum(parts) if parts else None
    why = None
    if total is None:
        why = ("No instrument funding the site states both an amount and a rate, and no rent is disclosed"
               if debt["instruments"] else "No debt is linked to the site and no rent is disclosed")
        if debt["excluded"]:
            why += f" ({'; '.join(debt['excluded'][:3])})"
    return {"total": total, "interest": interest, "rent": rent, "why": why}
