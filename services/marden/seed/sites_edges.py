"""Obligation edges for the `sites` loader: who owes whom, from four archive sources.

Sources (each record keeps its file, URL, tag and quote):
- HTML ``aichain.edges``: tenant -> Lease, customer / end user -> Compute purchase, guarantor and
  credit support -> Guarantee or backstop. An end user owes the tenant on the same sheet row.
- ``aiwc/q3x/ctr.json``: the case's main contract (GPU capacity contracts -> Compute purchase).
- ``aiwc/cases/<CASE>.json credit_support[]``: guarantees, backstops, warrants, prepayments and
  utility-contract support (-> Power purchase, owed to the site's utility).
- ``aiwcredit.rowinfo.x`` tenant links: the tenant group of each instrument exposed to the case.

Edges are deduplicated on (from, to, type). The project side of an edge is the case's borrower
group (the aidebt group of the case's own debt, = the aichain sheet); when the counterparty is that
same group (a parent guaranteeing its own project) the case's borrower SPV is used instead.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from marden.seed.common import slugify, tag_for_url
from marden.seed.sites_archive import HTML, Archive
from marden.seed.sites_entities import Registry, display_name, norm
from marden.seed.sites_facts import Src, is_nd, number, pick_url, usd
from marden.seed.sites_parties import ChainRow

EDGE_MAP = Path(__file__).with_name("chain_edge_sites.json")
AICHAIN_TYPE = {"tenant": "Lease", "customer": "Compute purchase", "end user": "Compute purchase",
                "guarantor": "Guarantee or backstop", "credit support": "Guarantee or backstop"}
_TO_PARTY = re.compile(r"\(to ([^,()]+?)(?:,|\))")
_STARTED = re.compile(r"\b(commenced|accepted|live|rent scaling|delivered)\b", re.I)


@dataclass
class Record:
    """One source record's view of an obligation."""

    kind: str                     # aichain | ctr | credit_support | rowinfo
    src: Src
    url: str | None
    tag: str
    case: str | None
    detail: dict[str, Any]
    quote: str | None = None
    evidence: str | None = None
    conditions: list[str] = field(default_factory=list)
    scalars: dict[str, tuple[Any, str | None]] = field(default_factory=dict)
    payer_instruments: list[str] = field(default_factory=list)


@dataclass
class Edge:
    id: str
    type: str
    from_id: str
    to_id: str
    records: list[Record] = field(default_factory=list)

    def cases(self) -> list[str]:
        return sorted({r.case for r in self.records if r.case})


@dataclass
class EdgeBuilder:
    arc: Archive
    reg: Registry
    chain: dict[str, list[ChainRow]]
    slugs: dict[str, str]
    edges: dict[str, Edge] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ parties on the project side
    def _role_entity(self, case: str, role: str) -> str | None:
        ids = {e for r in self.chain.get(case, []) if r.role == role for e in r.entity_ids}
        return ids.pop() if len(ids) == 1 else None

    def project_side(self, case: str | None, group: str | None, payer: str) -> str | None:
        """The project party an edge points at: the borrower group, else its borrower SPV."""
        grp_id = self.reg.grp.get(group or "") if group else None
        if case:
            grp_id = self.reg.grp.get(self.arc.case_grp.get(case, "")) or grp_id
            options = [grp_id, self._role_entity(case, "Borrower"), self._role_entity(case, "Project company")]
        else:
            options = [grp_id]
        return next((o for o in options if o and o != payer), None)

    def _utility(self, case: str, text: str) -> str | None:
        rows = [r for r in self.chain.get(case, []) if r.role == "Utility" and r.entity_ids]
        named = [r for r in rows if norm(display_name(r.name)) and norm(display_name(r.name)) in norm(text)]
        pick = named or rows
        return pick[0].entity_ids[0] if len(pick) == 1 or named else None

    def add(self, edge_type: str, from_id: str | None, to_id: str | None, rec: Record, what: str) -> None:
        if not from_id or not to_id:
            self.skipped.append(f"{rec.kind} {what}: party not resolved (from={from_id}, to={to_id})")
            return
        if from_id == to_id:
            self.skipped.append(f"{rec.kind} {what}: self-edge on {from_id}")
            return
        eid = f"{from_id}--{to_id}--{slugify(edge_type)}"
        edge = self.edges.setdefault(eid, Edge(eid, edge_type, from_id, to_id))
        edge.records.append(rec)

    # ------------------------------------------------------------ the four sources
    def from_aichain(self) -> None:
        chain = self.arc.aichain
        links = {(e["sheet"], e["cp"]): e for e in json.loads(EDGE_MAP.read_text())["edges"]}
        tenant_of = {(e["sheet"], e["cp"]): e["tenant"] for e in chain["edges"] if e["role"] == "tenant"}
        for e in chain["edges"]:
            sheet, cp, role = e["sheet"], e["cp"], e["role"]
            issuer = chain["issuers"].get(sheet, {})
            sources = issuer.get("sources") or {}
            urls = [sources.get(k.strip()) for k in str(e.get("src") or "").split(",")]
            url = pick_url(urls)
            link = links.get((sheet, cp))
            case = link["case"] if link and link["keyword"].lower() in (e.get("project") or "").lower() else None
            src = self.arc.src(HTML, issuer.get("asOf"))
            detail = {"sheet": sheet, "cp": cp, "role": role, "project": e.get("project"), "mw": e.get("mw"),
                      "contract": e.get("contract"), "backstop": e.get("backstop"), "why": e.get("why"),
                      "tenant_text": e.get("tenantText"), "inferred": e.get("inferred"),
                      "site": self.slugs.get(case) if case else None, "source_url": url}
            rec = Record("aichain", src, url, "M" if e.get("inferred") else tag_for_url(url), case,
                         {k: v for k, v in detail.items() if not is_nd(v)}, evidence=e.get("why"),
                         conditions=[c for c in (e.get("contract"), e.get("backstop")) if not is_nd(c)])
            payer = self.reg.tenant.get(e["tenant"])
            if role == "end user":
                target = self.reg.tenant.get(tenant_of.get((sheet, cp), ""))
            else:
                target = self.project_side(case, sheet, payer or "")
            edge_type = AICHAIN_TYPE.get(role)
            if edge_type is None:
                self.skipped.append(f"aichain {sheet}:{cp} {e['tenant']}: role {role!r} has no edge type")
                continue
            self.add(edge_type, payer, target, rec, f"{sheet}:{cp} {e['tenant']} ({role})")

    def _lessee(self, text: str) -> str | None:
        """The aichain tenant named first in a contract's lessee text; unnamed lessees -> 'undisclosed'."""
        hits = []
        for tid, eid in self.reg.tenant.items():
            name = self.reg.names.get(eid, "")
            for key in {k.strip(" )") for k in re.split(r"\s*/\s*|\s*\(", name) if len(k.strip(" )")) >= 3}:
                m = re.search(rf"(?<![A-Za-z]){re.escape(key)}(?![A-Za-z])", text, re.I)
                if m and tid != "undisclosed":
                    hits.append((m.start(), eid))
        if hits:
            return min(hits)[1]
        return self.reg.tenant.get("undisclosed")

    def from_contracts(self) -> None:
        src = self.arc.src("aiwc/q3x/ctr.json")
        for case, ctr in sorted(self.arc.ctr_by_case.items()):
            if case not in self.slugs:
                continue
            url = pick_url(ctr.get("src"))
            payer = self._lessee(ctr.get("tenant") or "")
            edge_type = "Compute purchase" if ctr.get("idle") == "gpu" else "Lease"
            status = None
            if not is_nd(ctr.get("start")) and _STARTED.search(ctr["start"]):
                status = "Started"
            elif tag_for_url(url) == "F":
                status = "Signed"
            term = ctr.get("term")
            scalars = {"total_usd": (usd(ctr.get("tcv_m")), "USD"), "annual_usd": (usd(ctr.get("rent_m")), "USD"),
                       "start": (ctr.get("start"), None), "term_years": (number(term), "years"),
                       "outside_date": (ctr.get("outside"), None), "status": (status, None)}
            detail = {"site": self.slugs[case], "tenant": ctr.get("tenant"), "term": term, "start": ctr.get("start"),
                      "total_usd": usd(ctr.get("tcv_m")), "annual_usd": usd(ctr.get("rent_m")),
                      "rent_basis": ctr.get("rb"), "termination": ctr.get("exit"), "renewal": ctr.get("renew"),
                      "guarantee": ctr.get("guar"), "outside_date": ctr.get("outside"), "kind": ctr.get("idle"),
                      "source_url": url, "quote": ctr.get("quote")}
            rec = Record("ctr", src, url, tag_for_url(url), case, {k: v for k, v in detail.items() if not is_nd(v)},
                         quote=ctr.get("quote"), evidence=ctr.get("quote"),
                         conditions=[c for c in (ctr.get("rb"), ctr.get("exit"), ctr.get("renew"), ctr.get("guar"))
                                     if not is_nd(c)],
                         scalars={k: v for k, v in scalars.items() if not is_nd(v[0])})
            self.add(edge_type, payer, self.project_side(case, None, payer or ""), rec, f"ctr {case}")

    def from_credit_support(self) -> None:
        for case in sorted(self.slugs):
            src = self.arc.src(f"aiwc/cases/{case}.json")
            for i, cs in enumerate(self.arc.case(case).get("credit_support") or []):
                kind, provider = (cs.get("type") or "").lower(), cs.get("provider") or ""
                what = f"credit_support {case}[{i}] {cs.get('type')}"
                if kind.startswith("none") or is_nd(provider):
                    self.skipped.append(f"{what}: record says no credit support")
                    continue
                giver, _, receiver = provider.partition("->")
                from_id = self.reg.resolve(giver, "guarantor")
                to_party = _TO_PARTY.search(provider)
                if "warrant" in kind:
                    edge_type = "Warrant"
                elif "prepayment" in kind:
                    edge_type = "Compute purchase"
                elif "utility" in kind:
                    edge_type = "Power purchase"
                else:
                    edge_type = "Guarantee or backstop"
                if receiver.strip():
                    to_id = self.reg.resolve(receiver, "company")
                elif to_party:
                    to_id = self.reg.lookup(to_party[1])
                elif edge_type == "Power purchase":
                    to_id = self._utility(case, f"{provider} {cs.get('covers') or ''}")
                else:
                    to_id = self.project_side(case, None, from_id or "")
                url = pick_url(cs.get("src"))
                amount = usd(cs.get("amount_m"))
                detail = {"site": self.slugs[case], "type": cs.get("type"), "provider": provider, "amount_usd": amount,
                          "covers": cs.get("covers"), "inferred": True if cs.get("inferred") else None,
                          "source_url": url, "quote": cs.get("quote")}
                scalars = {"total_usd": (amount, "USD")} if edge_type == "Guarantee or backstop" and amount else {}
                rec = Record("credit_support", src, url, "M" if cs.get("inferred") else tag_for_url(url), case,
                             {k: v for k, v in detail.items() if not is_nd(v)}, quote=cs.get("quote"),
                             evidence=cs.get("quote") or cs.get("covers"),
                             conditions=[c for c in (cs.get("covers"),) if not is_nd(c)], scalars=scalars)
                self.add(edge_type, from_id, to_id, rec, what)

    def from_rowinfo(self) -> None:
        src = self.arc.src("aiwcredit.json")
        for case in sorted(self.slugs):
            tenants = self.arc.case_links.get(case, {}).get("Tenant", [])
            groups: dict[str, list[str]] = {}
            for iid in tenants:
                grp = self.arc.debt_rows.get(iid, {}).get("grp")
                if grp:
                    groups.setdefault(grp, []).append(iid)
            ctr = self.arc.ctr_by_case.get(case) or {}
            edge_type = "Compute purchase" if ctr.get("idle") == "gpu" else "Lease"
            for grp, ids in sorted(groups.items()):
                payer = self.reg.grp.get(grp)
                rec = Record("rowinfo", src, None, "M", case, {"site": self.slugs[case], "tenant_group": grp},
                             payer_instruments=sorted(ids))
                self.add(edge_type, payer, self.project_side(case, None, payer or ""), rec, f"rowinfo {case} {grp}")

    def build(self) -> dict[str, Edge]:
        self.from_aichain()
        self.from_contracts()
        self.from_credit_support()
        self.from_rowinfo()
        return self.edges


def by_source(edge: Edge) -> Iterator[tuple[str, list[Record]]]:
    """An edge's records grouped by source file, in a stable order."""
    groups: dict[str, list[Record]] = {}
    for r in edge.records:
        groups.setdefault(r.src.relpath, []).append(r)
    yield from sorted(groups.items())


def record_date(rec: Record) -> date:
    return rec.src.as_of
