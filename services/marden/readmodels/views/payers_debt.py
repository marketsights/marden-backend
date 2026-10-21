"""The debt behind an obligation edge, for the payer, capacity and lease views: each instrument's size, the
first rating-tied term its documents quote (spec 24 "Rating triggers inside the debt") and its holders,
as known on the build date. The holder count matches the instrument's own page (the mark-gap output when
that model has run, else the funds whose latest N-PORT / BDC filing lists it).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date

from marden.core.db import Conn, Row
from marden.readmodels.context import known_fact_rows, known_facts
from marden.readmodels.views.instrument_inputs import holder_lines, holder_totals, instrument_identity, num
from marden.readmodels.views.instrument_text import display_name
from marden.readmodels.views.payers_common import billions_of
from marden.readmodels.views.sites_common import NOT_COUNTED, Doc, latest_output, nd, output_field, with_value

_RATING_TERM = re.compile(r"rating|investment[- ]grade|\bIG\b|\bBBB|\bBaa", re.I)


def holders_count(conn: Conn, instrument_id: str) -> int:
    """Funds holding the instrument, as on its detail page: the mark-gap output's count when the model has
    run, else the funds whose latest N-PORT / BDC filing lists it."""
    gap = output_field(latest_output(conn, "mark_gap", "instrument", instrument_id), "count")
    if num(gap) is not None:
        return int(num(gap))
    return holder_totals(holder_lines(conn, instrument_id))[0]


def holder_funds(conn: Conn, instrument_ids: Sequence[str]) -> tuple[set[str], date | None]:
    """Distinct funds holding any of the instruments (latest filings) and the newest period they report."""
    funds: set[str] = set()
    period: date | None = None
    for iid in instrument_ids:
        for line in holder_lines(conn, iid):
            funds.add(line["fund_entity_id"])
            period = max(period, line["period_end"]) if period else line["period_end"]
    return funds, period


def rating_term(conn: Conn, instrument_id: str) -> Row | None:
    """The instrument's first quoted term tied to a rating (agreement_term, then covenant_test, tag F)."""
    rows = known_fact_rows(conn, "instrument", instrument_id, ["agreement_term", "covenant_test"])
    for row in rows["agreement_term"]:
        v = row["value"] if isinstance(row["value"], dict) else {}
        if _RATING_TERM.search(str(v.get("type") or "")) or _RATING_TERM.search(str(v.get("threshold") or "")):
            return row
    for row in rows["covenant_test"]:
        v = row["value"] if isinstance(row["value"], dict) else {}
        if _RATING_TERM.search(f"{v.get('metric') or ''} {v.get('threshold') or ''}"):
            return row
    return None


def trigger_texts(row: Row | None) -> tuple[Doc, Doc]:
    """(ratingTrigger, onTrip) as Sourced<string> from a rating term (n/d when the debt states none)."""
    if row is None or not isinstance(row["value"], dict):
        return nd("F"), nd("F")
    v = row["value"]
    threshold = " ".join(str(x) for x in (v.get("threshold") or v.get("metric"),) if x)
    section = f" ({v['section']})" if v.get("section") else ""
    trigger = with_value(row, f"{threshold}{section}") if threshold else nd("F")
    trip = with_value(row, str(v["consequence"])) if v.get("consequence") else nd("F")
    return trigger, trip


def instrument_info(conn: Conn, ids: Sequence[str]) -> dict[str, Doc]:
    """id -> {name, amountB (Sourced $B), counted, ratingRow, holdersCount} for existing instruments."""
    out: dict[str, Doc] = {}
    for inst in instrument_identity(conn, list(ids)):
        facts = known_facts(conn, "instrument", inst["id"], ["amount_usd", "status", "amount_basis"])
        status = (facts.get("status") or {}).get("value")
        basis = (facts.get("amount_basis") or {}).get("value")
        out[inst["id"]] = {
            "name": display_name(inst),
            "kind": inst["kind"],
            "amountB": billions_of(facts.get("amount_usd")),
            "counted": status not in NOT_COUNTED and basis != "holder_par",
            "ratingRow": rating_term(conn, inst["id"]),
            "holdersCount": holders_count(conn, inst["id"]),
        }
    return out
