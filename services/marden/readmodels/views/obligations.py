"""Obligation map views (spec 18): `obligations` (GET /api/obligations, ObligationGraph) and
`obligation_company` (GET /api/obligations/:company, CompanyObligations).

Edges are the `obligation` rows (who owes whom, edge type) with their facts (seed/sites.py field table):
total and term, start, conditions, linked sites and instruments, and every source passage as Evidence.
An edge shows once Marden knew its type and at least one source passage on the build date. Edge status
is the record's own `status` fact, else Signed when a source is filed and Announced otherwise
(payers_common.edge_status). Annual amounts come from the `obligations` model (subject
('obligation', id), output `annual: [{year, amountB}]`); concentration and the funding gap from the
`obligation_company` model (subject ('entity', id), output `concentration: [{payerId, share}]`,
`fundingGap: [{year, committedB, fundedB, gapB}]`). Until those models run, those lists are empty (n/d).

A node's status is the party's own credit status in the chains: its worst link in the status model's
outputs (red -> Broken, amber -> Strained, green -> Holding), null when it holds no judged link.
KPIs: totalB sums the edges' disclosed totals; filedPct is the share of those dollars whose total is
filed (tag F), in percent; changedThisMonth counts edges with a fact Marden learned this month.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.registry import builder
from marden.readmodels.views.payers_common import (
    NODE_TYPES,
    Doc,
    day,
    edge_doc,
    edges_changed_since,
    entity_rows,
    latest_output,
    link_status,
    model_value,
    obligation_company_ids,
    obligation_rows,
    output_field,
)
from marden.readmodels.views.sites_common import site_slugs


def _total(edge: Doc) -> float:
    value = edge["totalB"].get("value")
    return float(value) if isinstance(value, int | float) else 0.0


def edges_for(conn: Conn, entity_id: str | None = None) -> list[Doc]:
    known_sites = site_slugs(conn)
    docs = (edge_doc(conn, ob, known_sites) for ob in obligation_rows(conn, entity_id))
    return [d for d in docs if d is not None]


def node_doc(conn: Conn, entity: Row, edges: list[Doc]) -> Doc:
    """ObligationNode: owedB = disclosed totals of edges into it, owesB = out of it ($B)."""
    status, _ = link_status(conn, entity["id"])
    return {
        "id": entity["id"],
        "name": entity["name"],
        "type": NODE_TYPES.get(entity["kind"], "Other"),
        "status": status,
        "owedB": round(sum(_total(e) for e in edges if e["to"] == entity["id"]), 3),
        "owesB": round(sum(_total(e) for e in edges if e["from"] == entity["id"]), 3),
    }


def graph_kpis(conn: Conn, edges: list[Doc]) -> Doc:
    total = sum(_total(e) for e in edges)
    filed = sum(_total(e) for e in edges if e["totalB"].get("tag") == "F")
    month_start = day().replace(day=1)
    changed = edges_changed_since(conn, [e["id"] for e in edges], month_start)
    return {
        "totalB": round(total, 3),
        "filedPct": round(100 * filed / total, 1) if total else 0.0,
        "changedThisMonth": len(changed),
    }


def _edge_sort(edge: Doc) -> tuple:
    return (-_total(edge), edge["from"], edge["to"], edge["type"])


@builder("obligations", many=True)
def build_obligations(conn: Conn):
    """Every edge Marden has a source for, largest disclosed total first, with its two ends as nodes."""
    edges = sorted(edges_for(conn), key=_edge_sort)
    entities = entity_rows(conn, [e["from"] for e in edges] + [e["to"] for e in edges])
    nodes = [node_doc(conn, entities[i], edges) for i in sorted(entities)]
    nodes.sort(key=lambda n: (-(n["owedB"] + n["owesB"]), n["name"]))
    yield "all", {"asOf": day().isoformat(), "nodes": nodes, "edges": edges, "kpis": graph_kpis(conn, edges)}


# ---------------------------------------------------------------- company page
def owes_by_year(owes: list[Doc]) -> list[Doc]:
    """Annual commitments by year and counterparty, from the edges' annual amounts (the obligations model)."""
    by_year: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for edge in owes:
        for item in edge["annual"]:
            value = item["amountB"].get("value")
            if isinstance(value, int | float):
                by_year[item["year"]][edge["to"]] += float(value)
    return [
        {"year": year, "byCounterparty": [{"id": cid, "amountB": round(v, 3)} for cid, v in sorted(parts.items())]}
        for year, parts in sorted(by_year.items())
    ]


def concentration(out: Row | None) -> list[Doc]:
    rows = []
    for item in output_field(out, "concentration") or []:
        if isinstance(item, dict) and isinstance(item.get("payerId"), str):
            share = model_value(out, item.get("share"))
            if share["value"] is not None:
                rows.append({"payerId": item["payerId"], "share": share})
    return rows


def _number(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("value")
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def funding_gap(out: Row | None) -> list[Doc]:
    rows = []
    for item in output_field(out, "fundingGap") or []:
        if not isinstance(item, dict) or not isinstance(item.get("year"), int):
            continue
        committed, funded = _number(item.get("committedB")), _number(item.get("fundedB"))
        gap = model_value(out, item.get("gapB"))
        if committed is None or funded is None or gap["value"] is None:
            continue
        rows.append({"year": item["year"], "committedB": committed, "fundedB": funded, "gapB": gap})
    return sorted(rows, key=lambda r: r["year"])


@builder("obligation_company", keys=obligation_company_ids)
def build_obligation_company(conn: Conn, key: str) -> Doc | None:
    """CompanyObligations for one entity; None when no edge at either end is known on the build date."""
    edges = sorted(edges_for(conn, key), key=_edge_sort)
    entity = entity_rows(conn, [key]).get(key)
    if entity is None or not edges:
        return None
    owes = [e for e in edges if e["from"] == key]
    is_owed = [e for e in edges if e["to"] == key]
    out = latest_output(conn, "obligation_company", "entity", key)
    return {
        "node": node_doc(conn, entity, edges),
        "owes": owes,
        "isOwed": is_owed,
        "owesByYear": owes_by_year(owes),
        "concentration": concentration(out),
        "fundingGap": funding_gap(out),
    }
