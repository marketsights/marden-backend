"""Copy the real rows the mark-gap, recovery and GPU models read from the local `marden` database into the
test database (what the loaders and collectors stored, not hand-made samples).

Copied: every entity (parents restored), site and instrument identity row; every site and instrument
fact; the entity facts the models read (fund net assets, GPU useful lives, XBRL CIP and PP&E net); the
series facts of the GPU market, the GPU-loan issue comps, the UST par curve and the PUE range; the fund
holding lines of the chosen instruments plus every line of their holder funds (which tells the model
whether a fund still holds an instrument) with their holding facts; price-model outputs and Marden daily
prices of the chosen instruments when the source has them. Document and run references are cleared (the
raw documents are not copied). Skips when the source database or the instruments are not there.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg
import pytest
from conftest import BASE_URL, TEST_URL
from psycopg import sql

ENTITY_FIELDS = ["net_assets_usd", "gpu_useful_life_years", "construction_in_progress_usd", "ppe_net_usd",
                 "public_date"]
SERIES_LIKE = ["gpu_%", "issue_comp:%", "ust_par_%", "data_center_pue"]
FILINGS = ["N-PORT", "10-Q", "10-K"]


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


def copy_model_rows(instrument_ids: Sequence[str]) -> list[str]:
    """Copy what the models read for these instruments (and the GPU market). Returns the ids found."""
    try:
        src = psycopg.connect(BASE_URL, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"source database not reachable: {exc}")
    with src, psycopg.connect(TEST_URL) as dst:
        found = [r[0] for r in src.execute("SELECT id FROM instrument WHERE id = ANY(%s) ORDER BY id",
                                           (list(instrument_ids),))]
        if len(found) != len(instrument_ids):
            pytest.skip("the seeded instruments are not in the source database")
        p: dict[str, Any] = {"ids": found, "efields": ENTITY_FIELDS, "like": SERIES_LIKE, "filings": FILINGS}
        p["funds"] = [r[0] for r in src.execute(
            "SELECT DISTINCT fund_entity_id FROM holding WHERE instrument_id = ANY(%(ids)s)", p)]
        _copy(src, dst, "entity", "true", p, nulls=("parent_id",))
        for eid, parent in src.execute("SELECT id, parent_id FROM entity WHERE parent_id IS NOT NULL"):
            dst.execute("UPDATE entity SET parent_id = %s WHERE id = %s", (parent, eid))
        _copy(src, dst, "site", "true", p)
        _copy(src, dst, "instrument", "true", p)
        _copy(src, dst, "fact", "subject_type IN ('site', 'instrument')", p, nulls=("document_id",))
        _copy(src, dst, "fact", "subject_type = 'entity' AND field = ANY(%(efields)s)", p, nulls=("document_id",))
        _copy(src, dst, "fact", "subject_type = 'series' AND subject_id LIKE ANY(%(like)s)", p, nulls=("document_id",))
        _copy(src, dst, "holding", "fund_entity_id = ANY(%(funds)s) AND filing_type = ANY(%(filings)s)", p,
              nulls=("document_id",))
        _copy(src, dst, "fact", """subject_type = 'holding' AND field IN ('cur_cd', 'mark_price') AND subject_id IN (
                 SELECT id::text FROM holding WHERE fund_entity_id = ANY(%(funds)s))""", p, nulls=("document_id",))
        _copy(src, dst, "price_obs", "instrument_id = ANY(%(ids)s) AND kind = 'marden'", p, nulls=("document_id",))
        _copy(src, dst, "model_output", "model = 'price' AND subject_type = 'instrument' AND subject_id = ANY(%(ids)s)",
              p, nulls=("run_id",))
        for table in ("fact", "holding", "price_obs", "model_output"):
            _reset_sequence(dst, table)
        dst.commit()
    return found


def rows(query: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[dict[str, Any]]:
    """Rows from the test database (after copying), for expectations computed independently."""
    from psycopg.rows import dict_row

    with psycopg.connect(TEST_URL, row_factory=dict_row) as conn:
        return conn.execute(query, params).fetchall()
