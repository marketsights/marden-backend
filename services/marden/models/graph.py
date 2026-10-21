"""The obligation graph (spec 18) read point in time: edges, their amounts and year-by-year schedules.

Shared by the obligations, obligation_company, contagion, payer_capacity, leases and stress models.

An edge is an `obligation` row (who owes whom, the edge type) with its `obligation` facts as public on the
as-of day (marden.models.pit). It is on the map once its type and at least one source passage are known
(spec 18: no edge without a document). Amounts, in order of preference:

- total: the edge's `total_usd` fact; else the payer's `funding_fact` naming the payee ("Oracle contract",
  value "~$300bn"; tag R, as filed or reported), the only other sourced statement of a contract size;
- term: `term_years`, else the funding fact's note ("~5 years"); start year: the first year `start` names
  ("2026-Q4 (first hall)" -> 2026), else the note's "start(s) 2027";
- annual: `annual_usd`, else the funding fact's "~$60bn a year", else total / term (tag M, straight line).

The schedule spreads the annual amount over the term from the start year (a fractional last year pays
its fraction). An edge with no amount, no start year or no term has no schedule and says which is missing;
nothing is spread by guesswork. A terminated edge has no schedule.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.core.db import Conn, Row
from marden.models import pit
from marden.models.sourced import m_value, num
from marden.readmodels.views.payers_common import (
    EDGE_FIELDS,
    EDGE_ROW_FIELDS,
    LINK_TO_STATUS,
    NODE_TYPES,
    STATUS_RANK,
    billions_of,
    edge_conditions,
    edge_instruments,
    edge_sites,
    edge_status,
    evidence_of,
    number_of,
    text_of,
)
from marden.readmodels.views.sites_common import with_value

GRAPH_METHOD = "obligations v0.1"
_YEAR = re.compile(r"\b(20\d\d)\b")
_BN = re.compile(r"\$\s*~?\s*(\d+(?:\.\d+)?)\s*(bn|billion|b|tn|trillion)\b", re.I)
_TERM = re.compile(r"~?\s*(\d+(?:\.\d+)?)[- ]?(?:years?|yrs?)\b", re.I)
_START = re.compile(r"start(?:s|ing)?\s+(?:in\s+)?(20\d\d)", re.I)
_PER_YEAR = re.compile(r"\$?\s*~?\s*(\d+(?:\.\d+)?)\s*bn\s+a\s+year", re.I)
_CONTRACT_LABEL = re.compile(r"^(.*?)\s+(?:contracts?|lease|order forms?)$", re.I)


def usd_of_text(text: Any) -> float | None:
    """'~$300bn' -> 3e11; 'up to $22.4bn' -> 2.24e10 (the stated maximum); None when no amount is stated."""
    m = _BN.search(str(text or ""))
    if not m:
        return None
    scale = 1e12 if m.group(2).lower().startswith("t") else 1e9
    return float(m.group(1)) * scale


def start_year(text: Any) -> int | None:
    m = _YEAR.search(str(text or ""))
    return int(m.group(1)) if m else None


@dataclass
class Amount:
    usd: float
    row: Row
    tag: str
    derived: str | None = None  # how it was read when not the edge's own fact

    def sourced_b(self) -> dict[str, Any]:
        out = with_value(self.row, round(self.usd / 1e9, 3))
        out["tag"] = self.tag
        return out


@dataclass
class Edge:
    ob: Row
    facts: dict[str, Row]
    rows: dict[str, list[Row]]
    total: Amount | None = None
    annual: Amount | None = None
    term: float | None = None
    start: int | None = None
    missing: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.ob["id"]

    @property
    def payer(self) -> str:
        return self.ob["from_entity_id"]

    @property
    def payee(self) -> str:
        return self.ob["to_entity_id"]

    @property
    def type(self) -> str:
        return self.ob["type"]

    @property
    def status(self) -> str:
        return edge_status(self.facts, self.rows)

    def schedule(self) -> list[tuple[int, float, str]]:
        """[(year, usd, tag)] of the payments by year (empty when amount, start or term is unknown)."""
        if self.status == "Terminated" or self.start is None:
            return []
        if self.annual is not None:
            per, tag = self.annual.usd, self.annual.tag
            term = self.term if self.term else (self.total.usd / per if self.total and per else None)
        elif self.total is not None and self.term:
            per, tag, term = self.total.usd / self.term, "M", self.term
        else:
            return []
        if not term or term <= 0 or per <= 0:
            return []
        years = math.ceil(term - 1e-9)
        out = []
        for i in range(years):
            share = min(1.0, term - i)
            out.append((self.start + i, per * share, tag))
        return out

    def annual_docs(self, day: date) -> list[dict[str, Any]]:
        docs = []
        for year, usd, tag in self.schedule():
            if tag == "M":
                amount = m_value(round(usd / 1e9, 3), day, GRAPH_METHOD, source_name="Marden obligation map")
            else:
                assert self.annual is not None
                amount = {**self.annual.sourced_b(), "value": round(usd / 1e9, 3)}
            docs.append({"year": year, "amountB": amount})
        return docs

    def annual_usd(self, year: int) -> float | None:
        for y, usd, _ in self.schedule():
            if y == year:
                return usd
        return None

    def doc(self, day: date, known_sites: set[str]) -> dict[str, Any]:
        """ObligationEdge (types.ts)."""
        total = self.total.sourced_b() if self.total else billions_of(None)
        return {
            "id": self.id,
            "from": self.payer,
            "to": self.payee,
            "type": self.type,
            "status": self.status,
            "totalB": total,
            "annual": self.annual_docs(day),
            "start": text_of(self.facts.get("start")),
            "termYears": number_of(self.facts.get("term_years")) if self.facts.get("term_years") else (
                m_value(self.term, day, GRAPH_METHOD, source_name="Marden obligation map") if self.term
                else number_of(None)),
            "conditions": edge_conditions(self.facts, self.rows),
            "linkedSites": edge_sites(self.ob, self.facts, known_sites),
            "linkedInstruments": self.instruments(),
            "evidence": sorted((evidence_of(r) for r in self.rows["evidence"]), key=lambda e: e["date"],
                               reverse=True),
        }

    def instruments(self) -> list[str]:
        return edge_instruments(self.ob, self.facts)


def _funding_facts(conn: Conn, day: date, payers: list[str]) -> dict[str, list[Row]]:
    out: dict[str, list[Row]] = {}
    for r in pit.fact_rows(conn, day, "entity", payers, ["funding_fact"]):
        if isinstance(r["value"], dict):
            out.setdefault(r["subject_id"], []).append(r)
    return out


def _match_funding(edge: Edge, names: dict[str, str], funding: list[Row]) -> Row | None:
    """The payer's funding fact whose label names the payee ('Oracle contract' -> oracle)."""
    payee = names.get(edge.payee, "").lower()
    for row in funding:
        m = _CONTRACT_LABEL.match(str(row["value"].get("label") or "").strip())
        if m and payee and (m.group(1).lower() == payee or payee.startswith(m.group(1).lower())):
            return row
    return None


def _amounts(edge: Edge, funding_row: Row | None) -> None:
    f = edge.facts
    if (row := f.get("total_usd")) is not None and num(row["value"]):
        edge.total = Amount(float(num(row["value"])), row, row["tag"].strip())
    if (row := f.get("annual_usd")) is not None and num(row["value"]):
        edge.annual = Amount(float(num(row["value"])), row, row["tag"].strip())
    if (row := f.get("term_years")) is not None and num(row["value"]):
        edge.term = float(num(row["value"]))
    edge.start = start_year((f.get("start") or {}).get("value"))
    if funding_row is not None:
        v, note = funding_row["value"], str(funding_row["value"].get("note") or "")
        tag = funding_row["tag"].strip()
        if edge.total is None and (usd := usd_of_text(v.get("value"))):
            edge.total = Amount(usd, funding_row, tag, "funding_fact value")
        if edge.annual is None and (m := _PER_YEAR.search(note)):
            edge.annual = Amount(float(m.group(1)) * 1e9, funding_row, tag, "funding_fact note")
        if edge.term is None and (m := _TERM.search(note)):
            edge.term = float(m.group(1))
        if edge.start is None and (m := _START.search(note)):
            edge.start = int(m.group(1))
    if edge.total is None and edge.annual is None:
        edge.missing.append(f"obligation {edge.id}: total_usd / annual_usd")
    if edge.start is None:
        edge.missing.append(f"obligation {edge.id}: start (year)")
    if edge.term is None and not (edge.total and edge.annual):
        edge.missing.append(f"obligation {edge.id}: term_years")


def load_edges(conn: Conn, day: date) -> list[Edge]:
    """Every edge on the map on `day` (type and a source passage public by then), with its amounts."""
    obs = conn.execute("SELECT * FROM obligation ORDER BY id").fetchall()
    rows = pit.fact_rows(conn, day, "obligation", None, [*EDGE_FIELDS, *EDGE_ROW_FIELDS])
    by_edge: dict[str, dict[str, list[Row]]] = {}
    for r in rows:
        by_edge.setdefault(r["subject_id"], {}).setdefault(r["field"], []).append(r)
    names = entity_names(conn)
    funding = _funding_facts(conn, day, sorted({o["from_entity_id"] for o in obs}))
    edges = []
    for ob in obs:
        fields = by_edge.get(ob["id"], {})
        facts = {f: cur for f in EDGE_FIELDS if (cur := pit.current_of(fields.get(f, []))) is not None}
        item_rows = {f: fields.get(f, []) for f in EDGE_ROW_FIELDS}
        if "edge_type" not in facts or not item_rows["evidence"]:
            continue
        edge = Edge(ob, facts, item_rows)
        _amounts(edge, _match_funding(edge, names, funding.get(edge.payer, [])))
        edges.append(edge)
    return edges


def entity_names(conn: Conn) -> dict[str, str]:
    return {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM entity").fetchall()}


def entity_kinds(conn: Conn) -> dict[str, str]:
    return {r["id"]: r["kind"] for r in conn.execute("SELECT id, kind FROM entity").fetchall()}


def known_sites(conn: Conn) -> set[str]:
    return {r["slug"] for r in conn.execute("SELECT slug FROM site").fetchall()}


def party_status(conn: Conn, entity_id: str, day: date) -> str | None:
    """The party's own credit status on the map: its worst link across the sites it is a chain party of, in
    the status model's latest output on or before `day` (status rules v0.1); None when no link is judged."""
    from marden.models.runs import latest_output

    best: tuple[int, str] | None = None
    parties = conn.execute("SELECT site_slug, role FROM chain_party WHERE entity_id = %s", (entity_id,)).fetchall()
    for p in parties:
        out = latest_output(conn, "status", "site", p["site_slug"], day)
        chain = out["output"].get("chain") if out is not None and isinstance(out["output"], dict) else None
        for link in chain if isinstance(chain, list) else []:
            if isinstance(link, dict) and link.get("role") == p["role"]:
                status = LINK_TO_STATUS.get(link.get("status"))
                if status is not None and (best is None or STATUS_RANK[status] < best[0]):
                    best = (STATUS_RANK[status], status)
    return best[1] if best else None


def node_doc(conn: Conn, entity_id: str, edges: list[Edge], day: date, names: dict[str, str],
             kinds: dict[str, str]) -> dict[str, Any]:
    """ObligationNode: owedB = disclosed totals of edges into it, owesB = out of it ($B)."""
    def total(e: Edge) -> float:
        return e.total.usd / 1e9 if e.total else 0.0

    return {
        "id": entity_id,
        "name": names.get(entity_id, entity_id),
        "type": NODE_TYPES.get(kinds.get(entity_id, ""), "Other"),
        "status": party_status(conn, entity_id, day),
        "owedB": round(sum(total(e) for e in edges if e.payee == entity_id), 3),
        "owesB": round(sum(total(e) for e in edges if e.payer == entity_id), 3),
    }
