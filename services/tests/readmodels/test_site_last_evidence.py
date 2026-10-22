"""Completion's 'Last evidence' (views/sites_evidence.py, views/sites_names.py) on real seeded rows.

Rows are copied from the local `marden` database (site_rows.copy_sites). The record a row shows must be
one of the site's own dated records (an event on the site, its linked campus or a plant serving it, or a
case-research item) and about the site or its chain parties: never a market print (GPU spot prices,
capacity auction results), a regulator order naming none of them (the MISO / PJM / NYISO rehearing
orders the archive flagged for several sites) or a county agenda. Expectations are computed from the
copied rows with SQL independent of the builder.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from real_rows import copied_rows
from site_rows import copy_sites

from marden.core import db
from marden.readmodels import business_today
from marden.readmodels.context import building
from marden.readmodels.deps import targets
from marden.readmodels.registry import get_builder
from marden.readmodels.views import sites as _sites  # noqa: F401 (registers builders)
from marden.readmodels.views.sites_common import site_row
from marden.readmodels.views.sites_links import linked_campuses, site_plants
from marden.readmodels.views.sites_names import SiteNames, site_names, url_words

LINKED = """SELECT campus_id FROM site_link WHERE site_slug = %(s)s AND status <> 'rejected'"""
RECORDS = f"""
    SELECT event_date::text AS date, title AS text, coalesce(source_url, '') AS url, tag, kind FROM event
     WHERE status = 'published' AND event_date <= %(d)s AND (
           (subject_type = 'site' AND subject_id = %(s)s)
        OR (subject_type = 'campus' AND subject_id IN ({LINKED}))
        OR (subject_type = 'plant' AND subject_id IN (SELECT plant_id FROM plant_serves WHERE campus_id IN ({LINKED}))))
    UNION ALL
    SELECT value->>'date', value->>'headline', coalesce(value->>'url', source_url, ''), tag, 'research' FROM fact
     WHERE subject_type = 'site' AND subject_id = %(s)s AND field = 'risk_evidence' AND status = 'published'"""


@pytest.fixture
def sites() -> list[str]:
    return copy_sites()


def completion_rows(day: date | None = None) -> dict[str, dict[str, Any]]:
    b = get_builder("completion")
    assert b is not None and b.build_many is not None
    with building(day or business_today()), db.transaction() as conn:
        doc = dict(b.build_many(conn))["all"]
    return {r["slug"]: r for r in doc["rows"]}


def names_of(slug: str) -> SiteNames:
    with building(business_today()), db.transaction() as conn:
        site = site_row(conn, slug)
        assert site is not None, slug
        campuses, _ = linked_campuses(conn, slug)
        return site_names(conn, site, campuses, site_plants(conn, slug, campuses), business_today())


def test_last_evidence_is_one_of_the_sites_own_dated_records(sites):
    rows = completion_rows()
    assert rows
    for slug, row in rows.items():
        ev = row["lastEvidence"]
        if ev is None:
            continue
        found = [r for r in copied_rows(RECORDS, {"s": slug, "d": business_today()})
                 if r["date"] == ev["date"] and r["text"] == ev["text"]]
        assert found, (slug, ev)
        assert ev["date"] <= business_today().isoformat(), slug
        assert ev["tag"] in {r["tag"] for r in found} and ev["sourceUrl"] in {r["url"] for r in found}, slug
        assert all(r["kind"] != "capacity_auction" for r in found), slug
        assert "/hr" not in ev["text"] and "spot print" not in ev["text"].lower(), (slug, ev["text"])


def test_generic_orders_flagged_for_several_sites_are_not_last_evidence(sites):
    """The archive judged FERC's rehearing orders (MISO EL26-70, NYISO EL26-69, PJM EL26-67) material for
    several sites; they name none of those sites' projects or parties, so no row shows them."""
    shared = copied_rows(
        """SELECT value->>'event_date' AS date, value->>'title' AS text FROM fact
            WHERE subject_type = 'site' AND field = 'materiality_note' AND status = 'published'
            GROUP BY 1, 2 HAVING count(DISTINCT subject_id) > 1""")
    if not shared:
        pytest.skip("no materiality note shared by several sites in the copied rows")
    generic = {(r["date"], r["text"]) for r in shared if r["text"].startswith("Order Dismissing Request for Rehearing")}
    rows = completion_rows()
    shown = {(r["lastEvidence"]["date"], r["lastEvidence"]["text"]) for r in rows.values() if r["lastEvidence"]}
    assert generic and not generic & shown
    for slug in ("the-barn", "lake-mariner", "hyperion", "river-bend"):
        if slug in rows and rows[slug]["lastEvidence"]:
            assert not rows[slug]["lastEvidence"]["text"].startswith("Order Dismissing Request for Rehearing"), slug


def test_childress_shows_a_record_about_childress_not_gpu_spot_prices(sites):
    if "childress" not in sites:
        pytest.skip("childress not in the copied rows")
    ev = completion_rows()["childress"]["lastEvidence"]
    assert ev is not None
    # IREN (developer) files its 10-K and 8-Ks under CIK 1878848: records of the chain, so the row shows
    # a record about the site at least as new as IREN's newest filing on it (filed or a newer report).
    cik = copied_rows("SELECT cik FROM entity WHERE id = 'iren-ltd'")
    filings = copied_rows(
        """SELECT max(value->>'date') AS d FROM fact WHERE subject_type = 'site' AND subject_id = 'childress'
             AND field = 'risk_evidence' AND status = 'published' AND tag = 'F' AND value->>'url' LIKE %s""",
        (f"%/edgar/data/{int(cik[0]['cik'])}/%",))
    assert filings[0]["d"], "IREN's filings are not in the copied rows"
    assert ev["date"] >= filings[0]["d"], ev
    assert "RunPod" not in ev["text"]


def test_newest_record_wins_when_no_material_event(sites):
    """Without a material event, the newest record about the site is shown whatever its tag; a filed
    record only wins a tie on the same day."""
    from marden.readmodels.views.sites_evidence import Candidate
    old_filed = Candidate(1, "2026-09-11", 0, 0, {})
    new_report = Candidate(2, "2026-10-17", 0, 0, {})
    same_day_report = Candidate(2, "2026-09-11", 0, 0, {})
    old_material = Candidate(0, "2026-08-01", 0, 0, {})
    assert sorted([old_filed, new_report], key=Candidate.key)[0] is new_report
    assert sorted([same_day_report, old_filed], key=Candidate.key)[0] is old_filed
    assert sorted([new_report, old_material], key=Candidate.key)[0] is old_material


def test_material_records_about_the_site_come_first(sites):
    """Jupiter: the archive judged Oracle's force-majeure coverage material; the newest of those wins over
    newer filed or reported records."""
    if "jupiter" not in sites:
        pytest.skip("jupiter not in the copied rows")
    notes = copied_rows(
        """SELECT value->>'event_date' AS date, value->>'title' AS text FROM fact
            WHERE subject_type = 'site' AND subject_id = 'jupiter' AND field = 'materiality_note'
              AND status = 'published' AND value->>'judgment' = 'material' AND value->>'event_date' <= %s
              AND value->>'title' ~ '(Oracle|Jupiter)'""", (business_today().isoformat(),))
    if not notes:
        pytest.skip("no material note naming Oracle or Project Jupiter")
    newest = max(n["date"] for n in notes)
    ev = completion_rows()["jupiter"]["lastEvidence"]
    assert ev["date"] == newest and ev["text"] in {n["text"] for n in notes if n["date"] == newest}


def test_place_names_count_only_for_the_site_not_the_local_government(sites):
    if "the-barn" not in sites or "abilene" not in sites:
        pytest.skip("the-barn / abilene not in the copied rows")
    barn = names_of("the-barn")
    assert barn.named_in("Michigan\u2019s largest data center project near Saline is taking shape")
    assert barn.named_in("OpenAI, Oracle data center fines for trucks on dirt roads nearly reach six figures")
    assert not barn.named_in("Saline, Township (Washtenaw, MI)")
    assert not barn.named_in("Saline Township Board of Trustees regular meeting")
    assert not barn.named_in("Michigan Public Service Commission order")  # 'Stargate Michigan' is the name
    abilene = names_of("abilene")
    assert abilene.named_in("Abilene residents win more time to fight power plant expansion")
    assert not abilene.named_in("City of Abilene City Council Meeting")
    county = copied_rows(
        """SELECT title, source_url FROM event WHERE subject_type = 'site' AND subject_id = 'the-barn'
             AND status = 'published' AND source_name ILIKE '%%saline%%township%%'""")
    for r in county:  # the township's agenda lines name no party of the site
        if not any(p in r["title"] for p in ("Related", "Oracle", "OpenAI", "Barn")):
            assert barn.strength(r["source_url"], r["title"]) is None, r["title"]


def test_a_party_filing_or_slug_names_the_site(sites):
    """An SEC filing under a chain party's CIK is the party's record; a publisher's URL slug is the
    article's own title."""
    if "lake-mariner" not in sites or "cedar-creek" not in sites:
        pytest.skip("lake-mariner / cedar-creek not in the copied rows")
    wulf = copied_rows("SELECT cik FROM entity WHERE id = 'terawulf-inc'")[0]["cik"]
    mariner = names_of("lake-mariner")
    assert mariner.strength(f"https://www.sec.gov/Archives/edgar/data/{int(wulf)}/000162828026064757/x.htm", "8-K") == 1
    assert mariner.strength("https://www.sec.gov/Archives/edgar/data/1341439/0001/x.htm", "8-K") is None
    assert url_words("https://www.datacenterdynamics.com/en/news/applications-filed-in-cedar-creek-texas/") == (
        "en news applications filed in cedar creek texas")
    ev = completion_rows()["cedar-creek"]["lastEvidence"]
    assert ev is not None and names_of("cedar-creek").strength(ev["sourceUrl"], ev["text"]) is not None


def test_no_record_about_the_site_yet_is_nd(sites):
    """Built for a day before most records: a site with no record about it shows n/d (null)."""
    early = date(2025, 1, 1)
    rows = completion_rows(early)
    assert any(r["lastEvidence"] is None for r in rows.values())
    assert all(r["lastEvidence"]["date"] <= early.isoformat() for r in rows.values() if r["lastEvidence"])


def test_linked_plant_and_campus_events_refresh_completion(sites):
    plant = copied_rows(
        """SELECT ps.plant_id FROM plant_serves ps JOIN site_link l ON l.campus_id = ps.campus_id
            WHERE l.status <> 'rejected' LIMIT 1""")
    if not plant:
        pytest.skip("no plant serving a linked campus")
    with db.transaction() as conn:
        assert {("completion", "all"), ("home", "all")} <= targets("plant", plant[0]["plant_id"], "@event", conn=conn)
