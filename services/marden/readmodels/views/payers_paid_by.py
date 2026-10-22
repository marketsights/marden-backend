"""Payer page rail (spec 08): `paidBy`, the next link up from a payer, and the rating statement that the
payer's KPI strip and this rail share.

The parties above a payer are the end users of the sites it is tenant of and the payers of the leases and
compute contracts it is owed (on the obligation map with a source). Each carries:

- status: its worst link in the status model's latest outputs (a party with no judged link is not listed),
  `ruleCode` and `rule`: the fixed rule that link tripped ('E1', "End user's plan needs new funding"), and
  `note`: the statement that tripped it (else its first funding statement, else n/d).
- endUserOn: the payer's own sites where it is the end user; owes: the leases / compute contracts it owes
  the payer.
- facts: its dated statements, newest first: funding and commitments (`funding_fact`, one row per source)
  and its latest rating statement. On the same day the statement that tripped the rule comes first, then
  the party's own (its rating, labels naming it), then the ones naming the payer (its exposure), then the
  rest; filed before reported before Marden.
- slug: its own payer page (null when it is not a payer).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from datetime import date

from marden.core.db import Conn, Row
from marden.readmodels.views.payers_common import (
    LEASE_TYPES,
    LINK_LABELS,
    LINK_TO_STATUS,
    Doc,
    chain_roles,
    edge_inputs,
    edge_known,
    entity_row,
    link_status,
    nd,
    obligation_rows,
    payer_ids,
    tripped_links,
    with_value,
)
from marden.readmodels.views.sites_common import TAG_RANK, known_fact_rows, known_facts

_RULE_CODE = re.compile(r"^[A-Z]\d+\s+")


def rule_label(link: Doc) -> str:
    """The rule a tripped link names, without its code: 'T2 Force majeure filed: …' -> 'Force majeure filed'."""
    return _RULE_CODE.sub("", str(link.get("reason") or "").split(":", 1)[0]).strip() or "Rule tripped"


def rating_statement(conn: Conn, entity_id: str) -> Doc:
    """The latest rating statement about the party: `tenant_rating` on the sites it is tenant of (newest,
    filed before reported), else the entity's `rating_summary`; n/d when neither exists."""
    rows = []
    for party in chain_roles(conn, entity_id):
        if party["role"] != "tenant":
            continue
        row = known_facts(conn, "site", party["site_slug"], ["tenant_rating"]).get("tenant_rating")
        if row is not None and isinstance(row["value"], str) and row["value"].strip():
            rows.append(row)
    if rows:
        best = max(rows, key=lambda r: (r["valid_from"], -TAG_RANK.get(r["tag"].strip(), 2)))
        return with_value(best, best["value"])
    row = known_facts(conn, "entity", entity_id, ["rating_summary"]).get("rating_summary")
    if row is not None and isinstance(row["value"], dict) and isinstance(row["value"].get("rating"), str):
        return with_value(row, row["value"]["rating"])
    return nd("R")


def funding_text(row: Row) -> tuple[str, str, str] | None:
    """A `funding_fact` row as (label, 'value (note)', the statement as the status model reads it)."""
    v = row["value"] if isinstance(row["value"], dict) else {}
    if not (v.get("label") and v.get("value")):
        return None
    note = f" ({v['note']})" if v.get("note") else ""
    said = " ".join(str(v[k]) for k in ("label", "value", "note") if v.get(k))
    return str(v["label"]), f"{v['value']}{note}", said


def funding_rows(conn: Conn, entity_id: str) -> list[Row]:
    return known_fact_rows(conn, "entity", entity_id, ["funding_fact"])["funding_fact"]


def funding_note(conn: Conn, entity_id: str) -> Doc | None:
    for row in funding_rows(conn, entity_id):
        if (got := funding_text(row)) is not None:
            return with_value(row, f"{got[0]}: {got[1]}")
    return None


def _trips(said: str, fact: Doc, trip: Doc | None) -> bool:
    """True when `trip` (a ChainLink fact, cut at 220 characters) is this statement from the same source."""
    if trip is None or not isinstance(trip.get("value"), str):
        return False
    text = trip["value"].rstrip("…").rstrip()
    return bool(text) and said.startswith(text) and (trip.get("sourceUrl") or None) == (fact.get("sourceUrl") or None)


def _names(text: str, name: str) -> bool:
    return bool(name) and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.I) is not None


def party_facts(conn: Conn, entity_id: str, name: str, payer_name: str, trip: Doc | None) -> list[Doc]:
    """The party's dated funding, commitment and rating statements (see the module docstring for the order)."""
    items: list[tuple[Doc, str]] = []
    for row in funding_rows(conn, entity_id):
        if (got := funding_text(row)) is not None:
            items.append(({"label": got[0], "fact": with_value(row, got[1])}, got[2]))
    rating = rating_statement(conn, entity_id)
    if isinstance(rating.get("value"), str) and rating["value"].strip():
        own = re.sub(rf"^{re.escape(name)}\s*:\s*", "", rating["value"]) or rating["value"]  # "OpenAI: unrated"
        items.append(({"label": "Rating", "fact": {**rating, "value": own}}, rating["value"]))
    out, seen = [], set()
    for item, said in items:
        if (item["label"], item["fact"]["value"]) in seen:
            continue
        seen.add((item["label"], item["fact"]["value"]))
        out.append({**item, "tripped": _trips(said, item["fact"], trip)})
    if trip is not None and trip.get("value") is not None and not any(i["tripped"] for i in out):
        out.append({"label": "Statement", "fact": trip, "tripped": True})

    def order(item: Doc) -> tuple:
        fact, label = item["fact"], item["label"]
        newest = -date.fromisoformat(str(fact["asOf"])[:10]).toordinal()
        own = label == "Rating" or (_names(label, name) and not _names(label, payer_name))
        about = 0 if own else 1 if _names(f"{label} {fact['value']}", payer_name) else 2
        return newest, not item["tripped"], about, TAG_RANK.get(fact["tag"], 2), label

    return sorted(out, key=order)


def tripping_link(conn: Conn, entity_id: str, roles: Sequence[str] | None, status: str, fact: Doc | None) -> Doc | None:
    """The amber or red link behind the party's status: the one whose statement `link_status` returned."""
    for _, link, tripped in tripped_links(conn, entity_id):
        if roles is not None and link.get("role") not in roles:
            continue
        if LINK_TO_STATUS.get(link.get("status")) == status and tripped == fact:
            return link
    return None


def paid_by(conn: Conn, entity: Row, site_slugs: Collection[str]) -> list[Doc]:
    entity_id = entity["id"]
    end_user_on: dict[str, list[str]] = {}
    for party in chain_roles(conn, entity_id):
        if party["role"] != "tenant":
            continue
        row = conn.execute(
            "SELECT entity_id FROM chain_party WHERE site_slug = %s AND role = 'end_user' AND entity_id IS NOT NULL",
            (party["site_slug"],),
        ).fetchone()
        if row and row["entity_id"] != entity_id:
            end_user_on.setdefault(row["entity_id"], []).append(party["site_slug"])
    owes: dict[str, set[str]] = {}
    for ob in obligation_rows(conn, entity_id):
        if ob["to_entity_id"] == entity_id and ob["from_entity_id"] != entity_id and ob["type"] in LEASE_TYPES:
            facts, rows = edge_inputs(conn, ob)
            if edge_known(facts, rows):
                owes.setdefault(ob["from_entity_id"], set()).add(LINK_LABELS[ob["type"]])
    payers = set(payer_ids(conn))
    out = []
    for cid in sorted(end_user_on.keys() | owes.keys()):
        roles = ("end_user",) if cid in end_user_on else None
        party = entity_row(conn, cid)
        status, fact = link_status(conn, cid, roles)
        if party is None or status is None:
            continue
        link = tripping_link(conn, cid, roles, status, fact)
        out.append({
            "name": party["name"],
            "slug": cid if cid in payers else None,
            "status": status,
            "note": fact or funding_note(conn, cid) or nd("R"),
            "ruleCode": (link.get("ruleCode") or None) if link else None,
            "rule": rule_label(link) if link else None,
            "endUserOn": sorted(s for s in set(end_user_on.get(cid, [])) if s in site_slugs),
            "owes": sorted(owes.get(cid, set())),
            "facts": party_facts(conn, cid, party["name"], entity["name"], fact if link else None),
        })
    return out
