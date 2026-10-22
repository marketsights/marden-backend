"""A plant carries every financed site of the campuses it serves: `financedSites` (types.ts PowerPlantRow), most
debt first, with `financed` the first of them. The list comes from the same helper as the campus row
(facilities_common.financed_sites), so a plant and the campus it serves show the same sites, debts and chain
statuses.

On the real rows (facility_rows.copy_facilities) no plant serves Galaxy Helios or Polaris Forge 1 yet, the two
campuses that finance two sites each (Helios Phase I and II; Polaris Forge 1 and its building 4). The first test
links a real plant that serves no financed campus to each of them in the test database (one plant_serves row with
no evidence fact) and compares the plant's list with the campus's. The single-site and no-site cases are the
plants as recorded.
"""

from __future__ import annotations

import pytest
from facility_rows import copy_facilities
from real_rows import copied_rows

from marden.core import db
from marden.readmodels import business_today
from marden.readmodels.context import building
from marden.readmodels.registry import get_builder
from marden.readmodels.views import facilities_campus, facilities_plants  # noqa: F401 (registers builders)

CAMPUSES = {"galaxy-helios": {"helios-1", "helios-2"},
            "apld-polaris-forge-1": {"polaris-forge-1", "polaris-forge-1-building-4"}}


def _build(view: str, key: str):
    with building(business_today()), db.transaction() as conn:
        return get_builder(view).build_one(conn, key)


def _approved(campuses: list[str]) -> set[str]:
    return {r["site_slug"] for r in copied_rows(
        "SELECT site_slug FROM site_link WHERE campus_id = ANY(%s) AND status = 'approved'", (campuses,))}


def _served(plant: str) -> list[str]:
    return [r["campus_id"] for r in copied_rows("SELECT campus_id FROM plant_serves WHERE plant_id = %s", (plant,))]


def _plants() -> list[str]:
    return [r["id"] for r in copied_rows("SELECT id FROM power_plant ORDER BY id")]


@pytest.mark.parametrize("campus", sorted(CAMPUSES))
def test_a_plant_serving_a_two_site_campus_lists_both_sites(campus):
    copy_facilities()
    if _approved([campus]) != CAMPUSES[campus]:
        pytest.skip(f"{campus}'s two site links are not approved in the source database")
    plant = next((p for p in _plants() if not _approved(_served(p))), None)
    if plant is None:
        pytest.skip("every plant serves a financed campus")
    before = _build("plant_row", plant)
    assert before["financedSites"] == [] and before["financed"] is None
    with db.transaction() as conn:
        conn.execute("INSERT INTO plant_serves (plant_id, campus_id) VALUES (%s, %s)", (plant, campus))
    sites = _build("dc_row", campus)["financedSites"]
    row = _build("plant_row", plant)
    assert {s["siteSlug"] for s in row["financedSites"]} == CAMPUSES[campus]
    assert row["financedSites"] == sites and row["financed"] == sites[0]  # the campus's list; primary first
    debts = [s["debtB"]["value"] for s in sites]
    assert debts == sorted(debts, reverse=True)  # most debt first
    detail = _build("plant_detail", plant)  # PowerPlantDetail extends PowerPlantRow
    assert detail["financedSites"] == sites and detail["financed"] == sites[0]


def test_a_plant_with_one_or_no_financed_site():
    copy_facilities()
    plants = _plants()
    one = [p for p in plants if len(_approved(_served(p))) == 1]
    none = [p for p in plants if not _approved(_served(p))]
    if not one or not none:
        pytest.skip("no plant serving exactly one (or no) financed site")
    for plant in one:
        row = _build("plant_row", plant)
        assert [s["siteSlug"] for s in row["financedSites"]] == sorted(_approved(_served(plant))), plant
        assert row["financed"] == row["financedSites"][0]
        campus = next(c for c in _served(plant) if _approved([c]))
        assert row["financed"] == _build("dc_row", campus)["financed"]  # as on the campus it serves
    row = _build("plant_row", none[0])
    assert row["financedSites"] == [] and row["financed"] is None
