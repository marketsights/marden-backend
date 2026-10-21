"""Copy the real rows behind a few instruments from the local `marden` database into the test database.

The builder tests run on what the loaders and collectors actually stored, not on hand-made samples:
instrument identity rows, their issuers (with parents), sites, instrument facts, prices, fund holdings
(plus each holding fund's newest filing line, which tells the builder whether the fund still holds the
instrument) and any model outputs for those subjects. Document references are cleared (document_id,
run_id), because the raw documents are not copied. Skips when the source database or the instruments
are not there.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg
import pytest
from conftest import BASE_URL, TEST_URL
from psycopg import sql

FUND_FILINGS = ["N-PORT", "10-Q", "10-K"]


def _columns(conn: psycopg.Connection, table: str) -> list[str]:
    rows = conn.execute(
        """SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position""",
        (table,),
    ).fetchall()
    return [r[0] for r in rows]


def _copy(
    src: psycopg.Connection,
    dst: psycopg.Connection,
    table: str,
    where: str,
    params: dict[str, Any],
    *,
    nulls: Sequence[str] = (),
) -> int:
    """COPY the matching rows of `table` (the test database's columns; `nulls` copied as NULL)."""
    have = set(_columns(src, table))
    cols = [c for c in _columns(dst, table) if c in have]
    select = sql.SQL(", ").join(sql.SQL("NULL") if c in nulls else sql.Identifier(c) for c in cols)
    query = sql.SQL("COPY (SELECT {} FROM {} WHERE {}) TO STDOUT").format(select, sql.Identifier(table), sql.SQL(where))
    target = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(table), sql.SQL(", ").join(sql.Identifier(c) for c in cols)
    )
    n = 0
    with src.cursor().copy(query, params) as out, dst.cursor().copy(target) as inp:
        for chunk in out:
            inp.write(chunk)
            n += 1
    return n


def _reset_sequence(dst: psycopg.Connection, table: str) -> None:
    dst.execute(
        sql.SQL("SELECT setval(pg_get_serial_sequence({t}, 'id'), coalesce(max(id), 0) + 1, false) FROM {i}").format(
            t=sql.Literal(table), i=sql.Identifier(table)
        )
    )


def copy_instruments(ids: Sequence[str]) -> list[str]:
    """Copy the instruments (those present in the source) and everything their documents read."""
    try:
        src = psycopg.connect(BASE_URL, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"source database not reachable: {exc}")
    # One snapshot for every read: loaders and collectors write the source while it is copied, and rows
    # written between two COPYs (a holding whose fund was added meanwhile) would break the foreign keys.
    src.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    with src, psycopg.connect(TEST_URL) as dst:
        found = [r[0] for r in src.execute("SELECT id FROM instrument WHERE id = ANY(%s) ORDER BY id", (list(ids),))]
        if not found:
            pytest.skip("the seeded instruments are not in the source database")
        p: dict[str, Any] = {"ids": found, "filings": FUND_FILINGS}
        entities = [
            r[0]
            for r in src.execute(
                """WITH RECURSIVE seed AS (
                 SELECT issuer_entity_id AS id FROM instrument WHERE id = ANY(%(ids)s) AND issuer_entity_id IS NOT NULL
                 UNION SELECT fund_entity_id FROM holding WHERE instrument_id = ANY(%(ids)s)),
               up AS (SELECT e.id, e.parent_id FROM entity e JOIN seed s ON s.id = e.id
                      UNION SELECT q.id, q.parent_id FROM entity q JOIN up ON up.parent_id = q.id)
               SELECT DISTINCT id FROM up""",
                p,
            )
        ]
        p["entities"] = entities
        # The instruments' own sites, and the sites their issuers are chain parties of (an unsited
        # note's chain status is its issuer's link there).
        p["sites"] = [
            r[0]
            for r in src.execute(
                """SELECT site_slug FROM instrument WHERE id = ANY(%(ids)s) AND site_slug IS NOT NULL
                   UNION SELECT site_slug FROM chain_party WHERE entity_id = ANY(%(entities)s)""",
                p,
            )
        ]
        _copy(src, dst, "entity", "id = ANY(%(entities)s)", p, nulls=("parent_id",))
        for eid, parent in src.execute("SELECT id, parent_id FROM entity WHERE id = ANY(%(entities)s)", p):
            if parent:
                dst.execute("UPDATE entity SET parent_id = %s WHERE id = %s", (parent, eid))
        _copy(src, dst, "site", "slug = ANY(%(sites)s)", p)
        _copy(src, dst, "instrument", "id = ANY(%(ids)s)", p)
        _copy(src, dst, "chain_party", "entity_id = ANY(%(entities)s) AND site_slug = ANY(%(sites)s)", p)
        _copy(src, dst, "fact", "subject_type = 'instrument' AND subject_id = ANY(%(ids)s)", p, nulls=("document_id",))
        _copy(src, dst, "price_obs", "instrument_id = ANY(%(ids)s)", p, nulls=("document_id",))
        _copy(src, dst, "holding", "instrument_id = ANY(%(ids)s)", p, nulls=("document_id",))
        _copy(
            src,
            dst,
            "holding",
            """id IN (SELECT DISTINCT ON (fund_entity_id) id FROM holding
                         WHERE fund_entity_id = ANY(%(entities)s) AND filing_type = ANY(%(filings)s)
                         ORDER BY fund_entity_id, period_end DESC, id DESC)
                 AND (instrument_id IS NULL OR NOT instrument_id = ANY(%(ids)s))""",
            p,
            nulls=("document_id", "instrument_id"),
        )
        _copy(
            src,
            dst,
            "model_output",
            """(subject_type = 'instrument' AND subject_id = ANY(%(ids)s))
                 OR (subject_type = 'site' AND subject_id = ANY(%(sites)s))
                 OR (subject_id = 'all' AND model IN ('curve', 'gpu_deployment'))""",
            p,
            nulls=("run_id",),
        )
        for table in ("fact", "price_obs", "holding", "model_output"):
            _reset_sequence(dst, table)
        dst.commit()
    return found


def copied_rows(query: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[dict[str, Any]]:
    """Rows from the test database (after copying), for expectations computed independently."""
    from psycopg.rows import dict_row

    with psycopg.connect(TEST_URL, row_factory=dict_row) as conn:
        return conn.execute(query, params).fetchall()
