"""Holder entities: N-PORT fund series (parent = registrant trust, CIK parsed from the filer string), BDCs (CIK)
and insurers (parent = insurance group). All go through ``common.ensure_entity``.

A series name that several registrants use ('High Yield Fund', 'Core Bond Fund') or that an existing entity of
another kind or registrant already carries is qualified with its registrant ('High Yield Fund (<trust>)'), so
two funds never merge into one entity. The registrant CIK is kept on the registrant entity (``entity.cik``) and
in the series' ``meta.registrant_cik``; a series is not given its trust's CIK, because ``ensure_entity`` matches
on CIK and every series of a trust would collapse into one entity.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from marden.core.db import Conn
from marden.seed.common import ensure_entity, slugify
from marden.seed.holdings_lines import FundSpec
from marden.seed.instruments_parse import core_name


def _existing(conn: Conn, name: str) -> dict[str, Any] | None:
    """The entity ``ensure_entity`` would match on name (id, alias or case-insensitive name), if any."""
    name = " ".join(name.split())
    return conn.execute(
        """SELECT id, kind, cik, meta FROM entity
            WHERE id = %(id)s OR %(name)s = ANY(aliases) OR lower(name) = lower(%(name)s)
            ORDER BY (id = %(id)s) DESC LIMIT 1""",
        {"id": slugify(name), "name": name},
    ).fetchone()


def _registrant_names(funds: dict[str, FundSpec]) -> dict[str, str]:
    """One registrant name per CIK (the filer string and the agg 'reg' field differ in case and spelling):
    the most frequent, ties broken alphabetically, so every run picks the same."""
    seen: dict[str, Counter[str]] = defaultdict(Counter)
    for spec in funds.values():
        if spec.parent_cik and spec.parent_name and spec.meta.get("role") == "nport_series":
            seen[spec.parent_cik][spec.parent_name] += 1
    return {cik: sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] for cik, c in seen.items()}


def ensure_funds(conn: Conn, funds: dict[str, FundSpec]) -> tuple[dict[str, str], Counter[str]]:
    """fund_key -> entity id, and counts by role ('<role>.created' / '<role>.existing')."""
    before = {r["id"] for r in conn.execute("SELECT id FROM entity").fetchall()}
    series_regs: dict[str, set[str | None]] = defaultdict(set)
    for spec in funds.values():
        if spec.meta.get("role") == "nport_series":
            series_regs[spec.name.lower()].add(spec.parent_cik)
    shared = {n for n, regs in series_regs.items() if len(regs) > 1}
    reg_names = _registrant_names(funds)
    out: dict[str, str] = {}
    counts: Counter[str] = Counter()
    for key in sorted(funds):
        spec = funds[key]
        role = str(spec.meta.get("role"))
        if role == "nport_series":
            eid = _series(conn, spec, reg_names.get(spec.parent_cik or ""), spec.name.lower() in shared)
        elif role == "bdc":
            eid = ensure_entity(conn, spec.name, "bdc", cik=spec.cik, meta=spec.meta)
        else:
            parent = None
            if spec.parent_name:
                parent = ensure_entity(conn, spec.parent_name, "company", meta={"role": "insurer_group"})
            eid = ensure_entity(conn, spec.name, "company", parent_id=parent, meta=spec.meta)
        out[key] = eid
        counts[f"{role}.{'existing' if eid in before else 'created'}"] += 1
    return out, counts


def _series(conn: Conn, spec: FundSpec, registrant: str | None, shared_name: bool) -> str:
    reg_id = None
    if spec.parent_cik and registrant:
        reg_id = ensure_entity(conn, registrant, "fund", cik=spec.parent_cik,
                               meta={"role": "nport_registrant", "registrant_cik": spec.parent_cik})
        if core_name(registrant) == core_name(spec.name):
            return reg_id  # a single-series registrant: the trust is the fund
    name = spec.name
    hit = _existing(conn, name)
    foreign = hit is not None and (hit["kind"] != "fund" or (hit["meta"] or {}).get("registrant_cik")
                                   not in (None, spec.parent_cik)) and hit["id"] != reg_id
    if shared_name or foreign:
        name = f"{spec.name} ({registrant or f'CIK {spec.parent_cik}'})"
    return ensure_entity(conn, name, "fund", parent_id=reg_id if reg_id and reg_id != slugify(name) else None,
                         meta=spec.meta)
