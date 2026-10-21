"""Copy the real rows the obligation, contagion, payer-capacity, lease-map, capacity, stress and risk-list
models read from the local `marden` database into the test database (what the loaders, collectors and the
upstream models stored, never hand-made samples).

Copied in one read-only snapshot: every entity (parents restored), site, instrument, chain party,
obligation edge, campus, building (with its footprint), power plant, power unit, plant link and site
link; the facts on sites, instruments, entities, obligations, campuses, buildings, plants and units; the
series the price engine and the capacity model read (Treasury par curve, SOFR, equity closes, issue comps,
GPU models and residuals, PUE range, ICE BofA indexes when collected); price observations, fund holdings
and their mark facts, site events; and the stored outputs of the upstream models (price, cds, curve,
status, cushion, mark_gap, recovery, gpu_collateral). Document, run and evidence references are cleared
(the raw documents are not copied). Skips when the source database or its obligations are not there.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from conftest import BASE_URL, TEST_URL
from psycopg import sql

FACT_SUBJECTS = ["site", "instrument", "entity", "obligation", "campus", "building", "plant", "unit", "holding"]
SERIES = ["ust_par_%", "sofr", "issue_comp:%", "gpu_%", "data_center_pue", "ice_bofa_%"]
UPSTREAM = ["price", "cds", "curve", "status", "cushion", "mark_gap", "recovery", "gpu_collateral"]
IDENTITY = (
    "site",
    "instrument",
    "chain_party",
    "campus",
    "obligation",
    "building",
    "power_plant",
    "power_unit",
    "plant_serves",
    "site_link",
)


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
    nulls: tuple[str, ...] = (),
) -> None:
    have = set(_columns(src, table))
    cols = [c for c in _columns(dst, table) if c in have]
    select = sql.SQL(", ").join(sql.SQL("NULL") if c in nulls else sql.Identifier(c) for c in cols)
    query = sql.SQL("COPY (SELECT {} FROM {} WHERE {}) TO STDOUT").format(select, sql.Identifier(table), sql.SQL(where))
    target = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(table), sql.SQL(", ").join(sql.Identifier(c) for c in cols)
    )
    with src.cursor().copy(query, params) as out, dst.cursor().copy(target) as inp:
        for chunk in out:
            inp.write(chunk)


def copy_credit_area() -> list[str]:
    """Copy the area; returns the obligation ids."""
    try:
        src = psycopg.connect(BASE_URL, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"source database not reachable: {exc}")
    src.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ  # one snapshot while collectors keep writing
    src.read_only = True
    with src, psycopg.connect(TEST_URL) as dst:
        ids = [r[0] for r in src.execute("SELECT id FROM obligation ORDER BY id")]
        if not ids:
            pytest.skip("the seeded obligations are not in the source database")
        p: dict[str, Any] = {"subjects": FACT_SUBJECTS, "series": SERIES, "upstream": UPSTREAM}
        _copy(src, dst, "entity", "true", p, nulls=("parent_id",))
        for eid, parent in src.execute("SELECT id, parent_id FROM entity WHERE parent_id IS NOT NULL"):
            dst.execute("UPDATE entity SET parent_id = %s WHERE id = %s", (parent, eid))
        for table in IDENTITY:
            _copy(
                src,
                dst,
                table,
                "true",
                p,
                nulls=("evidence_fact_id",) if table in ("site_link", "plant_serves") else (),
            )
        _copy(src, dst, "fact", "subject_type = ANY(%(subjects)s)", p, nulls=("document_id",))
        _copy(
            src,
            dst,
            "fact",
            """subject_type = 'series' AND (subject_id LIKE ANY(%(series)s)
                 OR (subject_id LIKE 'equity:%%' AND field = 'close'))""",
            p,
            nulls=("document_id",),
        )
        _copy(src, dst, "price_obs", "true", p, nulls=("document_id",))
        _copy(src, dst, "holding", "true", p, nulls=("document_id",))
        _copy(src, dst, "event", "subject_type = 'site'", p, nulls=("document_id", "fact_id"))
        _copy(src, dst, "model_output", "model = ANY(%(upstream)s)", p, nulls=("run_id",))
        for table in ("fact", "event", "price_obs", "holding", "model_output"):
            dst.execute(
                sql.SQL(
                    "SELECT setval(pg_get_serial_sequence({t}, 'id'), coalesce(max(id), 0) + 1, false) FROM {i}"
                ).format(t=sql.Literal(table), i=sql.Identifier(table))
            )
        dst.commit()
    return ids


def rows(query: str, params: Any = ()) -> list[dict[str, Any]]:
    """Rows from the test database (after copying), for expectations computed independently."""
    from psycopg.rows import dict_row

    with psycopg.connect(TEST_URL, row_factory=dict_row) as conn:
        return conn.execute(query, params).fetchall()


def shape(doc: dict[str, Any], drop: tuple[str, ...] = ("missing", "basis", "request")) -> dict[str, Any]:
    """The types.ts piece of a model output (the bookkeeping keys every output carries removed)."""
    return {k: v for k, v in doc.items() if k not in drop}
