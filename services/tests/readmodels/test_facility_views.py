"""Data centers and On-site power builders (dc_row, dc_detail, dc_tab:*, plant_row, plant_detail, plant_debt)
on real seeded rows.

Rows are copied from the local `marden` database (facility_rows.copy_facilities): every campus,
building, plant and unit with their facts, events, plant_serves rows, site links, the linked sites'
instruments and status outputs, obligations and docket entities. Expectations are computed from the
copied rows with SQL independent of the builders. Values no record states are n/d (null), never a
placeholder; a site link counts only once a person approves it (spec 16).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from facility_rows import copy_facilities
from facility_shapes import VIEW_TYPES, assert_shapes
from real_rows import copied_rows

from marden.core import db
from marden.readmodels import business_today, rebuild_all
from marden.readmodels.context import building
from marden.readmodels.deps import targets
from marden.readmodels.registry import get_builder
from marden.readmodels.views import (  # noqa: F401 (registers builders)  # noqa: F401
    facilities_campus,
    facilities_campus_tabs,
    facilities_plant_debt,
    facilities_plants,
    facilities_status,
)

ABILENE, JUPITER, FRONTIER, BARN = "stargate-abilene", "project-jupiter", "vantage-frontier", "related-the-barn"
NAMED = (ABILENE, JUPITER, FRONTIER, BARN)
FRONTIER_PLANT, JUPITER_PLANT, GREEN_CHILE, HYPERION_CCGT = (
    "n-frontier-shack",
    "n-jupiter-microgrid",
    "n-greenchile",
    "n-hyp-ccgt1",
)
NON_US = "alibaba-zhangbei"
CAMPUS_VIEWS = ("dc_row", "dc_detail", "dc_tab:buildings", "dc_tab:records", "dc_tab:history", "dc_tab:capacity")
PLANT_VIEWS = ("plant_row", "plant_detail", "plant_debt")


@pytest.fixture
def campuses() -> list[str]:
    return copy_facilities()


def build(view: str, key: str, day=None) -> Any:
    """The builder's document for `key` (built in a transaction, nothing written)."""
    b = get_builder(view)
    assert b is not None, view
    with building(day or business_today()), db.transaction() as conn:
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


def test_campus_rows_show_the_current_facts(campuses):
    need(campuses, *NAMED)
    for cid in NAMED:
        row = build("dc_row", cid)
        campus = copied_rows("SELECT * FROM campus WHERE id = %s", (cid,))[0]
        assert (row["id"], row["name"], row["state"], row["county"]) == (
            cid,
            campus["name"],
            campus["state"],
            campus["county"],
        )
        for key, field in (
            ("facilityMw", "facility_mw"),
            ("itMw", "it_mw"),
            ("developer", "developer"),
            ("powerSource", "power_source"),
            ("onlineYear", "online_year"),
        ):
            current = fact("campus", cid, field)
            assert row[key]["value"] == (current["value"] if current else None), (cid, key)
            if current:
                assert (row[key]["tag"], row[key]["asOf"]) == (current["tag"], current["valid_from"].isoformat())
        total = copied_rows("SELECT count(*) AS n FROM building WHERE campus_id = %s", (cid,))[0]["n"]
        if total:
            # Status and the operating count are decided once from the strongest, newest record
            # (facilities_status, tested on its own in test_facilities_status.py): the row shows that one decision.
            with building(business_today()), db.transaction() as conn:
                decided = facilities_status.load(conn, cid)
            assert row["buildings"] == {"built": decided.operating, "total": total}
            assert row["status"]["value"] == decided.status["value"]
            assert (row["statusNote"] or {}).get("text") == decided.note
        else:  # no building record yet: the campus's stated status and its own stated count
            current = fact("campus", cid, "status")
            assert row["status"]["value"] == (current["value"] if current else None)
            assert row["buildings"]["total"] == (fact("campus", cid, "building_count") or {"value": 0})["value"]
        serving = {r["plant_id"] for r in copied_rows("SELECT plant_id FROM plant_serves WHERE campus_id = %s", (cid,))}
        assert (row["plantId"] in serving) if serving else row["plantId"] is None
        assert row["lastChange"] is not None and row["lastChange"]["date"] <= business_today().isoformat()
        assert row["firstSeen"] == fact("campus", cid, "first_seen")["value"]
    assert build("dc_row", FRONTIER)["plantId"] == FRONTIER_PLANT
    jupiter = build("dc_row", JUPITER)
    assert jupiter["plantId"] != GREEN_CHILE  # a fuel line is not the campus's power plant


def test_iso_is_a_code_and_tenant_falls_back_to_users(campuses):
    need(campuses, BARN)
    barn = build("dc_row", BARN)
    stated = fact("campus", BARN, "iso")
    assert stated["value"].startswith("MISO") and barn["iso"]["value"] == "MISO"
    assert barn["iso"]["sourceName"] == stated["source_name"]
    no_tenant = copied_rows(
        """SELECT u.subject_id FROM fact_current u WHERE u.subject_type = 'campus' AND u.field = 'users'
             AND NOT EXISTS (SELECT 1 FROM fact_current t WHERE t.subject_type = 'campus'
                             AND t.subject_id = u.subject_id AND t.field = 'tenant')
             AND u.subject_id IN (SELECT id FROM campus WHERE state ~ '^[A-Z]{2}$') ORDER BY 1 LIMIT 1"""
    )
    if no_tenant:
        cid = no_tenant[0]["subject_id"]
        users = fact("campus", cid, "users")["value"]
        names = [u["name"] for u in users if u.get("confidence", "confident") in ("confident", "likely")]
        assert build("dc_row", cid)["tenant"]["value"] == (", ".join(names) or None)


def test_campuses_outside_the_us_have_a_detail_but_no_row(campuses):
    need(campuses, NON_US)
    assert build("dc_row", NON_US) is None
    detail = build("dc_detail", NON_US)
    assert detail["id"] == NON_US and detail["state"] == "Hebei"
    stats = rebuild_all("dc_row")
    us = copied_rows("SELECT count(*) AS n FROM campus WHERE state ~ '^[A-Z]{2}$'")[0]["n"]
    assert stats.built == us and stats.removed == len(campuses) - us and not stats.errors


def test_financed_link_needs_an_approved_site_link(campuses):
    need(campuses, JUPITER)
    links = copied_rows("SELECT * FROM site_link WHERE campus_id = %s", (JUPITER,))
    if not links:
        pytest.skip("no site link proposed for Jupiter")
    slug = links[0]["site_slug"]
    if links[0]["status"] != "approved":
        assert build("dc_row", JUPITER)["financed"] is None  # proposed links never show
        assert build("plant_row", JUPITER_PLANT)["financed"] is None
        with db.transaction() as conn:  # a person approves the loader's proposal (spec 16)
            conn.execute(
                "UPDATE site_link SET status = 'approved', approved_by = 'test', approved_at = now() "
                "WHERE site_slug = %s AND campus_id = %s",
                (slug, JUPITER),
            )
    financed = build("dc_row", JUPITER)["financed"]
    status = copied_rows(
        "SELECT output FROM model_output WHERE model = 'status' AND subject_id = %s "
        "ORDER BY as_of DESC, id DESC LIMIT 1",
        (slug,),
    )
    site = copied_rows("SELECT name FROM site WHERE slug = %s", (slug,))[0]
    assert (financed["siteSlug"], financed["siteName"]) == (slug, site["name"])
    assert financed["chainStatus"] == (status[0]["output"]["status"] if status else None)
    amounts = copied_rows(
        """SELECT a.value AS v FROM instrument i JOIN fact_current a ON a.subject_type = 'instrument'
             AND a.subject_id = i.id AND a.field = 'amount_usd'
             LEFT JOIN fact_current s ON s.subject_type = 'instrument' AND s.subject_id = i.id
                  AND s.field = 'status'
             LEFT JOIN fact_current b ON b.subject_type = 'instrument' AND b.subject_id = i.id
                  AND b.field = 'amount_basis'
            WHERE i.site_slug = %s
              AND coalesce(s.value #>> '{}', '') NOT IN ('repaid', 'proposed', 'cancelled', 'canceled')
              AND coalesce(b.value #>> '{}', '') <> 'holder_par'""",
        (slug,),
    )
    assert financed["debtB"]["value"] == (round(sum(r["v"] for r in amounts) / 1e9, 3) if amounts else None)
    assert build("plant_row", JUPITER_PLANT)["financed"] == financed
    with db.transaction() as conn:  # the site's status output and instruments refresh the plant pages
        assert ("plant_row", JUPITER_PLANT) in targets("site", slug, "@model:status", conn=conn)
        inst = copied_rows("SELECT id FROM instrument WHERE site_slug = %s LIMIT 1", (slug,))
        if inst:
            assert ("plant_detail", JUPITER_PLANT) in targets("instrument", inst[0]["id"], "amount_usd", conn=conn)


def test_plant_units_and_permitted_mw_are_computed_from_unit_facts(campuses):
    plants = {r["id"] for r in copied_rows("SELECT id FROM power_plant")}
    need(sorted(plants), FRONTIER_PLANT, HYPERION_CCGT)
    for pid in (FRONTIER_PLANT, HYPERION_CCGT):
        detail = build("plant_detail", pid)
        units = copied_rows(
            """SELECT u.id, c.value AS count, r.value AS rating FROM power_unit u
                 LEFT JOIN fact_current c ON c.subject_type = 'unit' AND c.subject_id = u.id AND c.field = 'count'
                 LEFT JOIN fact_current r ON r.subject_type = 'unit' AND r.subject_id = u.id
                      AND r.field = 'rating_mw_each'
                WHERE u.plant_id = %s""",
            (pid,),
        )
        assert detail["units"]["count"] == sum(u["count"] for u in units)
        assert sum(m["count"] for m in detail["units"]["byModel"]) == detail["units"]["count"]
        for unit in detail["unitRows"]:
            rating = unit["ratingMwEach"]["value"]
            assert unit["totalMw"] == (round(unit["count"] * rating, 3) if rating is not None else None)
            assert unit["status"] == fact("plant", pid, "status")["value"]
        if all(u["rating"] is not None for u in units):
            assert detail["permittedMw"]["value"] == round(sum(u["count"] * u["rating"] for u in units), 3)
        else:  # a unit without a rating: the plant's capacity as stated, never a partial sum
            assert detail["permittedMw"]["value"] == (fact("plant", pid, "capacity_mw") or {"value": None})["value"]
    assert (
        build("plant_row", HYPERION_CCGT)["permittedMw"]["tag"]
        == fact("unit", "n-hyp-ccgt1-unit-3x754mw", "count")["tag"]
    )


def test_air_permit_fuel_supply_and_served_campus(campuses):
    need(campuses, JUPITER, FRONTIER)
    frontier = build("plant_detail", FRONTIER_PLANT)
    permit = fact("plant", FRONTIER_PLANT, "air_permit")["value"]
    assert (frontier["airPermit"]["agency"], frontier["airPermit"]["number"]) == (permit["agency"], permit["number"])
    assert frontier["airPermit"]["type"] == ("Major" if permit["type"] == "PSD" else permit["type"])
    docket = copied_rows(
        """SELECT f.value FROM entity e JOIN fact_current f ON f.subject_type = 'entity'
                              AND f.subject_id = e.id AND f.field = 'status'
                             WHERE e.meta ->> 'proceeding_key' = %s""",
        (permit.get("proceeding_key"),),
    ) if permit.get("proceeding_key") else []
    if (docket and docket[0]["value"] == "PENDING") or permit.get("status") == "pending":
        assert frontier["airPermit"]["status"] == "Permit pending" == frontier["permit"]["status"]
    # filed / decided as the permit record states them (the air_permits collector); n/d when it does not
    assert frontier["permit"]["filed"] == permit.get("filed") and frontier["permit"]["decided"] == permit.get("issued")
    served = frontier["servedCampus"]
    mw = fact("campus", FRONTIER, "facility_mw")["value"]
    assert served["id"] == FRONTIER and served["facilityMw"]["value"] == mw
    assert served["loadCoverPct"]["value"] == round(100 * frontier["permittedMw"]["value"] / mw, 1)
    assert served["loadCoverPct"]["tag"] == "M"
    jupiter = build("plant_detail", JUPITER_PLANT)
    cod = fact("plant", GREEN_CHILE, "expected_cod")
    if cod is not None:  # the Green Chile lateral is the Jupiter plant's fuel line
        assert jupiter["fuelSupply"]["value"].endswith(f"in service {cod['value']}")
        dockets = fact("plant", GREEN_CHILE, "dockets")["value"]
        assert (jupiter["fuel"]["regulator"], jupiter["fuel"]["docket"]) == (dockets[0]["agency"], dockets[0]["number"])
        assert [s["date"] for s in jupiter["fuel"]["inService"]][-1] == cod["value"]
    assert frontier["fuel"] is None and frontier["fuelSupply"]["value"] is None
    with db.transaction() as conn:  # the fuel line's schedule refreshes the plant it feeds
        assert ("plant_detail", JUPITER_PLANT) in targets("plant", GREEN_CHILE, "expected_cod", conn=conn)


def test_plant_debt_shows_only_what_records_state(campuses):
    need(campuses, FRONTIER)
    debt = build("plant_debt", FRONTIER_PLANT)
    permit = fact("plant", FRONTIER_PLANT, "air_permit")
    if fact("plant", FRONTIER_PLANT, "owner") is None:
        assert debt["owner"]["value"] == permit["value"]["applicant"] and debt["owner"]["tag"] == permit["tag"]
    for field, key in (
        ("ucc_lien", "liens"),
        ("mortgage", "mortgages"),
        ("financing", "financing"),
        ("equipment_order", "equipmentOrders"),
    ):
        if fact("plant", FRONTIER_PLANT, field) is None:
            assert debt[key] == []  # nothing filed is n/d, not zero
    buyers = copied_rows(
        """SELECT o.id FROM obligation o JOIN fact_current f ON f.subject_type = 'plant' AND f.subject_id = %s
             AND f.field = 'operator_entity_id' AND o.to_entity_id = f.value #>> '{}'
            WHERE o.type = 'Power purchase'""",
        (FRONTIER_PLANT,),
    )
    assert (debt["powerBuyer"] is None) == (not buyers)


def test_records_history_buildings_and_capacity_tabs(campuses):
    need(campuses, BARN, ABILENE)
    records = build("dc_tab:records", BARN)
    keys = [(r["date"], r["sourceUrl"] or r["text"]) for r in records]
    assert len(keys) == len(set(keys))  # a docket item and its news copy are one record
    events = copied_rows("SELECT * FROM event WHERE subject_type = 'campus' AND subject_id = %s", (BARN,))
    assert {(e["event_date"].isoformat(), e["source_url"] or e["title"]) for e in events} <= set(keys)
    assert {r["type"] for r in records} <= {"Permit", "Zoning", "Filing", "Utility", "Court", "Press", "Imagery"}
    mpsc = [r for r in records if "U-21990" in r["text"]]
    assert mpsc and all(r["type"] == "Utility" for r in mpsc)
    images = copied_rows(
        "SELECT count(*) AS n FROM fact_current WHERE subject_type = 'campus' AND subject_id = %s "
        "AND field = 'imagery'",
        (BARN,),
    )[0]["n"]
    assert sum(r["type"] == "Imagery" for r in records) >= min(images, 1)
    history = build("dc_tab:history", ABILENE)
    assert [h["date"] for h in history] == sorted((h["date"] for h in history), reverse=True)
    assert any(h["field"] == "first_seen" for h in history)
    for h in history:
        assert h["before"] != h["after"] and h["evidence"]["date"] == h["date"]
    polygons = copied_rows(
        "SELECT count(*) AS n FROM building WHERE campus_id = %s AND GeometryType(footprint) = 'POLYGON'", (ABILENE,)
    )[0]["n"]
    assert len(build("dc_tab:buildings", ABILENE)) == polygons
    detail = build("dc_detail", ABILENE)
    assert detail["tabCounts"] == {
        "buildings": polygons,
        "records": len(build("dc_tab:records", ABILENE)),
        "history": len(history),
    }
    outputs = copied_rows("SELECT 1 FROM model_output WHERE model = 'capacity'")
    if not outputs:  # the estimate is the capacity model's to make (spec 27)
        assert build("dc_tab:capacity", ABILENE) == []


def test_backfill_shows_only_what_marden_knew(campuses):
    need(campuses, ABILENE)
    loaded = copied_rows("SELECT min(created_at) AS t FROM campus")[0]["t"].date()
    before = loaded - timedelta(days=1)
    for view in ("dc_row", "dc_detail", "plant_row"):
        stats = rebuild_all(view, as_of=before)
        assert stats.built == 0 and not stats.errors, view  # no campus or plant was known yet
    assert not copied_rows("SELECT 1 FROM read_model WHERE as_of = %s", (before,))


@pytest.mark.parametrize("views", [CAMPUS_VIEWS, PLANT_VIEWS], ids=["campus", "plant"])
def test_every_view_rebuilds_and_matches_the_response_types(campuses, views):
    checks = []
    for view in views:
        stats = rebuild_all(view)
        assert not stats.errors, (view, stats.errors[:3])
        docs = [
            r["doc"]
            for r in copied_rows("SELECT doc FROM read_model WHERE view = %s AND as_of = %s", (view, business_today()))
        ]
        assert len(docs) == stats.built and docs, view
        checks.append((VIEW_TYPES[view], docs))
    assert_shapes(checks)
    events = copied_rows("SELECT DISTINCT payload->>'type' AS t FROM stream_event")
    assert {e["t"] for e in events} == {"campus" if views is CAMPUS_VIEWS else "plant"}
