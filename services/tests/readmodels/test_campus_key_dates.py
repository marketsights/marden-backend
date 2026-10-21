"""`dc_detail` key dates (spec 14 Overview, screens/14c-campus-overview.html "Key dates"): besides first
seen, construction start, first building complete and online, the date the first building went into
service, from the campus's dated count of operational buildings (`buildings_operational`, Epoch AI's
imagery series: 0 -> 1 on the date the first hall is live). Real seeded rows copied from the local
`marden` database (facility_rows); the expectation is read from the copied facts with SQL, not from
the builder.
"""

from __future__ import annotations

from facility_rows import copy_facilities
from real_rows import copied_rows

from marden.core import db
from marden.readmodels import business_today
from marden.readmodels.context import building
from marden.readmodels.registry import get_builder
from marden.readmodels.views import facilities_campus  # noqa: F401 (registers builders)

ABILENE, FRONTIER = "stargate-abilene", "vantage-frontier"
LABEL = "First building operational"


def detail(campus_id: str) -> dict:
    b = get_builder("dc_detail")
    assert b is not None
    with building(business_today()), db.transaction() as conn:
        return b.build_one(conn, campus_id)


def first_live(campus_id: str) -> dict | None:
    """The earliest published count of operational buildings above zero, by SQL."""
    rows = copied_rows(
        """SELECT * FROM fact WHERE subject_type = 'campus' AND subject_id = %s AND field = 'buildings_operational'
              AND status = 'published' AND (valid_to IS NULL OR valid_to > CURRENT_DATE)
              AND valid_from <= CURRENT_DATE AND jsonb_typeof(value) = 'number' AND (value #>> '{}')::numeric > 0
            ORDER BY valid_from, id LIMIT 1""",
        (campus_id,),
    )
    return rows[0] if rows else None


def test_first_building_operational_is_the_first_nonzero_count_of_live_buildings():
    campuses = copy_facilities()
    assert ABILENE in campuses
    row = first_live(ABILENE)
    assert row is not None, "the seed holds Epoch AI's operational-building series for Abilene"
    dates = {d["label"]: d["date"] for d in detail(ABILENE)["keyDates"]}
    got = dates[LABEL]
    assert got["value"] == row["valid_from"].isoformat()
    want = (row["tag"], row["source_name"], row["valid_from"].isoformat())
    assert (got["tag"], got["sourceName"], got["asOf"]) == want
    assert got.get("sourceUrl") == row["source_url"]


def test_no_live_building_means_no_operational_date():
    campuses = copy_facilities()
    assert FRONTIER in campuses
    assert first_live(FRONTIER) is None, "Frontier has no operational building on record yet"
    assert LABEL not in {d["label"] for d in detail(FRONTIER)["keyDates"]}
