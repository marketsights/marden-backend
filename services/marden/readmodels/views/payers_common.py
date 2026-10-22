"""Shared reads for the payer and obligation views: payers, payer, payer_capacity (specs 08, 24),
obligations, obligation_company (spec 18) and leases (spec 25).

Every read honors the snapshot being built: facts through `known_facts` / `known_fact_rows`, model
outputs as the latest on or before `build_date()` (sites_common.latest_output). Obligation rows and
entity rows are identity (who owes whom); every value on an edge is an `obligation` fact (seed/sites.py
field table). Nothing here computes a model value: leverage, funding gaps, downgrade bands, annual
schedules, inferred leases and trigger flags are read from model outputs when those models have run and
are n/d (Sourced value null, or an empty list) until then.

Who is a payer (the `payer` / `payer_capacity` keys, spec 24 "Payers in V1"): an entity that is a
site's tenant or end user (`chain_party`); a cloud, AI lab or neocloud (top-level entity) that owes a
lease, compute contract or guarantee on the obligation map; and a top-level neocloud that is owed one
(an obligor on the GPU debt those contracts back).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any

from marden.core.db import Conn, Row
from marden.core.licensing import hidden_source_names
from marden.readmodels.context import BUSINESS_TZ, day_end, known_fact_rows, known_facts
from marden.readmodels.views.instrument_inputs import num
from marden.readmodels.views.sites_common import (
    Doc,
    billions,
    day,  # noqa: F401 (leases, obligations and payer_capacity import it from here)
    latest_output,
    nd,
    output_field,
    weakest_tag,
    with_value,
)

PAYER_KINDS = ("cloud", "ai_lab", "neocloud")
OWED_TYPES = ("Lease", "Compute purchase", "Guarantee or backstop")
LEASE_TYPES = ("Lease", "Compute purchase")
LINK_LABELS = {"Lease": "Lease", "Compute purchase": "Compute contract", "Guarantee or backstop": "Guarantee"}
EDGE_STATUSES = ("Announced", "Signed", "Started", "Amended", "Disputed", "Terminated")
NODE_TYPES = {
    "ai_lab": "AI lab",
    "cloud": "Cloud",
    "neocloud": "Neocloud",
    "chipmaker": "Chipmaker",
    "developer": "Developer",
    "spv": "SPV",
    "lender": "Lender",
    "bank": "Lender",
    "bdc": "Lender",
    "fund": "Lender",
    "guarantor": "Guarantor",
    "power_provider": "Power provider",
    "utility": "Power provider",
}
# Status rules v0.1: chain status = worst link (red -> Broken, amber -> Strained, else Holding). A party's
# own credit status on the map is the same rule applied to the links it holds across its sites.
LINK_TO_STATUS = {"red": "Broken", "amber": "Strained", "green": "Holding"}
STATUS_RANK = {"Broken": 0, "Strained": 1, "Holding": 2}
EDGE_FIELDS = (
    "edge_type",
    "linked_sites",
    "linked_instruments",
    "total_usd",
    "annual_usd",
    "start",
    "term_years",
    "status",
    "outside_date",
)
EDGE_ROW_FIELDS = ("evidence", "conditions")


# ---------------------------------------------------------------- identity
def top_level(entity: Row) -> bool:
    return entity.get("parent_id") in (None, entity["id"])


def payer_ids(conn: Conn) -> list[str]:
    rows = conn.execute(
        """SELECT entity_id AS id FROM chain_party WHERE role IN ('tenant', 'end_user') AND entity_id IS NOT NULL
           UNION SELECT o.from_entity_id FROM obligation o JOIN entity e ON e.id = o.from_entity_id
            WHERE o.type = ANY(%(owed)s) AND e.kind = ANY(%(kinds)s) AND (e.parent_id IS NULL OR e.parent_id = e.id)
           UNION SELECT o.to_entity_id FROM obligation o JOIN entity e ON e.id = o.to_entity_id
            WHERE o.type = ANY(%(lease)s) AND e.kind = 'neocloud' AND (e.parent_id IS NULL OR e.parent_id = e.id)
           ORDER BY 1""",
        {"owed": list(OWED_TYPES), "kinds": list(PAYER_KINDS), "lease": list(LEASE_TYPES)},
    ).fetchall()
    return [r["id"] for r in rows]


def obligation_company_ids(conn: Conn) -> list[str]:
    rows = conn.execute(
        "SELECT from_entity_id AS id FROM obligation UNION SELECT to_entity_id FROM obligation ORDER BY 1"
    ).fetchall()
    return [r["id"] for r in rows]


def entity_row(conn: Conn, entity_id: str) -> Row | None:
    return conn.execute("SELECT * FROM entity WHERE id = %s", (entity_id,)).fetchone()


def entity_rows(conn: Conn, ids: Iterable[str]) -> dict[str, Row]:
    rows = conn.execute("SELECT * FROM entity WHERE id = ANY(%s)", (sorted(set(ids)),)).fetchall()
    return {r["id"]: r for r in rows}


def site_names(conn: Conn) -> dict[str, str]:
    return {r["slug"]: r["name"] for r in conn.execute("SELECT slug, name FROM site").fetchall()}


def chain_roles(conn: Conn, entity_id: str) -> list[Row]:
    return conn.execute(
        "SELECT site_slug, role, label FROM chain_party WHERE entity_id = %s ORDER BY site_slug, role", (entity_id,)
    ).fetchall()


# ---------------------------------------------------------------- Sourced helpers
def billions_of(row: Row | None, tag: str = "R") -> Doc:
    """A USD fact as Sourced<number> in $B (n/d when absent or not a number)."""
    if row is None or num(row["value"]) is None:
        return nd(tag)
    return with_value(row, billions(num(row["value"])))


def text_of(row: Row | None, tag: str = "R") -> Doc:
    """A fact as Sourced<string> (lists joined with '; ', numbers as text)."""
    if row is None or row["value"] in (None, "", []):
        return nd(tag)
    value = row["value"]
    if isinstance(value, list):
        value = "; ".join(str(v) for v in value if v not in (None, ""))
    elif not isinstance(value, str):
        value = str(value)
    return with_value(row, value)


def number_of(row: Row | None, tag: str = "R") -> Doc:
    if row is None or num(row["value"]) is None:
        return nd(tag)
    return with_value(row, num(row["value"]))


def summed(rows: Sequence[Doc], tag: str = "R") -> Doc:
    """Sum of Sourced numbers: the weakest tag, newest as-of and every source named (n/d if none)."""
    known = [r for r in rows if isinstance(r.get("value"), int | float)]
    if not known:
        return nd(tag)
    if len(known) == 1:
        return dict(known[0])
    names = sorted({r["sourceName"] for r in known if r.get("sourceName")})
    out: Doc = {
        "value": round(sum(r["value"] for r in known), 3),
        "tag": weakest_tag(r["tag"] for r in known),
        "asOf": max(r["asOf"] for r in known),
    }
    if names:
        out["sourceName"] = " + ".join(names)
    methods = sorted({r["methodVersion"] for r in known if r["tag"] == "M" and r.get("methodVersion")})
    if out["tag"] == "M" and methods:
        out["methodVersion"] = " + ".join(methods)  # an M sum names the methods of its M parts
    return out


def model_value(out: Row | None, raw: Any, tag: str = "M") -> Doc:
    """A model output value as Sourced<T> (a Sourced dict passes through); n/d when the model has not run."""
    if out is None or raw is None:
        return nd(tag)
    if isinstance(raw, dict):
        if "value" not in raw or "tag" not in raw:
            return nd(tag)
        got = {k: raw[k] for k in ("value", "tag", "sourceUrl", "sourceName", "asOf", "methodVersion") if k in raw}
        got.setdefault("asOf", out["as_of"].isoformat())
        return got
    version = str(out.get("version") or "")
    method = version if out["model"] in version else f"{out['model']} v{version}".strip()
    return {
        "value": raw,
        "tag": "M",
        "asOf": out["as_of"].isoformat(),
        "methodVersion": method,
        "sourceName": "Marden model",
    }


def model_field(out: Row | None, key: str, tag: str = "M") -> Doc:
    return model_value(out, output_field(out, key), tag)


def evidence_of(row: Row) -> Doc:
    """An `evidence` fact ({date, text, url}) as types.ts Evidence."""
    value = row["value"] if isinstance(row["value"], dict) else {"text": str(row["value"])}
    return {
        "date": str(value.get("date") or row["valid_from"].isoformat()),
        "text": str(value.get("text") or ""),
        "tag": row["tag"].strip(),
        "sourceName": row["source_name"],
        "sourceUrl": str(value.get("url") or row.get("source_url") or ""),
    }


# ---------------------------------------------------------------- party status (status model)
def link_status(conn: Conn, entity_id: str, roles: Sequence[str] | None = None) -> tuple[str | None, Doc | None]:
    """The party's own status: its worst link across the sites it is a chain party of, in the status
    model's latest output for each site (None when no site's model output judges a link of it), with the
    statement that tripped that link (Sourced<string>) when there is one."""
    best: tuple[int, str, Doc | None] | None = None
    for party in chain_roles(conn, entity_id):
        if roles is not None and party["role"] not in roles:
            continue
        chain = output_field(latest_output(conn, "status", "site", party["site_slug"]), "chain")
        for link in chain if isinstance(chain, list) else []:
            if not isinstance(link, dict) or link.get("role") != party["role"]:
                continue
            status = LINK_TO_STATUS.get(link.get("status"))
            if status is None:
                continue
            fact = link.get("fact") if isinstance(link.get("fact"), dict) else None
            if best is None or STATUS_RANK[status] < best[0] or (STATUS_RANK[status] == best[0] and best[2] is None):
                best = (STATUS_RANK[status], status, fact)
    return (best[1], best[2]) if best else (None, None)


def tripped_links(conn: Conn, entity_id: str) -> list[tuple[str, Doc, Doc]]:
    """(site slug, link, fact) for each amber or red link the party holds in the status model's outputs."""
    out = []
    for party in chain_roles(conn, entity_id):
        chain = output_field(latest_output(conn, "status", "site", party["site_slug"]), "chain")
        for link in chain if isinstance(chain, list) else []:
            if (
                isinstance(link, dict)
                and link.get("role") == party["role"]
                and link.get("status") in ("red", "amber")
                and isinstance(link.get("fact"), dict)
            ):
                out.append((party["site_slug"], link, link["fact"]))
    return out


# ---------------------------------------------------------------- obligations (edges)
def obligation_rows(conn: Conn, entity_id: str | None = None) -> list[Row]:
    return conn.execute(
        """SELECT * FROM obligation WHERE %(e)s::text IS NULL OR from_entity_id = %(e)s OR to_entity_id = %(e)s
           ORDER BY id""",
        {"e": entity_id},
    ).fetchall()


def edge_inputs(conn: Conn, ob: Row) -> tuple[dict[str, Row], dict[str, list[Row]]]:
    """The obligation's current facts and every row of its one-row-per-source fields."""
    return known_facts(conn, "obligation", ob["id"], EDGE_FIELDS), known_fact_rows(
        conn, "obligation", ob["id"], EDGE_ROW_FIELDS
    )


def edge_known(facts: dict[str, Row], rows: dict[str, list[Row]]) -> bool:
    """An edge is on the map once Marden knew its type and a source passage for it (spec 18: no edge
    without a document)."""
    return "edge_type" in facts and bool(rows["evidence"])


def _list(row: Row | None) -> list[str]:
    value = row["value"] if row else None
    return [str(v) for v in value if isinstance(v, str) and v] if isinstance(value, list) else []


def edge_sites(ob: Row, facts: dict[str, Row], known_sites: Iterable[str]) -> list[str]:
    sites = set(_list(facts.get("linked_sites")))
    if ob.get("site_slug"):
        sites.add(ob["site_slug"])
    return sorted(sites & set(known_sites))


def edge_instruments(ob: Row, facts: dict[str, Row]) -> list[str]:
    """Debt that depends on this edge: the linked cases' own instruments (not the payer's notes)."""
    return sorted(set(_list(facts.get("linked_instruments"))) | set(ob.get("instrument_ids") or []))


def edge_status(facts: dict[str, Row], rows: dict[str, list[Row]]) -> str:
    """EdgeStatus: the `status` fact when the record states one; otherwise the loader's rule (seed/sites.py
    `status`: Signed when filed) applied to the edge's own sources: Signed if any source is filed (F), else
    Announced (reported or inferred only)."""
    row = facts.get("status")
    if row is not None and row["value"] in EDGE_STATUSES:
        return row["value"]
    tags = {r["tag"].strip() for r in [*facts.values(), *rows["evidence"]]}
    return "Signed" if "F" in tags else "Announced"


def edge_conditions(facts: dict[str, Row], rows: dict[str, list[Row]]) -> list[Doc]:
    out: list[Doc] = []
    seen: set[str] = set()
    for row in rows["conditions"]:
        for text in _list(row):
            if text not in seen:
                seen.add(text)
                out.append(with_value(row, text))
    if (row := facts.get("outside_date")) and isinstance(row["value"], str) and row["value"]:
        out.append(with_value(row, f"Outside date: {row['value']}"))
    return out


def edge_doc(conn: Conn, ob: Row, known_sites: Iterable[str]) -> Doc | None:
    """ObligationEdge (types.ts), or None while Marden has no source for it on the build date."""
    facts, rows = edge_inputs(conn, ob)
    if not edge_known(facts, rows):
        return None
    annual_out = latest_output(conn, "obligations", "obligation", ob["id"])
    annual = []
    for item in output_field(annual_out, "annual") or []:
        if isinstance(item, dict) and isinstance(item.get("year"), int):
            amount = model_value(annual_out, item.get("amountB"))
            if amount["value"] is not None:
                annual.append({"year": item["year"], "amountB": amount})
    return {
        "id": ob["id"],
        "from": ob["from_entity_id"],
        "to": ob["to_entity_id"],
        "type": ob["type"],
        "status": edge_status(facts, rows),
        "totalB": billions_of(facts.get("total_usd")),
        "annual": sorted(annual, key=lambda a: a["year"]),
        "start": text_of(facts.get("start")),
        "termYears": number_of(facts.get("term_years")),
        "conditions": edge_conditions(facts, rows),
        "linkedSites": edge_sites(ob, facts, known_sites),
        "linkedInstruments": edge_instruments(ob, facts),
        "evidence": sorted((evidence_of(r) for r in rows["evidence"]), key=lambda e: e["date"], reverse=True),
    }


def edges_changed_since(conn: Conn, ids: Sequence[str], since: date) -> set[str]:
    """Edges with a fact Marden learned on or after `since` (and by the build date): added or amended."""
    if not ids:
        return set()
    rows = conn.execute(
        """SELECT DISTINCT subject_id FROM fact WHERE subject_type = 'obligation' AND subject_id = ANY(%s)
             AND status IN ('published', 'superseded') AND source_name <> ALL(%s)
             AND learned_at >= %s AND learned_at < %s""",
        (list(ids), hidden_source_names(), datetime.combine(since, datetime.min.time(), BUSINESS_TZ), day_end()),
    ).fetchall()
    return {r["subject_id"] for r in rows}
