"""A payer's reported scale (PayerCapacity.scale, `listed`): for a payer no agency rates, the figures a credit
reader sizes it by instead of a rating, each sourced (spec 24 Payers index "Scale" column, Payer page card).

Entity facts (seed/payers_2026_10: each checked against its source document before it is written), values
{"usd": number, "basis": "<what the number is, in a few words>"}:
- `reported_revenue`: latest annualized revenue or revenue run-rate (R), or a filed period's revenue (F);
- `funding_raised`: total raised to date, or the latest round when no total is reported;
- `valuation`: the last round's (post-money) valuation;
- `cash_burn`: cash burn where reported (or projected, said so in its basis).
A listed payer with no `reported_revenue` shows its filed quarterly revenue (`revenue_q_usd`, F).
- commitments: the contract sizes (`total_usd`) of the leases and compute contracts it owes on the obligation
  map, summed (the weakest tag of the parts); the basis says how many of its contracts are sized.

`listed`: the entity has a ticker, or a `listing` fact ({"ticker", "exchange"}) from its filings.
"""

from __future__ import annotations

from datetime import date

from marden.core.db import Conn, Row
from marden.readmodels.views.payers_common import (
    LEASE_TYPES,
    Doc,
    billions_of,
    edge_inputs,
    edge_known,
    obligation_rows,
    summed,
    with_value,
)
from marden.readmodels.views.sites_common import billions, known_facts

SCALE_FIELDS = {
    "revenue": "reported_revenue",
    "raised": "funding_raised",
    "valuation": "valuation",
    "burn": "cash_burn",
}
ORDER = ("revenue", "raised", "valuation", "commitments", "burn")


def listed(conn: Conn, entity: Row) -> bool:
    if entity.get("ticker"):
        return True
    row = known_facts(conn, "entity", entity["id"], ["listing"]).get("listing")
    return bool(row and isinstance(row["value"], dict) and row["value"].get("ticker"))


def _quarter(day: date) -> str:
    return f"Q{(day.month - 1) // 3 + 1} {day.year} revenue"


def _figure(key: str, row: Row) -> Doc | None:
    value = row["value"] if isinstance(row["value"], dict) else {}
    amount = billions(value.get("usd"))
    basis = str(value.get("basis") or "").strip()
    if amount is None or not basis:
        return None
    return {"key": key, "basis": basis, "amountB": with_value(row, amount)}


def commitments(conn: Conn, entity_id: str) -> Doc | None:
    """The sized leases and compute contracts the payer owes on the obligation map, summed."""
    sizes, total = [], 0
    for ob in obligation_rows(conn, entity_id):
        if ob["from_entity_id"] != entity_id or ob["type"] not in LEASE_TYPES:
            continue
        facts, rows = edge_inputs(conn, ob)
        if not edge_known(facts, rows):
            continue
        total += 1
        if (size := billions_of(facts.get("total_usd")))["value"] is not None:
            sizes.append(size)
    if not sizes:
        return None
    contracts = "contract" if total == 1 else "contracts"
    basis = (
        f"{len(sizes)} of {total} {contracts} sized, Marden obligation map"
        if len(sizes) < total
        else (f"{total} {contracts}, Marden obligation map")
    )
    return {"key": "commitments", "basis": basis, "amountB": summed(sizes)}


def scale(conn: Conn, entity: Row, rated: bool) -> list[Doc]:
    """ScaleFigure[] for a payer no agency rates (empty for a rated payer)."""
    if rated:
        return []
    facts = known_facts(conn, "entity", entity["id"], [*SCALE_FIELDS.values(), "revenue_q_usd"])
    found: dict[str, Doc] = {}
    for key, field in SCALE_FIELDS.items():
        if (row := facts.get(field)) is not None and (fig := _figure(key, row)) is not None:
            found[key] = fig
    quarter = facts.get("revenue_q_usd")
    if "revenue" not in found and quarter is not None and billions_of(quarter, "F")["value"] is not None:
        found["revenue"] = {
            "key": "revenue",
            "basis": _quarter(quarter["valid_from"]),
            "amountB": billions_of(quarter, "F"),
        }
    if (owed := commitments(conn, entity["id"])) is not None:
        found["commitments"] = owed
    return [found[k] for k in ORDER if k in found]
