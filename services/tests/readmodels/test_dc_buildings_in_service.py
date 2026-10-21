"""`dc_tab:buildings` (the Data centers table's expanded row and the campus Buildings tab): each building
carries `inService`, the date its record says it came into service, apart from `firstVisible` (first
seen in imagery). Real seeded rows copied from the local `marden` database (facility_rows); the
expectation is read from the copied facts with SQL, not from the builder.
"""

from __future__ import annotations

from facility_rows import copy_facilities
from real_rows import copied_rows

from marden.core import db
from marden.readmodels import business_today
from marden.readmodels.context import building
from marden.readmodels.registry import get_builder
from marden.readmodels.views import facilities_campus_tabs  # noqa: F401 (registers builders)

ABILENE = "stargate-abilene"


def build_buildings(campus_id: str) -> list[dict]:
    b = get_builder("dc_tab:buildings")
    assert b is not None
    with building(business_today()), db.transaction() as conn:
        return b.build_one(conn, campus_id)


def facts(field: str, campus_id: str) -> dict[str, dict]:
    rows = copied_rows(
        """SELECT f.* FROM fact_current f JOIN building b ON f.subject_type = 'building' AND f.subject_id = b.id
            WHERE b.campus_id = %s AND f.field = %s""",
        (campus_id, field),
    )
    return {r["subject_id"]: r for r in rows}


def test_in_service_is_the_recorded_construction_end_and_first_visible_the_start():
    campuses = copy_facilities()
    assert ABILENE in campuses
    docs = build_buildings(ABILENE)
    assert docs, "Stargate Abilene has building records with footprints"
    ends, starts = facts("construction_end", ABILENE), facts("construction_start", ABILENE)
    assert ends, "the seed holds in-service dates for Abilene's first buildings"
    for doc in docs:
        end, start = ends.get(doc["id"]), starts.get(doc["id"])
        if end is None:
            assert doc["inService"]["value"] is None  # n/d: no record dates it
            assert "sourceUrl" not in doc["inService"]
        else:
            assert doc["inService"]["value"] == end["value"][:10]
            assert doc["inService"]["tag"] == end["tag"]
            assert doc["inService"]["sourceName"] == end["source_name"]
            assert doc["inService"]["asOf"] == end["valid_from"].isoformat()
        assert doc["firstVisible"] == (start["value"] if start else None)


def test_a_building_under_construction_has_no_in_service_date():
    copy_facilities()
    docs = {d["id"]: d for d in build_buildings(ABILENE)}
    building_rows = copied_rows(
        """SELECT b.id FROM building b JOIN fact_current s ON s.subject_type = 'building' AND s.subject_id = b.id
             AND s.field = 'status' AND s.value = '"Under construction"' WHERE b.campus_id = %s""",
        (ABILENE,),
    )
    assert building_rows
    for r in building_rows:
        doc = docs[r["id"]]
        assert doc["status"]["value"] == "Under construction"
        assert doc["inService"]["value"] is None
        assert doc["firstVisible"] is not None  # visible in imagery, not yet in service
