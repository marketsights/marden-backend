"""On-site power fidelity (spec 15, screens/15c-plant-detail.html) on real seeded rows: unit rows from the
generating-unit permits' equipment statements when a plant has no power_unit rows, the permit's holder,
facility, other ids and emission limits, and the units' fuel as stated.

Rows are copied from the local `marden` database (facility_rows.copy_facilities). Expectations are read
from the copied facts directly, independent of the builder.
"""

from __future__ import annotations

from typing import Any

import pytest
from facility_rows import copy_facilities
from facility_shapes import VIEW_TYPES, assert_shapes
from real_rows import copied_rows

from marden.core import db
from marden.readmodels import business_today
from marden.readmodels.context import building
from marden.readmodels.registry import get_builder
from marden.readmodels.views import facilities_plants  # noqa: F401 (registers builders)

ABILENE_PLANT, FRONTIER_PLANT, JUPITER_PLANT = "n-abilene-onsite", "n-frontier-shack", "n-jupiter-microgrid"


@pytest.fixture
def plants() -> list[str]:
    copy_facilities()
    return [r["id"] for r in copied_rows("SELECT id FROM power_plant ORDER BY id")]


def build(view: str, key: str) -> Any:
    b = get_builder(view)
    assert b is not None, view
    with building(business_today()), db.transaction() as conn:
        return b.build_one(conn, key)


def fact(subject_type: str, subject_id: str, field: str) -> dict | None:
    rows = copied_rows(
        "SELECT * FROM fact_current WHERE subject_type = %s AND subject_id = %s AND field = %s",
        (subject_type, subject_id, field),
    )
    return rows[0] if rows else None


def need(ids: list[str], *wanted: str) -> None:
    missing = [w for w in wanted if w not in ids]
    if missing:
        pytest.skip(f"not in the source database: {missing}")


def test_permit_statements_stand_in_for_missing_unit_rows(plants):
    need(plants, ABILENE_PLANT)
    if copied_rows("SELECT 1 FROM power_unit WHERE plant_id = %s", (ABILENE_PLANT,)):
        pytest.skip("the plant has power_unit rows; permit statements are not used")
    permits = (fact("plant", ABILENE_PLANT, "air_permits") or {}).get("value") or []
    # The generating-unit permits (not PBR registrations or Title V), live, and their statements
    # that give a count and a rating each.
    stated = [
        (p, s)
        for p in permits
        if p.get("type") not in ("PBR", "Title V") and p.get("status") not in ("withdrawn", "cancelled", "denied")
        for s in p.get("units") or []
        if isinstance(s.get("count"), int) and isinstance(s.get("rating_mw_each"), int | float)
    ]
    if not stated:
        pytest.skip("no permit statement gives a count and a rating")
    detail = build("plant_detail", ABILENE_PLANT)
    rows = detail["unitRows"]
    assert len(rows) == len({(p["number"], s["oem"], s["model"]) for p, s in stated})
    for unit in rows:
        p, s = next((p, s) for p, s in stated if p["number"] == unit["permitRef"] and s["model"] == unit["model"])
        assert unit["count"] == s["count"] and unit["ratingMwEach"]["value"] == s["rating_mw_each"]
        assert unit["totalMw"] == round(s["count"] * s["rating_mw_each"], 3)
        assert unit["ratingMwEach"]["tag"] == "F" and unit["ratingMwEach"]["sourceUrl"] == s["url"]
        assert unit["status"] == {"issued": "Permitted", "pending": "Permit pending"}[p["status"]]
    # Permitted MW = the sum of unit ratings, computed in code (spec 15), and the row says so too.
    total = round(sum(u["totalMw"] for u in rows), 3)
    assert detail["permittedMw"]["value"] == total and detail["permittedMw"]["tag"] == "F"
    row = build("plant_row", ABILENE_PLANT)
    assert row["units"]["count"] == sum(u["count"] for u in rows) and row["permittedMw"]["value"] == total
    assert {m["oem"] for m in row["units"]["byModel"]} == {u["oem"] for u in rows}
    # Load cover follows the permitted MW (permitted ÷ facility MW, tag M).
    served = detail["servedCampus"]
    if served and served["facilityMw"]["value"]:
        assert served["loadCoverPct"]["value"] == round(100 * total / served["facilityMw"]["value"], 1)


def test_unit_rows_from_power_units_are_not_mixed_with_permit_statements(plants):
    need(plants, FRONTIER_PLANT)
    units = copied_rows("SELECT id FROM power_unit WHERE plant_id = %s", (FRONTIER_PLANT,))
    detail = build("plant_detail", FRONTIER_PLANT)
    assert len(detail["unitRows"]) == len(units)
    fuels = copied_rows(
        """SELECT f.value #>> '{}' AS fuel, f.tag FROM power_unit u JOIN fact_current f
             ON f.subject_type = 'unit' AND f.subject_id = u.id AND f.field = 'fuel' WHERE u.plant_id = %s""",
        (FRONTIER_PLANT,),
    )
    if not fuels:
        assert detail["fuelType"]["value"] is None
        return
    for f in fuels:  # the fuel as stated, with the units it covers when not every unit states one
        assert f["fuel"] in detail["fuelType"]["value"]
    assert detail["fuelType"]["tag"] in {f["tag"] for f in fuels}
    if len(fuels) < len(units):
        assert "(" in detail["fuelType"]["value"]


def test_permit_card_names_holder_facility_ids_and_stated_limits(plants):
    need(plants, ABILENE_PLANT, JUPITER_PLANT)
    for pid in (ABILENE_PLANT, JUPITER_PLANT):
        permit = fact("plant", pid, "air_permit")["value"]
        card = build("plant_detail", pid)["permit"]
        assert card["holder"] == (permit.get("applicant") or None)
        facility = permit.get("facility")
        if isinstance(facility, dict):
            assert card["facility"] == {"id": facility["key"], "name": facility.get("name")}
        assert card["otherIds"] == [i for i in permit.get("ids") or [] if i != permit["number"]]
        limits = [lim for lim in permit.get("emission_limits") or [] if isinstance(lim.get("value"), int | float)]
        assert [(lim["pollutant"], lim["value"]) for lim in card["limits"]] == [
            (lim["pollutant"], lim["value"]) for lim in limits
        ]  # listed only when the permit states them (spec 15)
        others = [p["number"] for p in (fact("plant", pid, "air_permits") or {}).get("value") or []
                  if p.get("number") and p["number"] != permit["number"]]
        assert sorted(r["number"] for r in card["related"]) == sorted(others)  # the plant's other permits


def test_plant_docs_still_match_the_response_types(plants):
    need(plants, ABILENE_PLANT, FRONTIER_PLANT)
    docs = {view: [build(view, pid) for pid in (ABILENE_PLANT, FRONTIER_PLANT, JUPITER_PLANT) if pid in plants]
            for view in ("plant_row", "plant_detail")}
    assert_shapes([(VIEW_TYPES[view], d) for view, d in docs.items()])
