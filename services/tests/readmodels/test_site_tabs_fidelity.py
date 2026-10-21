"""Site detail header, Opposition and Power tabs as the reference screens show them (spec 04 / 16;
screens 04, 04c, 04d), on real seeded rows copied from the local `marden` database (site_rows).

- The header counts the linked campus's buildings as the Sites and Completion lists do (spec 16), in
  `buildings` and in the facts line.
- Material events are one per story: a headline syndicated by several outlets is listed once.
- Opposition: a press report's outlet is not the court or body, and no event states a case status, so
  both are n/d; the next hearing is a date with what is heard as its note.
- Power: the in-service history's Source is the power source the record is about, not its publisher.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from real_rows import copied_rows
from site_rows import copy_sites

from marden.core import db
from marden.readmodels import business_today
from marden.readmodels.context import building
from marden.readmodels.registry import get_builder
from marden.readmodels.views import sites as _sites  # noqa: F401 (registers builders)
from marden.readmodels.views import sites_detail, sites_power, sites_tabs  # noqa: F401
from marden.readmodels.views.sites_detail import _story_key, facts_line
from marden.readmodels.views.sites_power import SITE_SCHEDULE, power_source_of

ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@pytest.fixture
def sites() -> list[str]:
    return copy_sites()


def build(view: str, key: str = "all") -> Any:
    b = get_builder(view)
    assert b is not None, view
    with building(business_today()), db.transaction() as conn:
        if b.build_many is not None:
            return dict(b.build_many(conn))[key]
        return b.build_one(conn, key)


def test_header_counts_the_linked_campus_buildings_as_the_lists_do(sites):
    listed = {r["slug"]: r["buildings"] for r in build("sites")}
    for slug in listed:
        detail = build("site_detail", slug)
        assert detail["buildings"] == listed[slug], slug
        total = (detail["buildings"] or {}).get("value", {}).get("total") or 0
        named = re.search(r"(\d+) buildings?\b", detail["facts"])
        assert (int(named.group(1)) if named else 0) == total, (slug, detail["facts"])
    # Jupiter's campus has no building records yet; its own building_count fact (R) is the count.
    count = copied_rows("SELECT value FROM fact_current WHERE subject_type = 'campus' AND subject_id = 'project-jupiter' "
                        "AND field = 'building_count'")
    records = copied_rows("SELECT 1 FROM building WHERE campus_id = 'project-jupiter' LIMIT 1")
    if count and not records and "jupiter" in listed:
        assert build("site_detail", "jupiter")["buildings"]["value"]["total"] == count[0]["value"]


def test_facts_line_names_the_buildings_once_counted():
    summary = {"gw": {"value": "2.46 GW"}, "debtB": {"value": 18}}
    assert facts_line(summary, None) == "2.46 GW · $18.0B debt"
    assert facts_line(summary, {"value": {"built": 0, "total": 4}}) == "2.46 GW · $18.0B debt · 4 buildings"
    assert facts_line(summary, {"value": {"built": 1, "total": 1}}) == "2.46 GW · $18.0B debt · 1 building"


def test_material_events_are_one_per_story(sites):
    assert _story_key("Oracle’s ‘force majeure’ warning lands as AI power gets scarce - Yahoo Finance") == _story_key(
        "Oracle's ‘force majeure’ warning lands as AI power gets scarce - TheStreet")
    assert _story_key("Pipeline right-of-way denied") != _story_key("Pipeline right-of-way reconsideration denied")
    for slug in sites:
        events = build("site_detail", slug)["materialEvents"]
        keys = [_story_key(e["text"]) for e in events]
        assert len(keys) == len(set(keys)), slug
        assert [e["date"] for e in events] == sorted((e["date"] for e in events), reverse=True), slug


def test_opposition_states_only_what_records_say(sites):
    for slug in sites:
        tab = build("site_tab:opposition", slug)
        for item in tab["items"]:
            if item["type"] == "Court case":
                continue
            assert item["status"] == "n/d", (slug, item)
            if item["evidence"]["tag"] != "F":
                assert item["where"] == "n/d", (slug, item)
        hearing = tab["nextHearing"]
        value = hearing["value"]["value"]
        if value is None:
            assert "note" not in hearing
        else:
            assert ISO_DAY.match(value) and value >= business_today().isoformat(), (slug, value)
            assert hearing["value"]["tag"] in ("F", "R")
            assert len(hearing.get("note", "")) <= 91


def test_power_history_names_the_power_source(sites):
    assert power_source_of("Fuel supply slip: Energy Transfer gas pipeline delayed ~6 months") == "Gas pipeline"
    assert power_source_of("Gas turbine air permits withdrawn; switch to fuel cells") == "Gas turbines"
    assert power_source_of("Redesign filed with NMED: Bloom fuel cells ~35% lower emissions") == "Fuel cells"
    assert power_source_of("Commercial operation planned 2028") == SITE_SCHEDULE
    labels = {"Gas pipeline", "Gas turbines", "Fuel cells", "On-site generation", "Grid", SITE_SCHEDULE}
    seen = 0
    for slug in sites:
        for row in build("site_tab:power", slug)["history"]:
            seen += 1
            assert row["source"] in labels, (slug, row)
            assert row["source"] == power_source_of(row["change"])
            assert row["evidence"]["sourceName"] is not None  # the publisher stays in the Record column
    if copied_rows("SELECT 1 FROM fact WHERE subject_type = 'site' AND field IN ('schedule_change', 'rfs_statement') "
                   "AND status = 'published' LIMIT 1"):
        assert seen > 0
