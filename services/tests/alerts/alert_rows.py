"""Copy the real rows behind a few sites from the local `marden` database into the test database, and make
test firms (orgs, members, product access, watchlists) around them.

Copied for each site: the site, its chain parties, the instruments that finance it, their issuers and the
chain-party entities (with parents), the site's published events (document and fact references cleared,
the raw documents are not copied), the status and cushion outputs of the site, the price and mark-gap
outputs and rating facts of its instruments (run references cleared). Skips when the source database or the
sites are not there. Firms are app rows a test makes up (they are who watches, not market data).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg
import pytest
from conftest import BASE_URL, TEST_URL
from psycopg import sql
from psycopg.rows import dict_row

from marden.core.jsonutil import jsonb


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


def _reset_sequence(dst: psycopg.Connection, table: str) -> None:
    dst.execute(sql.SQL("SELECT setval(pg_get_serial_sequence({t}, 'id'), coalesce(max(id), 0) + 1, false) FROM {i}")
                .format(t=sql.Literal(table), i=sql.Identifier(table)))


def copy_sites(slugs: Sequence[str]) -> dict[str, list[str]]:
    """Copy the sites (those present in the source). Returns {'sites': [...], 'instruments': [...]}."""
    try:
        src = psycopg.connect(BASE_URL, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"source database not reachable: {exc}")
    with src, psycopg.connect(TEST_URL) as dst:
        sites = [r[0] for r in src.execute("SELECT slug FROM site WHERE slug = ANY(%s) ORDER BY slug", (list(slugs),))]
        if not sites:
            pytest.skip("the sites are not in the source database")
        p: dict[str, Any] = {"sites": sites}
        p["instruments"] = [r[0] for r in src.execute(
            "SELECT id FROM instrument WHERE site_slug = ANY(%(sites)s) ORDER BY id", p)]
        p["entities"] = [r[0] for r in src.execute(
            """WITH RECURSIVE seed AS (
                 SELECT issuer_entity_id AS id FROM instrument WHERE id = ANY(%(instruments)s)
                 UNION SELECT entity_id FROM chain_party WHERE site_slug = ANY(%(sites)s)),
               up AS (SELECT e.id, e.parent_id FROM entity e JOIN seed s ON s.id = e.id
                      UNION SELECT q.id, q.parent_id FROM entity q JOIN up ON up.parent_id = q.id)
               SELECT DISTINCT id FROM up""", p)]
        _copy(src, dst, "entity", "id = ANY(%(entities)s)", p, nulls=("parent_id",))
        for eid, parent in src.execute("SELECT id, parent_id FROM entity WHERE id = ANY(%(entities)s)", p):
            if parent:
                dst.execute("UPDATE entity SET parent_id = %s WHERE id = %s", (parent, eid))
        _copy(src, dst, "site", "slug = ANY(%(sites)s)", p)
        _copy(src, dst, "chain_party", "site_slug = ANY(%(sites)s)", p)
        _copy(src, dst, "instrument", "id = ANY(%(instruments)s)", p)
        _copy(src, dst, "event", "subject_type = 'site' AND subject_id = ANY(%(sites)s) AND status = 'published'", p,
              nulls=("document_id", "fact_id"))
        _copy(src, dst, "fact", """subject_type = 'instrument' AND subject_id = ANY(%(instruments)s)
                                   AND field IN ('rating', 'rating_summary')""", p, nulls=("document_id",))
        _copy(src, dst, "model_output",
              """(subject_type = 'site' AND subject_id = ANY(%(sites)s) AND model IN ('status', 'cushion'))
                 OR (subject_type = 'instrument' AND subject_id = ANY(%(instruments)s)
                     AND model IN ('price', 'mark_gap'))""", p, nulls=("run_id",))
        for table in ("event", "fact", "model_output"):
            _reset_sequence(dst, table)
        dst.commit()
    return {"sites": sites, "instruments": p["instruments"]}


def make_firm(org_id: str, members: Sequence[tuple[str, dict[str, Any] | None, bool]],
              watch: Sequence[tuple[str, str]], *, product: bool = True) -> None:
    """An org with members (user id, stored alert settings or None for the defaults, has a product role),
    the 'marden' product when `product`, and watchlist items (kind, ref id)."""
    with psycopg.connect(TEST_URL, autocommit=True) as c:
        c.execute("INSERT INTO product (id, name) VALUES ('marden', 'Marden') ON CONFLICT DO NOTHING")
        c.execute("INSERT INTO org (id, name) VALUES (%s, %s)", (org_id, f"Test firm {org_id}"))
        if product:
            c.execute("""INSERT INTO org_product (org_id, product_id, plan, status, starts_on)
                         VALUES (%s, 'marden', 'paid', 'active', current_date - 30)""", (org_id,))
        for user_id, settings, has_role in members:
            c.execute("INSERT INTO app_user (id, email, name) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                      (user_id, f"{user_id}@example.test", user_id))
            c.execute("INSERT INTO membership (org_id, user_id, role) VALUES (%s, %s, 'member')", (org_id, user_id))
            if product and has_role:
                c.execute("""INSERT INTO member_product_role (org_id, user_id, product_id, role)
                             VALUES (%s, %s, 'marden', 'member')""", (org_id, user_id))
            if settings is not None:
                c.execute("INSERT INTO alert_settings (org_id, user_id, settings) VALUES (%s, %s, %s)",
                          (org_id, user_id, jsonb(settings)))
        for kind, ref in watch:
            c.execute("INSERT INTO watchlist_item (org_id, kind, ref_id) VALUES (%s, %s, %s)", (org_id, kind, ref))


def rows(query: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[dict[str, Any]]:
    with psycopg.connect(TEST_URL, row_factory=dict_row) as conn:
        return conn.execute(query, params).fetchall()
