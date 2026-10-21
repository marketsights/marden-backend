"""Copy the real rows behind the Data centers and On-site power views from the local `marden` database.

The facility builders read campuses, buildings, plants, units, plant_serves, site links, sites and
their instruments and chain parties, entities (plant operators, permit dockets, power buyers),
obligations, facts on all of those, campus and plant events, and model outputs (site status, capacity).
Everything is copied as the loaders, collectors and models stored it (not hand-made samples);
references to raw documents and model runs are cleared (the raw documents are not copied); evidence
fact ids are kept, since the evidence facts are copied too. Skips when the source database has no
campuses. Uses real_rows' COPY helpers.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from conftest import BASE_URL, TEST_URL
from real_rows import _copy, _reset_sequence

FACT_SUBJECTS = ["campus", "building", "plant", "unit", "obligation", "site"]
LINKED_SITES = "slug IN (SELECT site_slug FROM site_link) OR slug IN (SELECT site_slug FROM obligation)"


def copy_facilities() -> list[str]:
    """Copy every campus and plant and what their documents read. Returns the campus ids."""
    try:
        src = psycopg.connect(BASE_URL, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"source database not reachable: {exc}")
    with src, psycopg.connect(TEST_URL) as dst:
        ids = [r[0] for r in src.execute("SELECT id FROM campus ORDER BY id")]
        if not ids:
            pytest.skip("the seeded campuses are not in the source database")
        p: dict[str, Any] = {"subjects": FACT_SUBJECTS}
        p["sites"] = [r[0] for r in src.execute(f"SELECT slug FROM site WHERE {LINKED_SITES}")]
        p["instruments"] = [r[0] for r in src.execute("SELECT id FROM instrument WHERE site_slug = ANY(%(sites)s)", p)]
        _copy(src, dst, "entity", "true", p, nulls=("parent_id",))
        for eid, parent in src.execute("SELECT id, parent_id FROM entity WHERE parent_id IS NOT NULL"):
            dst.execute("UPDATE entity SET parent_id = %s WHERE id = %s", (parent, eid))
        _copy(src, dst, "site", "slug = ANY(%(sites)s)", p)
        _copy(src, dst, "chain_party", "site_slug = ANY(%(sites)s)", p)
        _copy(src, dst, "instrument", "id = ANY(%(instruments)s)", p)
        for table in ("campus", "building", "power_plant", "power_unit", "obligation"):
            _copy(src, dst, table, "true", p)
        _copy(
            src,
            dst,
            "fact",
            """(subject_type = ANY(%(subjects)s) AND (subject_type <> 'site' OR subject_id = ANY(%(sites)s)))
               OR (subject_type = 'instrument' AND subject_id = ANY(%(instruments)s))
               OR (subject_type = 'entity' AND subject_id IN (SELECT id FROM entity WHERE meta ? 'proceeding_key'))""",
            p,
            nulls=("document_id",),
        )
        _copy(src, dst, "site_link", "true", p)
        _copy(src, dst, "plant_serves", "true", p)
        _copy(src, dst, "event", "subject_type IN ('campus', 'plant', 'building')", p, nulls=("document_id", "fact_id"))
        _copy(
            src,
            dst,
            "model_output",
            """(subject_type = 'site' AND subject_id = ANY(%(sites)s))
                 OR model = 'capacity' OR subject_type IN ('campus', 'building', 'plant')""",
            p,
            nulls=("run_id",),
        )
        for table in ("fact", "event", "model_output"):
            _reset_sequence(dst, table)
        dst.commit()
    return ids
