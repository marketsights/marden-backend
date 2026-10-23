"""Which debt a payer's "Debt exposed" counts (spec 24 Payers index, PayerCapacity.exposed): only the debt
whose payment chain runs through the payer's own contract.

Every obligation edge on a case carries the case's own debt: the sites loader writes `linked_instruments` as
all of the case's instruments whatever the edge's role (seed/sites_obligations.py). Read as is, a customer
that buys compute from a lessee picks up the lessee's site debt (OpenAI buying from Oracle got the Abilene,
Jupiter, Frontier and The Barn construction debt that Oracle's leases service; Anthropic buying Colossus
capacity got the Valor GPU loans that SpaceX's equipment leases service). So, per instrument on an edge the
payer owes:

- Lease: the lessee's rent services the project's debt. Counted, link "Lease".
- Compute purchase: counted, link "Compute contract" (the debt is secured on that contract, as CoreWeave's
  DDTL 3.0 is on OpenAI's), unless another party owes a Lease the same debt sits behind. Then the debt is
  that lessee's: it is counted on the lessee's row and listed here under `exposedElsewhere`.
- Guarantee or backstop: counted as a backstop, link "Guarantee": contingent, paid only if the obligor
  defaults (Google behind Fluidstack's leases with TeraWulf, Cipher, Hut 8 and Flash Compute).

An instrument takes the first link the payer holds on it: lease, then compute contract, then guarantee.
"""

from __future__ import annotations

from collections import defaultdict

from marden.core.db import Conn
from marden.readmodels.views.payers_common import (
    LINK_LABELS,
    OWED_TYPES,
    Doc,
    edge_inputs,
    edge_instruments,
    edge_known,
    entity_rows,
    obligation_rows,
    summed,
)

Lessees = dict[str, set[str]]


def lessees_by_instrument(conn: Conn) -> Lessees:
    """instrument -> the entities owing a Lease (known on the build date) that the instrument sits behind."""
    out: Lessees = defaultdict(set)
    for ob in conn.execute("SELECT * FROM obligation WHERE type = 'Lease' ORDER BY id").fetchall():
        facts, rows = edge_inputs(conn, ob)
        if edge_known(facts, rows):
            for iid in edge_instruments(ob, facts):
                out[iid].add(ob["from_entity_id"])
    return out


def _family(conn: Conn, entity_id: str) -> set[str]:
    """The payer, its parent and its subsidiaries: a lease one of them owes is the payer's own."""
    rows = conn.execute(
        """SELECT id FROM entity WHERE id = %(e)s OR parent_id = %(e)s
           UNION SELECT parent_id FROM entity WHERE id = %(e)s AND parent_id IS NOT NULL""",
        {"e": entity_id},
    ).fetchall()
    return {r["id"] for r in rows}


def exposure_links(
    conn: Conn, entity_id: str, lessees: Lessees | None = None
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """(instrument -> link counted for the payer, lessee -> instruments behind the payer's compute contracts
    that the lessee's lease services instead)."""
    lessees = lessees_by_instrument(conn) if lessees is None else lessees
    family = _family(conn, entity_id)
    owed = [
        ob for ob in obligation_rows(conn, entity_id) if ob["from_entity_id"] == entity_id and ob["type"] in OWED_TYPES
    ]
    owed.sort(key=lambda ob: (OWED_TYPES.index(ob["type"]), ob["id"]))
    links: dict[str, str] = {}
    elsewhere: dict[str, list[str]] = defaultdict(list)
    for ob in owed:
        facts, rows = edge_inputs(conn, ob)
        if not edge_known(facts, rows):
            continue
        for iid in edge_instruments(ob, facts):
            if iid in links:
                continue
            others = sorted(lessees.get(iid, set()) - family)
            if ob["type"] == "Compute purchase" and others:
                for lessee in others:
                    if iid not in elsewhere[lessee]:
                        elsewhere[lessee].append(iid)
                continue
            links[iid] = LINK_LABELS[ob["type"]]
    return links, {k: v for k, v in elsewhere.items() if any(i not in links for i in v)}


def elsewhere_docs(conn: Conn, elsewhere: dict[str, list[str]], info: dict[str, Doc], counted: set[str]) -> list[Doc]:
    """DebtCountedElsewhere rows: per lessee, the instruments (and their summed size) it services that the
    payer's compute contracts also carry; instruments the payer itself counts are left out."""
    names = entity_rows(conn, elsewhere)
    out = []
    for lessee, ids in sorted(elsewhere.items()):
        ids = [i for i in ids if i in info and i not in counted]
        if not ids:
            continue
        out.append(
            {
                "slug": lessee,
                "name": names[lessee]["name"] if lessee in names else lessee,
                "instruments": len(ids),
                "amountB": summed([info[i]["amountB"] for i in ids]),
            }
        )
    out.sort(key=lambda d: (-(d["amountB"].get("value") or 0), d["name"]))
    return out
