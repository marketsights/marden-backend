"""Copy the real rows the price engine reads from the local `marden` database into the test database.

What the loaders and collectors stored: every entity and site identity row, the chosen instruments and every
bond of their issuers and of the chosen sites' tenants (identity rows, terms facts, price observations:
N-PORT, BDC, TRACE, exchange quotes, dealer quotes), the chain parties, the Treasury / SOFR / equity / issue
comp / GPU residual series, the ICE BofA OAS series and their fund-OAS proxies (etf_oas_*), the issuers' XBRL
debt and share facts, the chosen sites' power and permit facts and events, and the cushion outputs of those
sites. Document references are cleared (the raw documents are
not copied). Skips when the source database or the rows are not there.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg
import pytest
from conftest import BASE_URL, TEST_URL
from psycopg import sql

from marden.models.price_equity import DEBT_FIELDS, SHARE_FIELDS
from marden.models.price_signals import SITE_FIELDS
from marden.models.price_terms import TERM_FIELDS


def _columns(conn: psycopg.Connection, table: str) -> list[str]:
    rows = conn.execute(
        """SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position""", (table,)).fetchall()
    return [r[0] for r in rows]


def _copy(src: psycopg.Connection, dst: psycopg.Connection, table: str, where: str, params: dict[str, Any],
          nulls: Sequence[str] = ()) -> None:
    have = set(_columns(src, table))
    cols = [c for c in _columns(dst, table) if c in have]
    select = sql.SQL(", ").join(sql.SQL("NULL") if c in nulls else sql.Identifier(c) for c in cols)
    query = sql.SQL("COPY (SELECT {} FROM {} WHERE {}) TO STDOUT").format(select, sql.Identifier(table), sql.SQL(where))
    target = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(table), sql.SQL(", ").join(sql.Identifier(c) for c in cols))
    with src.cursor().copy(query, params) as out, dst.cursor().copy(target) as inp:
        for chunk in out:
            inp.write(chunk)


def copy_price_inputs(instruments: Sequence[str]) -> list[str]:
    """Copy what pricing `instruments` needs; marks exactly those instruments (and their sites) V1."""
    try:
        src = psycopg.connect(BASE_URL, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"source database not reachable: {exc}")
    with src, psycopg.connect(TEST_URL) as dst:
        found = [r[0] for r in src.execute("SELECT id FROM instrument WHERE id = ANY(%s) ORDER BY id",
                                           (list(instruments),))]
        if len(found) != len(instruments):
            pytest.skip("the seeded instruments are not in the source database")
        p: dict[str, Any] = {"ids": found, "terms": list(TERM_FIELDS), "site_fields": list(SITE_FIELDS),
                             "xbrl": [*DEBT_FIELDS, *SHARE_FIELDS]}
        p["slugs"] = [r[0] for r in src.execute(
            "SELECT DISTINCT site_slug FROM instrument WHERE id = ANY(%(ids)s) AND site_slug IS NOT NULL", p)]
        p["issuers"] = [r[0] for r in src.execute(
            """SELECT issuer_entity_id FROM instrument WHERE id = ANY(%(ids)s)
               UNION SELECT entity_id FROM chain_party WHERE site_slug = ANY(%(slugs)s) AND role = 'tenant'
                 AND entity_id IS NOT NULL""", p)]
        p["all"] = [r[0] for r in src.execute(
            """SELECT id FROM instrument WHERE id = ANY(%(ids)s)
               UNION SELECT id FROM instrument WHERE kind = 'bond' AND issuer_entity_id = ANY(%(issuers)s)""", p)]
        _copy(src, dst, "entity", "true", p)
        _copy(src, dst, "site", "true", p)
        _copy(src, dst, "instrument", "id = ANY(%(all)s)", p)
        _copy(src, dst, "chain_party", "site_slug = ANY(%(slugs)s)", p)
        _copy(src, dst, "fact", """status IN ('published', 'superseded') AND (
                 (subject_type = 'instrument' AND subject_id = ANY(%(all)s) AND field = ANY(%(terms)s))
              OR (subject_type = 'series' AND (subject_id LIKE 'ust_par_%%' OR subject_id = 'sofr'
                  OR (subject_id LIKE 'equity:%%' AND field = 'close') OR subject_id LIKE 'issue_comp:%%'
                  OR subject_id LIKE 'gpu_residual:%%'
                  OR subject_id LIKE 'ice_bofa_%%' OR subject_id LIKE 'etf_oas_%%'))
              OR (subject_type = 'entity' AND subject_id = ANY(%(issuers)s) AND field = ANY(%(xbrl)s))
              OR (subject_type = 'site' AND subject_id = ANY(%(slugs)s) AND field = ANY(%(site_fields)s)))""",
              p, nulls=("document_id",))
        _copy(src, dst, "price_obs", "instrument_id = ANY(%(all)s) AND kind <> 'marden'", p, nulls=("document_id",))
        _copy(src, dst, "event", "subject_type = 'site' AND subject_id = ANY(%(slugs)s)", p,
              nulls=("document_id", "fact_id"))
        _copy(src, dst, "model_output", "model = 'cushion' AND subject_type = 'site' AND subject_id = ANY(%(slugs)s)",
              p, nulls=("run_id",))
        for table in ("fact", "event", "price_obs", "model_output"):
            dst.execute(sql.SQL("SELECT setval(pg_get_serial_sequence({t}, 'id'), coalesce(max(id), 0) + 1, false) "
                                "FROM {i}").format(t=sql.Literal(table), i=sql.Identifier(table)))
        dst.execute("UPDATE instrument SET in_v1 = (id = ANY(%s))", (found,))
        dst.execute("UPDATE site SET in_v1 = (slug = ANY(%s))", (p["slugs"],))
    return found
