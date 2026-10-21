"""Copy the real rows behind the payer and obligation views from the local `marden` database into the test
database.

The payer, payer capacity, obligation map and lease map builders read entities, sites, chain parties,
instruments, obligation edges, the facts on sites, instruments, entities and obligations, fund holdings,
site links (with their campuses) and model outputs. Everything is copied as the loaders, collectors and
models stored it (no hand-made samples); references to raw documents, model runs and evidence facts are
cleared because the raw documents are not copied. Skips when the source database has no obligations.
Uses real_rows' COPY helpers.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from conftest import BASE_URL, TEST_URL
from real_rows import _copy, _reset_sequence

FACT_SUBJECTS = ["site", "instrument", "entity", "obligation"]
CAMPUSES = "id IN (SELECT campus_id FROM site_link UNION SELECT campus_id FROM obligation WHERE campus_id IS NOT NULL)"


def copy_payer_area() -> list[str]:
    """Copy every obligation edge and what the payer-area documents read. Returns the obligation ids."""
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
        p: dict[str, Any] = {"subjects": FACT_SUBJECTS}
        _copy(src, dst, "entity", "true", p, nulls=("parent_id",))
        for eid, parent in src.execute("SELECT id, parent_id FROM entity WHERE parent_id IS NOT NULL"):
            dst.execute("UPDATE entity SET parent_id = %s WHERE id = %s", (parent, eid))
        _copy(src, dst, "campus", CAMPUSES, p)
        for table in ("site", "instrument", "chain_party", "obligation"):
            _copy(src, dst, table, "true", p)
        _copy(src, dst, "site_link", "true", p, nulls=("evidence_fact_id",))
        _copy(src, dst, "fact", "subject_type = ANY(%(subjects)s)", p, nulls=("document_id",))
        _copy(src, dst, "holding", "true", p, nulls=("document_id",))
        _copy(src, dst, "model_output", "true", p, nulls=("run_id",))
        for table in ("fact", "holding", "model_output"):
            _reset_sequence(dst, table)
        dst.commit()
    return ids
