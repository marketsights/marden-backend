"""Copy the real rows the cushion model reads from the local `marden` database into the test database.

What the loaders and collectors actually stored: every site identity row, the schedule / power / lease
facts and catalyst, permit and opposition events of the chosen sites, every site's dated plan revisions
(the slip history), the plants serving the chosen sites with their facts, and the EIA-860M generator rows.
Document references are cleared (the raw documents are not copied). Skips when the source database or
the sites are not there.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg
import pytest
from conftest import BASE_URL, TEST_URL
from psycopg import sql

from marden.models.cushion_inputs import EVENT_KINDS, PLANT_FIELDS, SITE_FIELDS

HISTORY_FIELDS = ["rfs_statement", "original_energization", "expected_energization"]


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


def copy_sites(slugs: Sequence[str]) -> list[str]:
    try:
        src = psycopg.connect(BASE_URL, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"source database not reachable: {exc}")
    with src, psycopg.connect(TEST_URL) as dst:
        found = [r[0] for r in src.execute("SELECT slug FROM site WHERE slug = ANY(%s) ORDER BY slug", (list(slugs),))]
        if len(found) != len(slugs):
            pytest.skip("the seeded sites are not in the source database")
        p: dict[str, Any] = {"slugs": found, "site_fields": list(SITE_FIELDS), "history": HISTORY_FIELDS,
                             "plant_fields": list(PLANT_FIELDS), "kinds": list(EVENT_KINDS)}
        p["plants"] = [r[0] for r in src.execute(
            """SELECT DISTINCT f.subject_id FROM fact f, jsonb_array_elements_text(
                      CASE WHEN jsonb_typeof(f.value) = 'array' THEN f.value ELSE '[]' END) s(slug)
                WHERE f.subject_type = 'plant' AND f.field = 'serves_sites' AND f.status = 'published'
                  AND s.slug = ANY(%(slugs)s)""", p)]
        _copy(src, dst, "site", "true", p)
        _copy(src, dst, "power_plant", "id = ANY(%(plants)s)", p)
        _copy(src, dst, "fact", """status IN ('published', 'superseded') AND (
                 (subject_type = 'site' AND subject_id = ANY(%(slugs)s) AND field = ANY(%(site_fields)s))
              OR (subject_type = 'site' AND NOT subject_id = ANY(%(slugs)s) AND field = ANY(%(history)s))
              OR (subject_type = 'plant' AND subject_id = ANY(%(plants)s) AND field = ANY(%(plant_fields)s))
              OR subject_type = 'eia_generator')""", p, nulls=("document_id",))
        _copy(src, dst, "event", "subject_type = 'site' AND subject_id = ANY(%(slugs)s) AND kind = ANY(%(kinds)s)",
              p, nulls=("document_id", "fact_id"))
        for table in ("fact", "event"):
            dst.execute(sql.SQL("SELECT setval(pg_get_serial_sequence({t}, 'id'), coalesce(max(id), 0) + 1, false) "
                                "FROM {i}").format(t=sql.Literal(table), i=sql.Identifier(table)))
        dst.execute("UPDATE site SET in_v1 = (slug = ANY(%s))", (found,))
    return found
