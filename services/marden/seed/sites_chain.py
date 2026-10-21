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
from marden.seed.sites_entities import Registry, display_name, is_placeholder, norm
from marden.seed.sites_facts import FactSink, is_nd, weakest
from marden.seed.sites_parties import ChainRow

_MULTI = re.compile(r"\s/\s|,|\band\b|;")
_STOP = frozenset({"jv", "and", "of", "the", "funds", "group", "capital", "holdings", "partners"})
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


def _named_rows(rows: list[ChainRow], roles: tuple[str, ...], *, keep_placeholders: bool) -> list[ChainRow]:
    """Rows of the first role present. 'Not disclosed' rows are dropped; other placeholder names
    ('Noteholders', 'About 20 banks') are kept only when asked (they still describe the lenders)."""
    for role in roles:
        found = [r for r in rows if r.role == role and not is_nd(r.name)
                 and (keep_placeholders or r.entity_ids or not is_placeholder(r.name))]
        if found:
            return found
    return []


def _overlaps(reg: Registry, label: str, eid: str) -> bool:
    """The entity's name shares a word with the label (so 'Fluidstack JV' never maps to TeraWulf)."""
    words = set(norm(display_name(label)).split()) - _STOP
    return bool(words & set(norm(reg.names.get(eid, eid)).split()))


def _entity(arc: Archive, reg: Registry, case: str, link: str, label: str, rows: list[ChainRow],
            roles: tuple[str, ...]) -> str | None:
    if _MULTI.search(label):
        return _single(rows) if link == "lenders" else None
    found = None if is_placeholder(label) else reg.lookup(label)
    if found:
        return found
    candidates = [e for role in roles for r in rows if r.role == role for e in r.entity_ids]
    rowinfo = _rowinfo_entity(arc, reg, case, link)
    candidates += [rowinfo] if rowinfo else []
    return next((e for e in dict.fromkeys(candidates) if _overlaps(reg, label, e)), None)


def chain_links(arc: Archive, reg: Registry, case: str, rows: list[ChainRow]) -> dict[str, tuple[str, str | None]]:
    """LinkRole -> (label, entity id or None)."""
    short = arc.data["aiwc/memo/shortnames.json"].get(case) or [None, None]
    out: dict[str, tuple[str, str | None]] = {}
    plans = (("end_user", None, ("End user",)), ("tenant", short[1], ("Tenant",)),
             ("developer", short[0], ("Developer", "Sponsor")), ("lenders", None, ("Arranger", "Lender")))
    for link, label, roles in plans:
        role_rows = _named_rows(rows, roles, keep_placeholders=link == "lenders")
        if is_nd(label) or (label and label.strip().lower() == "not disclosed"):
            label = None
        every = _named_rows(rows, roles, keep_placeholders=True)
        sole = _single(every) if every and all(r.entity_ids for r in every) else None
        if label and sole and _GENERIC.search(label) and reg.lookup(label) is None:
            label = "; ".join(r.name for r in role_rows)  # the short name predates the disclosed party
        if label is None and role_rows:
            label = "; ".join(r.name for r in role_rows)
        if label:
            out[link] = (label, _entity(arc, reg, case, link, label, rows if link != "lenders" else role_rows,
                                        roles))
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
