"""`chain_party` rows and the party-list facts for each site (field table: seed/sites.py).

The four chain links (types.ts LinkRole) per site:
- end_user: memo.chain 'End user' rows.
- tenant: the payer short name (memo/shortnames.json [1]), else memo.chain 'Tenant' rows.
- developer: the sponsor short name (memo/shortnames.json [0]), else memo 'Developer' / 'Sponsor' rows.
- lenders: memo.chain 'Arranger' rows (the lead banks), else 'Lender' rows.
The label is the display text. The entity is set only when the label names one party: matched by
name, else the single memo.chain party of that role, else the single aidebt group linked to the
case in that role (aiwcredit.rowinfo.x).
"""

from __future__ import annotations

import re

from marden.core.db import Conn
from marden.seed.common import tag_for_basis
from marden.seed.sites_archive import Archive
from marden.seed.sites_entities import Registry, is_placeholder
from marden.seed.sites_facts import FactSink, is_nd, weakest
from marden.seed.sites_parties import ChainRow

_MULTI = re.compile(r"\s/\s|,|\band\b|;")
_GENERIC = re.compile(r"\b(hyperscalers?|customers|tenants?|IG|investment-grade)\b", re.I)
_ROWINFO_ROLES = {"tenant": ("Tenant",), "developer": ("Developer", "Sponsor")}


def _single(rows: list[ChainRow]) -> str | None:
    ids = {e for r in rows for e in r.entity_ids}
    return ids.pop() if len(ids) == 1 else None


def _rowinfo_entity(arc: Archive, reg: Registry, case: str, link: str) -> str | None:
    roles = _ROWINFO_ROLES.get(link, ())
    groups = {arc.debt_rows[i]["grp"] for role in roles for i in arc.case_links.get(case, {}).get(role, [])
              if i in arc.debt_rows}
    return reg.grp.get(groups.pop()) if len(groups) == 1 else None


def _named_rows(rows: list[ChainRow], *roles: str) -> list[ChainRow]:
    for role in roles:
        found = [r for r in rows if r.role == role and not is_nd(r.name) and r.name.strip().lower() != "not disclosed"]
        if found:
            return found
    return []


def chain_links(arc: Archive, reg: Registry, case: str, rows: list[ChainRow]) -> dict[str, tuple[str, str | None]]:
    """LinkRole -> (label, entity id or None)."""
    short = arc.data["aiwc/memo/shortnames.json"].get(case) or [None, None]
    out: dict[str, tuple[str, str | None]] = {}
    plans = (("end_user", None, ("End user",)), ("tenant", short[1], ("Tenant",)),
             ("developer", short[0], ("Developer", "Sponsor")), ("lenders", None, ("Arranger", "Lender")))
    for link, label, roles in plans:
        role_rows = _named_rows(rows, *roles)
        if is_nd(label) or (label and label.strip().lower() == "not disclosed"):
            label = None
        sole = _single(role_rows) if all(r.entity_ids for r in role_rows) else None
        if label and sole and _GENERIC.search(label) and reg.lookup(label) is None:
            label = "; ".join(r.name for r in role_rows)  # the short name predates the disclosed party
        if label is None and role_rows:
            label = "; ".join(r.name for r in role_rows)
        if not label:
            continue
        entity = None
        if not _MULTI.search(label):
            entity = reg.lookup(label) if not is_placeholder(label) else None
            entity = entity or _single(role_rows) or _rowinfo_entity(arc, reg, case, link)
        elif link in ("end_user", "lenders"):
            entity = _single(role_rows)
        out[link] = (label, entity)
    return out


def write_chain(conn: Conn, sink: FactSink, arc: Archive, reg: Registry, case: str, slug: str,
                rows: list[ChainRow]) -> int:
    links = chain_links(arc, reg, case, rows)
    for link, (label, entity) in sorted(links.items()):
        conn.execute(
            """INSERT INTO chain_party (site_slug, role, entity_id, label) VALUES (%s, %s, %s, %s)
               ON CONFLICT (site_slug, role) DO UPDATE SET entity_id = EXCLUDED.entity_id, label = EXCLUDED.label""",
            (slug, link, entity, label),
        )
    src = arc.src(f"aiwc/memo/{case}.json")
    for presumed, fld in ((False, "chain_parties"), (True, "chain_parties_presumed")):
        picked = [r for r in rows if (tag_for_basis(r.basis) == "M") == presumed]
        items = [{"role": r.role, "name": r.name, "what": r.what, "basis": r.basis, "ticker": r.ticker,
                  "group": r.grp, "entity_ids": list(r.entity_ids) or None, "tag": tag_for_basis(r.basis)}
                 for r in picked]
        items = [{k: v for k, v in i.items() if v is not None} for i in items]
        if items:
            sink.add("site", slug, fld, items, src=src, tag="M" if presumed else weakest(i["tag"] for i in items))
    return len(links)
