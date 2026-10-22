"""Copy the real rows behind the financed sites from the local `marden` database into the test database.

The site builders read sites, chain parties, entities, instruments, campuses, buildings, plants, units,
site links, facts on all of those, site, campus and plant events, prices and fund holdings of the site-linked
and V1 instruments, the `source` rows and model outputs. Everything is copied as stored by the loaders,
collectors and models (not hand-made samples); references to raw documents, model runs and evidence
facts are cleared (the raw documents are not copied). Skips when the source database has no sites.
Uses real_rows' COPY helpers.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from conftest import BASE_URL, TEST_URL
from real_rows import _copy, _reset_sequence

FACT_SUBJECTS = ["site", "campus", "building", "plant", "unit", "instrument", "entity"]
INSTRUMENTS = "instrument_id IN (SELECT id FROM instrument WHERE site_slug IS NOT NULL OR in_v1)"


def copy_sites() -> list[str]:
    """Copy every site and what its documents read. Returns the site slugs."""
    try:
        src = psycopg.connect(BASE_URL, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"source database not reachable: {exc}")
    # One snapshot for every read: loaders and collectors write the source while it is copied, and rows
    # written between two COPYs (a holding whose fund was added meanwhile) would break the foreign keys.
    src.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    with src, psycopg.connect(TEST_URL) as dst:
        slugs = [r[0] for r in src.execute("SELECT slug FROM site ORDER BY slug")]
        if not slugs:
            pytest.skip("the seeded sites are not in the source database")
        p: dict[str, Any] = {"subjects": FACT_SUBJECTS}
        _copy(src, dst, "source", "true", p)
        _copy(src, dst, "entity", "true", p, nulls=("parent_id",))
        for eid, parent in src.execute("SELECT id, parent_id FROM entity WHERE parent_id IS NOT NULL"):
            dst.execute("UPDATE entity SET parent_id = %s WHERE id = %s", (parent, eid))
        for table in ("site", "campus", "building", "power_plant", "power_unit", "instrument", "chain_party"):
            _copy(src, dst, table, "true", p)
        _copy(src, dst, "fact", "subject_type = ANY(%(subjects)s)", p, nulls=("document_id",))
        _copy(src, dst, "site_link", "true", p, nulls=("evidence_fact_id",))
        _copy(src, dst, "plant_serves", "true", p, nulls=("evidence_fact_id",))
        _copy(src, dst, "event", "subject_type IN ('site', 'campus', 'plant')", p, nulls=("document_id", "fact_id"))
        _copy(src, dst, "price_obs", INSTRUMENTS, p, nulls=("document_id",))
        _copy(src, dst, "holding", INSTRUMENTS, p, nulls=("document_id",))
        _copy(src, dst, "model_output", "subject_type IN ('site', 'instrument')", p, nulls=("run_id",))
        for table in ("fact", "event", "price_obs", "holding", "model_output"):
            _reset_sequence(dst, table)
        dst.commit()
    return slugs
