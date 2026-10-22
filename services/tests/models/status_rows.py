"""Copy the real rows the status model reads for a few sites from the local `marden` database into the test
database (what the loaders, collectors and the review queue stored, not hand-made samples).

Copied: every entity (parents restored); the sites, their chain parties, every site fact and event; the
chain-party entities' funding facts and events; the site links and, for approved ones, the campuses, the
plants serving them (plus any extra `plants`), their facts and events. Document and fact references of
events are cleared (the raw documents are not copied), so the archive's publication-date rule does not apply
(a fact is public on its `valid_from`). Skips when the source database or the sites are not there.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg
import pytest
from conftest import BASE_URL, TEST_URL
from model_rows import _copy, _reset_sequence


def copy_status_rows(slugs: Sequence[str], plants: Sequence[str] = ()) -> list[str]:
    try:
        src = psycopg.connect(BASE_URL, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"source database not reachable: {exc}")
    with src, psycopg.connect(TEST_URL) as dst:
        found = [r[0] for r in src.execute("SELECT slug FROM site WHERE slug = ANY(%s) ORDER BY slug", (list(slugs),))]
        if len(found) != len(slugs):
            pytest.skip("the sites are not in the source database")
        p: dict[str, Any] = {"sites": found}
        p["campuses"] = [r[0] for r in src.execute(
            "SELECT DISTINCT campus_id FROM site_link WHERE site_slug = ANY(%(sites)s) AND status = 'approved'", p)]
        p["plants"] = sorted({r[0] for r in src.execute(
            "SELECT plant_id FROM plant_serves WHERE campus_id = ANY(%(campuses)s)", p)} | set(plants))
        p["entities"] = [r[0] for r in src.execute(
            "SELECT DISTINCT entity_id FROM chain_party WHERE site_slug = ANY(%(sites)s) AND entity_id IS NOT NULL", p)]
        if not src.execute("SELECT 1 FROM power_plant WHERE id = ANY(%(plants)s)", p).fetchall() and p["plants"]:
            pytest.skip("the plants are not in the source database")
        _copy(src, dst, "entity", "true", p, nulls=("parent_id",))
        for eid, parent in src.execute("SELECT id, parent_id FROM entity WHERE parent_id IS NOT NULL"):
            dst.execute("UPDATE entity SET parent_id = %s WHERE id = %s", (parent, eid))
        _copy(src, dst, "site", "slug = ANY(%(sites)s)", p)
        _copy(src, dst, "chain_party", "site_slug = ANY(%(sites)s)", p)
        _copy(src, dst, "campus", "id = ANY(%(campuses)s)", p)
        _copy(src, dst, "power_plant", "id = ANY(%(plants)s)", p)
        _copy(src, dst, "site_link", "site_slug = ANY(%(sites)s) AND campus_id = ANY(%(campuses)s)", p,
              nulls=("evidence_fact_id",))
        _copy(src, dst, "plant_serves", "campus_id = ANY(%(campuses)s) AND plant_id = ANY(%(plants)s)", p,
              nulls=("evidence_fact_id",))
        _copy(src, dst, "fact", """(subject_type = 'site' AND subject_id = ANY(%(sites)s))
                 OR (subject_type = 'plant' AND subject_id = ANY(%(plants)s))
                 OR (subject_type = 'entity' AND subject_id = ANY(%(entities)s) AND field = 'funding_fact')""",
              p, nulls=("document_id",))
        _copy(src, dst, "event", """(subject_type = 'site' AND subject_id = ANY(%(sites)s))
                 OR (subject_type = 'campus' AND subject_id = ANY(%(campuses)s))
                 OR (subject_type = 'plant' AND subject_id = ANY(%(plants)s))
                 OR (subject_type = 'entity' AND subject_id = ANY(%(entities)s))""", p,
              nulls=("document_id", "fact_id"))
        for table in ("fact", "event"):
            _reset_sequence(dst, table)
        dst.commit()
    return found
