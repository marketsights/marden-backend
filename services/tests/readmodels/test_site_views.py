"""Site builders (completion, sites, site_detail, site_schedule, site_tab:*, project_file) on real seeded rows.

Rows are copied from the local `marden` database (site_rows.copy_sites): every financed site with its
parties, instruments, campuses, buildings, plants, facts, events, prices, holdings and model outputs.
Expectations are computed from the copied rows with SQL independent of the builders, so the tests
follow the data as collectors and models add to it. Where a model has not run, the documents must show
n/d (null), never a placeholder; where it has, they must show its output.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from real_rows import copied_rows
from site_rows import copy_sites
from site_shapes import SITE_TYPES, available, check

from marden.core import db
from marden.readmodels import business_today, rebuild_all
from marden.readmodels.context import building
from marden.readmodels.deps import targets
from marden.readmodels.registry import get_builder
from marden.readmodels.views import facilities_status, sites_debt, sites_detail, sites_power, sites_project, sites_tabs  # noqa: F401
from marden.readmodels.views import sites as _sites  # noqa: F401 (registers builders)
from marden.readmodels.views.sites_chain import is_placeholder
from marden.readmodels.views.sites_checklist import ITEMS

NAMED = ("jupiter", "abilene", "frontier", "the-barn", "coreweave-ddtl-3", "southgate")
LIST_VIEWS = ("sites", "completion")


@pytest.fixture
def sites() -> list[str]:
    return copy_sites()


def build(view: str, key: str = "all", day=None) -> Any:
    """The builder's document for `key` (built in a transaction, nothing written)."""
    b = get_builder(view)
    assert b is not None, view
    with building(day or business_today()), db.transaction() as conn:
        if b.build_many is not None:
            return dict(b.build_many(conn))[key]
        return b.build_one(conn, key)


def latest_output(model: str, slug: str) -> dict | None:
    rows = copied_rows(
        "SELECT * FROM model_output WHERE model = %s AND subject_type = 'site' AND subject_id = %s "
        "ORDER BY as_of DESC, id DESC LIMIT 1", (model, slug))
    return rows[0] if rows else None


def stored(view: str, key: str) -> Any:
    rows = copied_rows("SELECT doc FROM read_model WHERE view = %s AND key = %s AND as_of = %s",
                       (view, key, business_today()))
    return rows[0]["doc"] if rows else None


def test_every_site_view_rebuilds_every_site(sites):
    for view in SITE_TYPES:
        stats = rebuild_all(view)
        assert not stats.errors, (view, stats.errors[:3])
        assert stats.built == (1 if view in LIST_VIEWS else len(sites)), view
    assert stored("site_detail", "jupiter")["slug"] == "jupiter"
    events = copied_rows("SELECT payload FROM stream_event WHERE payload->>'type' = 'status'")
    assert {e["payload"]["siteSlug"] for e in events} == set(sites)  # site pages announce themselves
    # the event carries each link without its details lines, which stay in the document
    assert all(e["payload"]["chain"] == [{k: v for k, v in link.items() if k != "details"}
                                         for link in stored("site_detail", e["payload"]["siteSlug"])["chain"]]
               for e in events)
    before = len(events)
    rebuild_all("site_detail")  # unchanged documents: no new event
    assert len(copied_rows("SELECT id FROM stream_event WHERE payload->>'type' = 'status'")) == before


@pytest.mark.parametrize("view", list(SITE_TYPES))
def test_documents_match_the_response_types(sites, view):
    if not available():
        pytest.skip("TypeScript toolchain (apps/api/node_modules) not installed")
    keys = ["all"] if view in LIST_VIEWS else [s for s in NAMED if s in sites]
    docs = [(view, build(view, key)) for key in keys]
    errors = check(docs)
    assert errors == "", errors[:3000]


def test_model_pending_fields_are_null_only_until_the_model_runs(sites):
    for slug in [s for s in NAMED if s in sites]:
        detail, schedule = build("site_detail", slug), build("site_schedule", slug)
        status = latest_output("status", slug)
        if status is None:
            assert detail["status"] is None
            assert {link["status"] for link in detail["chain"]} == {"none"}
        else:
            assert detail["status"] == status["output"]["status"]
            # the status model decides each link's status and rule; the page adds named parties and details
            core = lambda chain: [(lk["role"], lk["status"], lk.get("ruleCode")) for lk in chain]  # noqa: E731
            assert core(detail["chain"]) == core(status["output"]["chain"])
        cushion = latest_output("cushion", slug)
        projection = (cushion or {"output": {}})["output"].get("projection") or {}
        if projection.get("p50"):
            assert schedule["projection"] == projection
            assert detail["cushionMonths"] == projection.get("cushionMonths")
        else:
            assert schedule["projection"] is None and detail["cushionMonths"] is None
            assert detail["rentStartP50"] is None


def _instrument_total(slug: str) -> float | None:
    rows = copied_rows(
        """SELECT a.value AS amount FROM instrument i
             JOIN fact_current a ON a.subject_type = 'instrument' AND a.subject_id = i.id AND a.field = 'amount_usd'
             LEFT JOIN fact_current s ON s.subject_type = 'instrument' AND s.subject_id = i.id AND s.field = 'status'
             LEFT JOIN fact_current b ON b.subject_type = 'instrument' AND b.subject_id = i.id
                  AND b.field = 'amount_basis'
            WHERE i.site_slug = %s AND coalesce(s.value #>> '{}', '') NOT IN ('repaid', 'proposed')
              AND coalesce(b.value #>> '{}', '') <> 'holder_par'""",
        (slug,))
    return round(sum(r["amount"] for r in rows) / 1e9, 3) if rows else None


def test_lists_cover_us_sites_with_their_debt(sites):
    rows = build("sites")
    completion = build("completion")
    slugs = {r["slug"] for r in rows}
    non_us = {s["slug"] for s in copied_rows("SELECT slug FROM site WHERE state ILIKE '%%australia%%'")}
    assert slugs == set(sites) - non_us and "southgate" not in slugs
    assert {r["slug"] for r in completion["rows"]} <= slugs
    for row in rows:
        assert row["debtB"]["value"] == _instrument_total(row["slug"]), row["slug"]
        parties = {p["role"]: p["label"] for p in copied_rows(
            "SELECT role, label FROM chain_party WHERE site_slug = %s", (row["slug"],))}
        label = parties.get("lenders", "n/d")
        # a placeholder label ("About 20 banks") gives way to the lenders the debt records name
        assert row["leadLenders"] == label or (is_placeholder(label) and row["leadLenders"]), row["slug"]
        assert [link["role"] for link in row["chain"]] == ["end_user", "tenant", "developer", "lenders"]
    frontier = next(r for r in rows if r["slug"] == "frontier")
    held = copied_rows("SELECT id FROM instrument WHERE site_slug = 'frontier' AND NOT in_v1")
    assert held and frontier["debtB"]["value"] == _instrument_total("frontier")  # the PGIM-held slice is not added
    summary = completion["summary"]
    with_cushion = [r for r in completion["rows"] if ((r["projection"] or {}).get("cushionMonths") or {}).get("value")
                    is not None]
    assert summary["pastOutside"]["count"] == sum(r["projection"]["cushionMonths"]["value"] < 0 for r in with_cushion)
    assert completion["asOf"] == business_today().isoformat()


def test_site_detail_header_and_debt_exposed(sites):
    detail = build("site_detail", "jupiter")
    quote = copied_rows("SELECT * FROM price_obs WHERE instrument_id = 'EX-JUPITER-190' AND kind = 'dealer' "
                        "ORDER BY obs_date DESC LIMIT 1")
    exposed = detail["debtExposed"]
    assert exposed["instrumentId"] == "EX-JUPITER-190"
    if quote:
        assert exposed["dealerQuote"]["value"] == f"{float(quote[0]['price']):.1f}"
        assert exposed["dealerQuote"]["tag"] == quote[0]["tag"] == "R"
    holders = copied_rows(
        "SELECT count(DISTINCT fund_entity_id) AS n FROM holding WHERE instrument_id = 'EX-JUPITER-190'")
    assert (holders[0]["n"] == 0) == exposed["holdersNote"].startswith("No N-PORT")
    # One source: the site header shows the loan's Marden price and recovery exactly as its own page does
    # (price / recovery model outputs, else the archive's recovery research fact with its own tag, else n/d M).
    loan = build("instrument_detail", "EX-JUPITER-190")
    assert exposed["mardenPrice"] == loan["mardenPrice"] and exposed["recovery"] == loan["recovery"]
    if not copied_rows("SELECT 1 FROM model_output WHERE model = 'price' AND subject_id = 'EX-JUPITER-190'") \
            and not copied_rows("SELECT 1 FROM price_obs WHERE instrument_id = 'EX-JUPITER-190' AND kind = 'marden'"):
        assert exposed["mardenPrice"]["value"] is None and exposed["mardenPrice"]["tag"] == "M"
    research = copied_rows("SELECT tag FROM fact_current WHERE subject_type = 'instrument' "
                           "AND subject_id = 'EX-JUPITER-190' AND field LIKE 'recovery%%'")
    if not copied_rows("SELECT 1 FROM model_output WHERE model IN ('price', 'recovery') "
                       "AND subject_id = 'EX-JUPITER-190'"):
        if research:
            assert exposed["recovery"]["value"] and exposed["recovery"]["tag"] == research[0]["tag"]
        else:
            assert exposed["recovery"]["value"] is None and exposed["recovery"]["tag"] == "M"
    assert detail["trackingNames"] == copied_rows("SELECT tracking_names FROM site WHERE slug = 'jupiter'")[0][
        "tracking_names"]
    assert detail["tabCounts"] == {
        "evidence": len(build("site_tab:evidence", "jupiter")["records"]),
        "opposition": len(build("site_tab:opposition", "jupiter")["items"]),
        "events": len(build("site_tab:events", "jupiter")["events"]),
    }
    notes = {(r["value"]["event_date"], r["value"]["title"]) for r in copied_rows(
        "SELECT value FROM fact WHERE subject_type = 'site' AND subject_id = 'jupiter' AND field = 'materiality_note'")}
    # one row per development: a row's reports (its sources, else the row itself) are material records
    assert detail["materialEvents"] and all((s["date"], s["text"]) in notes
                                            for e in detail["materialEvents"] for s in e.get("sources") or [e])


def test_debt_tab_lists_site_debt_and_tenant_notes(sites):
    debt = build("site_tab:debt", "jupiter")
    tenant_notes = {r["id"] for r in copied_rows(
        """SELECT i.id FROM instrument i JOIN fact_current f ON f.subject_type = 'instrument' AND f.subject_id = i.id
            WHERE f.field = 'exposure_links' AND i.in_v1 AND i.site_slug IS DISTINCT FROM 'jupiter'
              AND f.value @> '[{"case": "JUPITER", "role": "Tenant"}]'""")}
    # Instruments, then the stack's equity (first loss, no instrument), then the tenant's notes.
    ids = [i["instrumentId"] for i in debt["instruments"] if i["instrumentId"]]
    assert ids[0] == "EX-JUPITER-190" and set(ids[1:]) == tenant_notes
    tenant = copied_rows("SELECT entity_id FROM chain_party WHERE site_slug = 'jupiter' AND role = 'tenant'")[0]
    assert {i["payerSlug"] for i in debt["instruments"][1:] if i["instrumentId"]} <= {tenant["entity_id"]}
    names = copied_rows("SELECT tracking_names FROM site WHERE slug = 'jupiter'")[0]["tracking_names"]
    assert debt["spvName"] == names[0]
    assert [c["kind"] for c in debt["capitalStack"]] == ["loan", "equity"]
    lease = build("site_tab:lease", "jupiter")
    assert [t["key"] for t in lease["terms"]] == ["parties", "rent_start", "outside_date", "force_majeure", "rent",
                                                  "term", "termination", "guarantee"]
    assert all(("pending" in t) == (t["value"]["value"] is None) for t in lease["terms"])


def test_power_and_buildings_follow_links_and_plants(sites):
    power = build("site_tab:power", "jupiter")
    plants = {r["name"] for r in copied_rows(
        """SELECT DISTINCT p.name FROM power_plant p
            WHERE p.id IN (SELECT ps.plant_id FROM plant_serves ps JOIN site_link l ON l.campus_id = ps.campus_id
                           WHERE l.site_slug = 'jupiter' AND l.status IN ('approved', 'proposed'))
               OR p.id IN (SELECT subject_id FROM fact_current WHERE subject_type = 'plant' AND field = 'serves_sites'
                           AND value ? 'jupiter')""")}
    assert {s["source"] for s in power["sources"]} == plants
    lateral = copied_rows("SELECT f.* FROM power_plant p JOIN fact_current f ON f.subject_type = 'plant' "
                          "AND f.subject_id = p.id AND f.field = 'expected_cod' WHERE p.name ILIKE '%%lateral%%' "
                          "AND p.id IN (SELECT plant_id FROM plant_serves WHERE campus_id = 'project-jupiter')")
    if lateral:
        assert power["gasInService"]["value"]["value"] == lateral[0]["value"]
    buildings = build("site_tab:buildings", "abilene")
    rows = copied_rows("""SELECT b.id FROM building b JOIN site_link l ON l.campus_id = b.campus_id
                           WHERE l.site_slug = 'abilene' AND l.status IN ('approved', 'proposed')""")
    links = copied_rows("SELECT status FROM site_link WHERE site_slug = 'abilene'")
    assert {b["id"] for b in buildings["buildings"]} == {r["id"] for r in rows}
    assert all(b["footprint"]["type"] == "Polygon" for b in buildings["buildings"])
    assert buildings["linkApproved"] == any(link["status"] == "approved" for link in links)


def _expected_buildings(slug: str) -> tuple[int, int, str] | None:
    """(built, total, first campus) as SQL reads it from the copied rows, independent of the builder:
    the approved links (else the proposed ones); per campus its building records (Operating = built),
    else its building_count / buildings_operational facts. A campus's operating count is the one decision
    facilities_status makes from its records."""
    links = copied_rows("SELECT campus_id, building_ids, status FROM site_link WHERE site_slug = %s "
                        "AND status IN ('approved', 'proposed') ORDER BY campus_id", (slug,))
    links = [lk for lk in links if lk["status"] == "approved"] or links
    built = total = 0
    for link in links:
        records = copied_rows(
            """SELECT b.id, s.value #>> '{}' AS status FROM building b
                 LEFT JOIN fact_current s ON s.subject_type = 'building' AND s.subject_id = b.id AND s.field = 'status'
                WHERE b.campus_id = %s AND (%s::text[] IS NULL OR b.id = ANY(%s::text[]))""",
            (link["campus_id"], link["building_ids"], link["building_ids"]))
        if records:
            # the operating count decided from the strongest, newest record (facilities_status; its own tests)
            with building(business_today()), db.transaction() as conn:
                decided = facilities_status.load(conn, link["campus_id"])
            if link["building_ids"]:
                built += sum((decided.buildings.get(r["id"]) or {}).get("value") == "Operating" for r in records)
            else:
                built += decided.operating or 0
            total += len(records)
            continue
        if link["building_ids"]:
            continue
        counts = {r["field"]: r["value"] for r in copied_rows(
            "SELECT field, value FROM fact_current WHERE subject_type = 'campus' AND subject_id = %s "
            "AND field IN ('building_count', 'buildings_operational')", (link["campus_id"],))}
        n_total, n_built = counts.get("building_count") or 0, counts.get("buildings_operational") or 0
        built += min(n_built, n_total) if n_total else n_built
        total += max(n_total, n_built)
    return (built, total, links[0]["campus_id"]) if total else None


def test_list_rows_count_the_linked_campus_buildings(sites):
    """Spec 16 "Bldgs" on Sites and Completion: built / total on the linked campuses, n/d (null) when
    no linked campus has a building; the same value on both lists."""
    rows = build("sites")
    completion = {r["slug"]: r for r in build("completion")["rows"]}
    counted = 0
    for row in rows:
        expected = _expected_buildings(row["slug"])
        doc = row["buildings"]
        if expected is None:
            assert doc is None, row["slug"]
        else:
            counted += 1
            assert (doc["value"]["built"], doc["value"]["total"], doc["value"]["campusId"]) == expected, row["slug"]
            assert doc["tag"] in ("F", "R", "M") and doc["asOf"]
        if row["slug"] in completion:
            assert completion[row["slug"]]["buildings"] == doc, row["slug"]
    if copied_rows("SELECT 1 FROM site_link l JOIN building b ON b.campus_id = l.campus_id LIMIT 1"):
        assert counted > 0
    abilene = next((r for r in rows if r["slug"] == "abilene"), None)
    if abilene and abilene["buildings"]:  # the count is sourced by the record that decided it
        with building(business_today()), db.transaction() as conn:
            winner = facilities_status.load(conn, "stargate-abilene").winner
        if winner is not None:
            assert abilene["buildings"]["tag"] == winner.row["tag"]
            assert abilene["buildings"].get("sourceName") == winner.row["source_name"]


def test_opposition_and_event_lanes(sites):
    opposition = build("site_tab:opposition", "jupiter")
    courts = copied_rows(
        """SELECT k.subject_id FROM fact_current k JOIN fact_current l ON l.subject_type = 'entity'
             AND l.subject_id = k.subject_id AND l.field = 'linked_sites' AND l.value ? 'jupiter'
            WHERE k.subject_type = 'entity' AND k.field = 'proceeding_kind' AND k.value = '"court"'""")
    assert sum(i["type"] == "Court case" for i in opposition["items"]) == len(courts)
    if courts:
        assert opposition["openCases"]["value"]["value"] <= len(courts)
    events = build("site_tab:events", "jupiter")
    assert [lane["role"] for lane in events["lanes"]] == ["end_user", "tenant", "developer", "lenders", "chain"]
    status = latest_output("status", "jupiter")
    tenant = events["lanes"][1]["segments"]
    if status is None:
        assert tenant == []
    else:
        link = next(x for x in status["output"]["chain"] if x["role"] == "tenant")
        assert tenant[-1]["status"] == link["status"] and tenant[-1]["to"] == business_today().isoformat()
    assert all(e["severity"] in ("red", "amber", "green", "none") for e in events["events"])


def test_project_file_checklist_and_score(sites):
    pf = build("project_file", "jupiter")
    items = pf["checklist"]
    assert len(items) == len(ITEMS)
    counted = [i for i in items if i["state"] != "n/p"]
    assert pf["completeness"] == round(100 * sum(i["state"] == "sourced" for i in counted) / len(counted))
    assert {i["area"] for i in items if i["state"] == "n/p"} <= {"Insurance", "Independent engineer"}
    assert all((i["state"] == "sourced") == (i["value"]["value"] is not None) for i in items)
    parties = copied_rows("SELECT value FROM fact_current WHERE subject_type = 'site' AND subject_id = 'jupiter' "
                          "AND field IN ('chain_parties', 'chain_parties_presumed')")
    assert len(pf["parties"]) == sum(len(r["value"]) for r in parties)
    assert pf["structure"]["tranches"][0]["instrumentId"] == "EX-JUPITER-190"
    assert pf["construction"]["liens"] == [] and pf["construction"]["costPerMwM"]["value"] is None


def test_draws_vs_progress_follow_filed_draws_and_percent_complete(sites):
    reported = copied_rows(
        """SELECT DISTINCT i.site_slug FROM instrument i JOIN fact_current d ON d.subject_type = 'instrument'
             AND d.subject_id = i.id AND d.field = 'drawn_usd'
           JOIN fact_current c ON c.subject_type = 'instrument' AND c.subject_id = i.id AND c.field = 'commitment_usd'
          WHERE i.site_slug IS NOT NULL
         UNION SELECT subject_id FROM fact_current WHERE subject_type = 'site' AND field = 'pct_complete'""")
    assert reported, "no site has filed draws or a percent complete"
    for slug in [r["site_slug"] for r in reported]:
        points = build("project_file", slug)["construction"]["drawsVsProgress"]
        assert points and points == sorted(points, key=lambda p: p["date"]), slug
        last = points[-1]
        shares = copied_rows(
            """SELECT sum((d.value #>> '{}')::numeric) AS drawn, sum((c.value #>> '{}')::numeric) AS committed
                 FROM instrument i JOIN fact_current d ON d.subject_type = 'instrument' AND d.subject_id = i.id
                  AND d.field = 'drawn_usd'
                 JOIN fact_current c ON c.subject_type = 'instrument' AND c.subject_id = i.id
                  AND c.field = 'commitment_usd'
                WHERE i.site_slug = %s""", (slug,))[0]
        if shares["committed"]:
            assert last["drawnShare"] == round(min(float(shares["drawn"] / shares["committed"]), 1.0), 3), slug
        else:
            assert last["drawnShare"] is None
        pct = copied_rows("SELECT value FROM fact_current WHERE subject_type = 'site' AND subject_id = %s "
                          "AND field = 'pct_complete'", (slug,))
        assert last["builtShare"] == (round(pct[0]["value"] / 100, 3) if pct else None), slug
    # Signals nobody has computed yet stay n/d (null), never a placeholder 'false'.
    pf = build("project_file", reported[0]["site_slug"])["construction"]
    assert pf["drawPaceFlag"] is None and all(e["late"] is None for e in pf["equipment"])


def test_lease_today_tone_follows_the_cushion(sites):
    for slug in [s for s in NAMED if s in sites]:
        lease = build("site_tab:lease", slug)
        months = ((latest_output("cushion", slug) or {"output": {}})["output"].get("projection") or {}).get(
            "cushionMonths")
        value = months.get("value") if isinstance(months, dict) else None
        if lease["today"]["value"] is None or value is None:
            assert "todayTone" not in lease, slug
        else:
            assert lease["todayTone"] == ("red" if value < 0 else "amber" if value < 3 else "green"), slug


def test_backfilled_snapshot_shows_only_what_was_known(sites):
    seed_day = copied_rows("SELECT min(learned_at)::date AS d FROM fact WHERE subject_type = 'site'")[0]["d"]
    past = seed_day - timedelta(days=1)
    known = copied_rows("SELECT id FROM fact_current WHERE subject_type = 'site' AND subject_id = 'jupiter' "
                        "AND field = 'gross_mw' AND valid_from <= %s", (past,))
    if not known:
        pytest.skip("no Jupiter gross_mw fact dated before the seed day")
    copied_rows("UPDATE fact SET learned_at = %s WHERE id = %s RETURNING id", (past, known[0]["id"]))
    detail = build("site_detail", "jupiter", past)
    today = build("site_detail", "jupiter")
    assert detail["gw"]["value"] == today["gw"]["value"] is not None  # known on `past`
    assert detail["debtB"]["value"] is None and detail["materialEvents"] == []  # learned on the seed day
    # A model run for a past day reads only what was public that day (marden.models.pit): the snapshot shows
    # the output dated on or before `past`, whenever it was computed, and n/d when there is none.
    then = copied_rows("SELECT output FROM model_output WHERE model = 'status' AND subject_type = 'site' "
                       "AND subject_id = 'jupiter' AND as_of <= %s ORDER BY as_of DESC, created_at DESC, id DESC "
                       "LIMIT 1", (past,))
    assert detail["status"] == (then[0]["output"]["status"] if then else None)
    stats = rebuild_all("site_detail", as_of=past)
    assert not stats.errors and stats.events == 0
    assert copied_rows("SELECT count(*) AS n FROM read_model WHERE view = 'site_detail' AND as_of = %s",
                       (past,))[0]["n"] == len(sites)


def test_dependencies_reach_the_site_views(sites):
    with db.transaction() as conn:
        building_id = copied_rows("SELECT id FROM building WHERE campus_id = 'stargate-abilene' LIMIT 1")[0]["id"]
        assert {("site_detail", "abilene"), ("site_tab:buildings", "abilene")} <= targets(
            "building", building_id, "status", conn=conn)
        # The lists' "Bldgs" column (spec 16): a linked campus's building or count fact refreshes them.
        assert {("sites", "all"), ("completion", "all")} <= targets("building", building_id, "status", conn=conn)
        assert {("sites", "all"), ("completion", "all")} <= targets(
            "campus", "stargate-abilene", "building_count", conn=conn)
        unlinked = copied_rows("SELECT id FROM campus WHERE id NOT IN (SELECT campus_id FROM site_link) LIMIT 1")
        if unlinked:
            assert ("sites", "all") not in targets("campus", unlinked[0]["id"], "building_count", conn=conn)
        note = copied_rows("""SELECT i.id FROM instrument i JOIN fact_current f ON f.subject_type = 'instrument'
                               AND f.subject_id = i.id AND f.field = 'exposure_links'
                              WHERE f.value @> '[{"case": "JUPITER", "role": "Tenant"}]' LIMIT 1""")
        if note:
            assert ("site_tab:debt", "jupiter") in targets("instrument", note[0]["id"], "@price", conn=conn)
        plant = copied_rows("SELECT plant_id FROM plant_serves WHERE campus_id = 'project-jupiter' LIMIT 1")
        if plant:
            assert ("site_tab:power", "jupiter") in targets("plant", plant[0]["plant_id"], "status", conn=conn)
        court = copied_rows("SELECT subject_id FROM fact_current WHERE subject_type = 'entity' "
                            "AND field = 'linked_sites' AND value ? 'jupiter' LIMIT 1")
        if court:
            assert ("site_tab:opposition", "jupiter") in targets("entity", court[0]["subject_id"], "status", conn=conn)
        holding = copied_rows("SELECT h.id, i.site_slug FROM holding h JOIN instrument i ON i.id = h.instrument_id "
                              "WHERE i.site_slug IS NOT NULL LIMIT 1")
        if holding:
            assert ("site_tab:debt", holding[0]["site_slug"]) in targets(
                "holding", str(holding[0]["id"]), "@holding", conn=conn)
