"""The `facilities` seed loader against a slice of the real archive (MARDEN_SEED_DIR).

The slice: the JUPITER, VANTAGE_TX, APLD_PF1 and APLD_PF1B4 cases (their campuses, plants,
dockets, news and catalysts), two Epoch-only campuses and the first 20 SEC exhibits. The financed
sites come from the real `sites` loader. Every expected value below is read from the archive
files themselves; nothing is made up.
"""

from __future__ import annotations

import csv
import io
import os

import pytest

from marden.core import config
from marden.seed import common

pytestmark = pytest.mark.skipif(not os.environ.get("MARDEN_SEED_DIR"), reason="MARDEN_SEED_DIR is not set")

CASES = ["JUPITER", "VANTAGE_TX", "APLD_PF1", "APLD_PF1B4"]
EPOCH_ONLY = ("ep:Microsoft Fairwater Atlanta", "ep:QTS Richmond 1")
SCOPE = {"cases": CASES, "campus_keys": EPOCH_ONLY, "exhibit_limit": 20}


@pytest.fixture(scope="module")
def seed():
    config.reload_settings()
    common.seed_dir.cache_clear()
    return common.load_json


@pytest.fixture
def loaded(seed, committed):
    from marden.seed.facilities import load
    from marden.seed.sites import load as load_sites

    load_sites(CASES)
    return load(**SCOPE)


def _one(c, sql, *params):
    return c.execute(sql, params).fetchone()


def _counts(c) -> dict[str, int]:
    tables = ("campus", "building", "power_plant", "power_unit", "plant_serves", "site_link", "event",
              "review_item", "entity", "document")
    out = {t: _one(c, f"SELECT count(*) AS n FROM {t}")["n"] for t in tables}
    out["fact"] = _one(c, "SELECT count(*) AS n FROM fact WHERE producer = 'seed:facilities'")["n"]
    return out


def _csv(relpath: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO((common.seed_dir() / relpath).read_text(encoding="utf-8"))))


def test_second_run_writes_nothing_new(loaded, committed):
    from marden.seed.facilities import load

    first = _counts(committed)
    again = load(**SCOPE)
    assert _counts(committed) == first
    assert again["facts_new"] == 0 and again["events_new"] == 0 and again["facts_seen"] == loaded["facts_seen"]
    runs = committed.execute(
        "SELECT ok, meta->>'loader' AS loader, items_new FROM source_run ORDER BY id").fetchall()
    fac = [r for r in runs if r["loader"] == "facilities"]
    assert [r["ok"] for r in fac] == [True, True] and fac[1]["items_new"] == 0


def test_campus_rows_follow_dcmap(loaded, committed, seed):
    rows = {r["key"]: r for r in seed("aiwc/q3x/dcmap.json")["rows"]}
    jup = rows["project_jupiter"]
    c = _one(committed, "SELECT *, ST_X(geom) AS lon, ST_Y(geom) AS lat FROM campus WHERE id = 'project-jupiter'")
    assert c["name"] == jup["name"] and (round(c["lat"], 5), round(c["lon"], 5)) == (jup["lat"], jup["lon"])
    assert c["state"] == "NM" and c["county"] == "Doña Ana"  # Census county containing the point
    abilene_ep = rows["vantage_frontier"]["ep"]["name"]
    assert abilene_ep in _one(committed, "SELECT aliases FROM campus WHERE id = 'vantage-frontier'")["aliases"]
    ids = {r["id"] for r in committed.execute("SELECT id FROM campus").fetchall()}
    assert {"microsoft-fairwater-atlanta", "qts-richmond-1"} <= ids  # 'ep:' stripped, kebab case
    county = _one(committed, "SELECT tag, method_version, value FROM fact WHERE subject_id = 'project-jupiter'"
                  " AND field = 'county'")
    assert county["tag"] == "M" and county["method_version"].startswith("seed-facilities-rules")


def test_epoch_facts_are_reported_with_confidence(loaded, committed):
    row = next(r for r in _csv("aiwc/q3x/epoch_dl/x/data_centers.csv") if r["Name"] == "Microsoft Fairwater Atlanta")
    owner, label = row["Owner"].split(" #")
    f = _one(committed, "SELECT * FROM fact WHERE subject_id = 'microsoft-fairwater-atlanta' AND field = 'owner'")
    assert f["value"] == owner and f["tag"] == "R" and f["source_name"] == "Epoch AI"
    assert f["confidence"] == pytest.approx({"confident": 0.9, "likely": 0.6}[label])
    timeline = [r for r in _csv("aiwc/q3x/epoch_dl/x/data_center_timelines.csv")
                if r["Data center"] == "Microsoft Fairwater Atlanta" and r["Date"] <= "2026-10-19"]
    hist = committed.execute(
        "SELECT valid_from, value FROM fact WHERE subject_id = 'microsoft-fairwater-atlanta'"
        " AND field = 'operating_mw' ORDER BY valid_from").fetchall()
    assert [(h["valid_from"].isoformat(), float(h["value"])) for h in hist] == [
        (r["Date"], float(r["Power (MW)"])) for r in timeline]
    notice = _one(committed, "SELECT value FROM fact WHERE subject_id = 'microsoft-fairwater-atlanta'"
                  " AND field = 'copyright_notice'")
    assert "CC BY 4.0" in notice["value"]


def test_buildings_are_epoch_footprints(loaded, committed, seed):
    dcs = {d["id"]: d for d in seed("aiwc/q3x/epoch_dl/map_props.json")["dataCenters"]}
    expected = sum(1 for f in dcs["QTS Richmond 1"]["shapes"]["features"] if f["properties"]["type"] == "Building")
    rows = committed.execute(
        "SELECT id, ST_GeometryType(footprint) AS g, ST_IsClosed(ST_ExteriorRing(footprint)) AS closed"
        " FROM building WHERE campus_id = 'qts-richmond-1'").fetchall()
    assert len(rows) == expected and all(r["g"] == "ST_Polygon" and r["closed"] for r in rows)  # outlines closed


def test_plants_and_large_loads(loaded, committed, seed):
    from marden.seed.facilities_plants import PLANT_TYPES

    projects = {p["id"]: p for p in seed("power.json")["projects"]}
    plants = {r["id"] for r in committed.execute("SELECT id FROM power_plant").fetchall()}
    assert "n-jupiter-microgrid" in plants and "p-097" not in plants  # P-097 is a large-load request
    assert all(projects[_one(committed, "SELECT value FROM fact WHERE subject_type = 'plant' AND subject_id = %s"
                                        " AND field = 'legacy_id'", p)["value"]]["ty"] in PLANT_TYPES for p in plants)
    ll = committed.execute("SELECT subject_type, subject_id FROM fact WHERE field = 'large_load_request'"
                           " AND value->>'project_id' = 'P-097'").fetchall()
    assert {(r["subject_type"], r["subject_id"]) for r in ll} == {("campus", "vantage-frontier"), ("site", "frontier")}
    units = committed.execute("SELECT oem, model, count FROM power_unit WHERE plant_id = 'n-frontier-shack'"
                              " ORDER BY count").fetchall()
    assert [(u["model"], u["count"]) for u in units] == [("C32B", 20), ("J620", 250), ("J624", 370)]
    rating = _one(committed, "SELECT value, quote, tag FROM fact WHERE subject_type = 'unit' AND field ="
                  " 'rating_mw_each' AND subject_id LIKE 'n-frontier-shack-innio-jenbacher-j620%%'")
    assert float(rating["value"]) == 3.22 and "J620 (3.22 MW)" in rating["quote"] and rating["tag"] == "R"


def test_plant_serves_only_case_linked_campuses(loaded, committed, seed):
    rows = {r["key"]: r for r in seed("aiwc/q3x/dcmap.json")["rows"]}
    for r in committed.execute("SELECT plant_id, campus_id, evidence_fact_id FROM plant_serves").fetchall():
        key = next(k for k in rows if common.slugify(k.removeprefix("ep:")) == r["campus_id"])
        assert rows[key]["cov"] and rows[key]["case"] in CASES and r["evidence_fact_id"] is not None
        assert r["plant_id"] in {common.slugify(p["id"]) for p in rows[key]["pw"]}


def test_site_links_wait_for_a_person(loaded, committed):
    links = committed.execute("SELECT site_slug, campus_id, status FROM site_link ORDER BY 1").fetchall()
    assert {(r["site_slug"], r["campus_id"]) for r in links} == {
        ("jupiter", "project-jupiter"), ("frontier", "vantage-frontier"),
        ("polaris-forge-1", "apld-polaris-forge-1"), ("polaris-forge-1-building-4", "apld-polaris-forge-1")}
    assert {r["status"] for r in links} == {"proposed"}
    reviews = _one(committed, "SELECT count(*) AS n FROM review_item WHERE kind = 'link' AND status = 'open'")["n"]
    assert reviews == len(links)


def test_docket_items_become_site_events(loaded, committed, seed):
    items = seed("dockets/out/NMED_AQB_26_57.json")["items"]
    n = _one(committed, "SELECT count(DISTINCT (event_date, title, source_url)) AS n FROM event"
             " WHERE subject_id = 'jupiter' AND kind = 'docket' AND source_name = 'NMED'")["n"]
    assert n == len({(i["d"], " ".join(i["t"].split()), i["u"]) for i in items})
    assert _one(committed, "SELECT tag FROM event WHERE source_name = 'NMED' LIMIT 1")["tag"] == "F"
    ent = _one(committed, "SELECT id, kind FROM entity WHERE id = 'docket-nmed-aqb-26-57'")
    assert ent["kind"] == "docket"


def test_material_events_follow_the_review_rule(loaded, committed):
    pending = committed.execute(
        "SELECT e.id, e.event_date, e.material FROM event e WHERE e.status = 'candidate'").fetchall()
    for e in pending:  # material and dated on/after the archive date: wait for a person
        assert e["material"] and e["event_date"].isoformat() >= "2026-10-20"
        assert _one(committed, "SELECT status FROM review_item WHERE kind = 'event' AND ref_id = %s",
                    str(e["id"]))["status"] == "open"
    assert _one(committed, "SELECT count(*) AS n FROM event WHERE status = 'published' AND material")["n"] == 0
    notes = committed.execute("SELECT value FROM fact WHERE field = 'materiality_note'").fetchall()
    assert notes and all(n["value"]["event_date"] < "2026-10-20" for n in notes)
    fm = [n for n in notes if "force majeure" in n["value"]["title"].lower()]
    assert fm  # the archive's Jupiter force-majeure history is published as history with its judgment


def test_no_prices_and_exhibit_documents(loaded, committed, seed):
    assert _one(committed, "SELECT count(*) AS n FROM price_obs")["n"] == 0
    assert _one(committed, "SELECT count(*) AS n FROM fact WHERE producer = 'seed:facilities'"
                " AND (field LIKE '%%price%%' OR field = 'px')")["n"] == 0
    lib = seed("aiwc/q3x/docs/library.json")["docs"][:20]
    docs = {r["url"]: r for r in committed.execute(
        "SELECT url, doc_type, external_id FROM document WHERE meta->>'record_only' = 'true'").fetchall()}
    assert set(docs) == {d["url"] for d in lib}
    assert all(docs[d["url"]]["doc_type"] == d["ex"] and docs[d["url"]]["external_id"] == d["acc"] for d in lib)
