"""grid_join: point-in-polygon facts on campuses, buildings, sites and plants; stand-in points and county polygons for
subjects without a point; overlaps, county splits, retirements, seams; reruns.

Boundaries here are small test rectangles (the join logic is what is tested; the real HIFLD layers are read by
the hifld_grid tests and the live run). Each rectangle carries the meta hifld_grid writes.
"""

from __future__ import annotations

from datetime import date

from psycopg.types.json import Jsonb

from marden.collectors import grid_join, grid_registry, hifld_grid
from marden.collectors.grid_store import Region, upsert
from marden.core import db, documents, sources
from marden.core.queue import app

GPKG = "https://storage.googleapis.com/hifld-next-datasets-prod/hifld/{0}/{0}/v1.0.0/geopackage/{0}.gpkg"


def _doc(layer: str) -> documents.DocumentRow:
    sources.upsert_spec(hifld_grid.SOURCE)
    return documents.record_document(hifld_grid.SOURCE_ID, GPKG.format(layer), f"test {layer}".encode(),
                                     content_type="application/geopackage+sqlite3", doc_type="geopackage")


def _box(conn, xmin: float, ymin: float, xmax: float, ymax: float) -> bytes:
    row = conn.execute("SELECT ST_AsBinary(ST_MakeEnvelope(%s, %s, %s, %s, 4326)) AS wkb",
                       (xmin, ymin, xmax, ymax)).fetchone()
    return bytes(row["wkb"])


def _hifld(doc: documents.DocumentRow, layer: str, as_of: str, fid: str, name: str) -> dict:
    return {"hifld": {"layer": layer, "version": "v1.0.0", "document_id": doc.id, "source_url": doc.url,
                      "as_of": as_of, "feature_id": fid, "name": name}}


def _setup(conn) -> dict[str, documents.DocumentRow]:
    docs = {k: _doc(k) for k in ("nerc-regions", "independent-systems-operators", "control-areas",
                                 "electric-retail-service-territories")}
    grid_registry.ensure_registry(conn)
    nerc = docs["nerc-regions"]
    upsert(conn, Region("nerc:serc-central", "reliability_region", "SERC RELIABILITY CORPORATION (SERC) / CENTRAL",
                        "US", grid_registry.EASTERN, _hifld(nerc, "nerc-regions", "2022-12-07", "11", "SERC")),
           wkb=_box(conn, -100, 30, -80, 45))
    upsert(conn, Region(grid_registry.EASTERN, "interconnection", "Eastern Interconnection", None, None, {
        "boundary": {"layer": "nerc-regions", "version": "v1.0.0", "document_id": nerc.id, "source_url": nerc.url,
                     "as_of": "2022-12-07"}}), wkb=_box(conn, -100, 30, -80, 45))
    iso = docs["independent-systems-operators"]
    upsert(conn, Region("iso:PJM", "iso_rto", "PJM", "US", None, _hifld(
        iso, "independent-systems-operators", "2022-12-15", "14725", "PJM INTERCONNECTION, LLC")),
        wkb=_box(conn, -85, 38, -80, 42), prefer_existing=True)
    ca = docs["control-areas"]
    upsert(conn, Region("ba:PJM", "balancing_authority", "PJM Interconnection, LLC", "US", grid_registry.EASTERN, {
        **_hifld(ca, "control-areas", "2025-04-08", "14725", "PJM INTERCONNECTION, LLC"),
        "eia930": {"code": "PJM", "active": True}}), wkb=_box(conn, -85, 38, -80, 42))
    upsert(conn, Region("ba:XRET", "balancing_authority", "Retired test BA", "US", grid_registry.EASTERN, {
        **_hifld(ca, "control-areas", "2025-04-08", "1", "RETIRED TEST BA"),
        "eia930": {"code": "XRET", "active": False, "retirement_date": "2026-04-01"}}),
        wkb=_box(conn, -95, 35, -90, 40))
    upsert(conn, Region("ba:XWST", "balancing_authority", "Western test BA", "US", grid_registry.WESTERN, {
        **_hifld(ca, "control-areas", "2025-04-08", "2", "WESTERN TEST BA"),
        "eia930": {"code": "XWST", "active": True}}), wkb=_box(conn, -99, 31, -98, 32))
    ut = docs["electric-retail-service-territories"]
    for fid, name, box in (("1", "UTILITY ONE", (-85, 39, -81, 41)), ("2", "UTILITY TWO", (-83, 39, -80, 41))):
        upsert(conn, Region(f"utility:{fid}", "utility", name, "US", "ba:PJM", _hifld(
            ut, "electric-retail-service-territories", "2025-02-19", fid, name)), wkb=_box(conn, *box))
    points = {"in-pjm": (-84.5, 40), "both-utils": (-82, 40), "non-rto": (-97, 33), "retired": (-92, 37),
              "seam": (-98.5, 31.5), "outside": (0, 0)}
    for slug, (lon, lat) in points.items():
        conn.execute("INSERT INTO site (slug, name, geom) VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))",
                     (slug, slug, lon, lat))
    conn.execute("""INSERT INTO campus (id, name, geom)
                    VALUES ('c1', 'Campus 1', ST_SetSRID(ST_MakePoint(-84.5, 40), 4326));
                    INSERT INTO building (id, campus_id, label) VALUES ('b1', 'c1', 'Building 1');
                    INSERT INTO power_plant (id, name, geom)
                    VALUES ('p1', 'Plant 1', ST_SetSRID(ST_MakePoint(-97, 33), 4326))""")
    return docs


def _facts(committed, subject: str) -> dict[str, list[dict]]:
    stype, sid = subject.split(":")
    out: dict[str, list[dict]] = {}
    for r in committed.execute("SELECT * FROM fact WHERE subject_type = %s AND subject_id = %s ORDER BY id",
                               (stype, sid)).fetchall():
        out.setdefault(r["field"], []).append(r)
    return out


def test_join_publishes_unique_matches_and_flags_the_rest(committed):
    with db.transaction() as conn:
        docs = _setup(conn)
    out = grid_join.run()
    assert out["ok"], out

    pjm = _facts(committed, "site:in-pjm")
    assert {f: [r["value"] for r in rows] for f, rows in pjm.items()} == {
        "interconnection": ["Eastern"], "iso": ["PJM"], "balancing_authority": ["PJM"], "utility": ["UTILITY ONE"],
        "utility_territories": [["UTILITY ONE"]]}
    iso = pjm["iso"][0]
    assert (iso["tag"], iso["status"], iso["source_name"], iso["producer"]) == ("F", "published", "HIFLD",
                                                                                 "collector:grid_join")
    assert iso["valid_from"] == date(2022, 12, 15) and iso["document_id"] == docs["independent-systems-operators"].id
    assert iso["source_url"] == GPKG.format("independent-systems-operators")
    assert iso["quote"] == "PJM INTERCONNECTION, LLC" and iso["quote_loc"].startswith("spatial join: point (-84.50000")
    assert pjm["interconnection"][0]["quote"] == "SERC RELIABILITY CORPORATION (SERC) / CENTRAL"

    both = _facts(committed, "site:both-utils")
    assert "utility" not in both  # overlapping territories: n/d until a filing names the utility
    assert both["utility_territories"][0]["value"] == ["UTILITY ONE", "UTILITY TWO"]

    assert _facts(committed, "site:non-rto")["iso"][0]["value"] == "non-RTO"
    assert _facts(committed, "plant:p1")["iso"][0]["status"] == "published"

    retired = _facts(committed, "site:retired")
    assert retired["balancing_authority"][0]["status"] == "candidate"
    assert retired["iso"][0]["value"] == "non-RTO" and retired["iso"][0]["status"] == "candidate"

    seam = _facts(committed, "site:seam")
    assert seam["interconnection"][0]["status"] == "candidate"  # a Western BA inside the Eastern union
    assert seam["balancing_authority"][0]["value"] == "XWST"

    assert _facts(committed, "site:outside") == {}
    building = _facts(committed, "building:b1")
    assert building["iso"][0]["value"] == "PJM" and "campus point" in building["iso"][0]["quote_loc"]
    assert _facts(committed, "campus:c1")["balancing_authority"][0]["value"] == "PJM"

    reviews = committed.execute("SELECT title, reason, payload FROM review_item ORDER BY title").fetchall()
    assert [r["title"] for r in reviews] == [
        "site retired: which balancing_authority?", "site retired: which iso?", "site seam: which balancing_authority?",
        "site seam: which interconnection?"]
    assert seam["balancing_authority"][0]["status"] == "candidate"  # the seam puts the BA in doubt too
    dirty = {(r["view"], r["key"]) for r in committed.execute("SELECT view, key FROM read_model_dirty").fetchall()}
    assert ("site_detail", "in-pjm") in dirty and ("dc_row", "c1") in dirty and ("plant_row", "p1") in dirty


def test_overlapping_isos_are_candidates_with_one_review(committed):
    with db.transaction() as conn:
        docs = _setup(conn)
        iso = docs["independent-systems-operators"]
        upsert(conn, Region("iso:MISO", "iso_rto", "MISO", "US", None, _hifld(
            iso, "independent-systems-operators", "2022-12-15", "56669", "MIDCONTINENT")),
            wkb=_box(conn, -85, 39, -84, 41), prefer_existing=True)
    grid_join.run()
    isos = _facts(committed, "site:in-pjm")["iso"]
    assert sorted((r["value"], r["status"]) for r in isos) == [("MISO", "candidate"), ("PJM", "candidate")]
    review = committed.execute("SELECT payload FROM review_item WHERE title = 'site in-pjm: which iso?'").fetchone()
    assert sorted(c["value"] for c in review["payload"]["candidates"]) == ["MISO", "PJM"]


def test_rerun_writes_nothing_new(committed):
    with db.transaction() as conn:
        _setup(conn)
    first = grid_join.run()
    second = grid_join.run()
    assert second["ok"] and second["facts_new"] == 0 and second["facts_seen"] == first["facts_seen"] > 0
    assert committed.execute("SELECT count(*) AS n FROM review_item").fetchone()["n"] == first["reviews"] == 4


def test_scheduled_daily_in_the_collect_queue():
    task = app.tasks["collect.grid_join"]
    assert task.queue == "collect"
    assert {t.cron for (n, _), t in app.periodic_registry.periodic_tasks.items() if n == "collect.grid_join"} == {
        grid_join.CRON_UTC}


def _county(conn, geoid: str, stusps: str, name: str, box: tuple[float, float, float, float]) -> None:
    conn.execute(
        """INSERT INTO us_county (geoid, statefp, countyfp, stusps, name, namelsad, geom, meta)
           VALUES (%s, %s, %s, %s, %s, %s, ST_Multi(ST_MakeEnvelope(%s, %s, %s, %s, 4326)), %s)""",
        (geoid, geoid[:2], geoid[2:], stusps, name, f"{name} County", *box,
         Jsonb({"vintage": 2025, "scale": "1:5M", "document_id": 1, "source_url": "https://www2.census.gov/x.zip"})))


def test_plants_without_a_location_use_their_served_campus_point(committed):
    with db.transaction() as conn:
        _setup(conn)
        conn.execute("""INSERT INTO power_plant (id, name, state) VALUES ('p2', 'On-site plant', NULL),
                                                                       ('p3', 'Plant in another state', 'WV');
                        UPDATE campus SET state = 'OH' WHERE id = 'c1';
                        INSERT INTO plant_serves (plant_id, campus_id) VALUES ('p2', 'c1'), ('p3', 'c1')""")
    grid_join.run()
    p2 = _facts(committed, "plant:p2")
    assert {f: rows[0]["value"] for f, rows in p2.items()} == {
        "interconnection": "Eastern", "iso": "PJM", "balancing_authority": "PJM"}  # no utility from a stand-in point
    assert "served campus c1 point (plant has no location)" in p2["iso"][0]["quote_loc"]
    assert p2["iso"][0]["status"] == "published"
    assert _facts(committed, "plant:p3") == {}  # the campus is in another state: not a stand-in


def test_subjects_without_a_point_are_placed_by_their_county_polygon(committed):
    with db.transaction() as conn:
        _setup(conn)
        _county(conn, "39001", "OH", "Inside", (-84.9, 40.5, -84.6, 40.8))  # inside PJM and UTILITY ONE only
        _county(conn, "39003", "OH", "Straddle", (-85.5, 39.5, -84.5, 39.8))  # half in PJM, half outside
        conn.execute("""INSERT INTO site (slug, name, state, county) VALUES ('s-inside', 'S', 'OH', 'Inside County'),
                            ('s-straddle', 'S2', 'OH', 'Straddle'), ('s-two', 'S3', 'OH', 'Inside; Straddle')""")
    out = grid_join.run()
    assert out["basis"]["county"] == 2
    inside = _facts(committed, "site:s-inside")
    assert {f: (rows[0]["value"], rows[0]["status"]) for f, rows in inside.items()} == {
        "interconnection": ("Eastern", "published"), "iso": ("PJM", "published"),
        "balancing_authority": ("PJM", "published"), "utility": ("UTILITY ONE", "published"),
        "utility_territories": (["UTILITY ONE"], "published")}
    assert inside["iso"][0]["quote_loc"].startswith("spatial join: county polygon Inside County, OH (us_county 39001")
    assert "(100.0% of the county area)" in inside["iso"][0]["quote_loc"]

    straddle = _facts(committed, "site:s-straddle")
    isos = sorted((r["value"], r["status"], r["quote_loc"]) for r in straddle["iso"])
    assert [(v, st) for v, st, _ in isos] == [("PJM", "candidate"), ("non-RTO", "candidate")]
    assert "outside all Independent System Operators polygons" in isos[1][2]
    assert straddle["balancing_authority"][0]["status"] == "candidate"
    assert "utility" not in straddle
    review = committed.execute("SELECT reason, payload FROM review_item WHERE title = 'site s-straddle: which iso?'"
                               ).fetchone()
    assert "only partly inside iso:PJM PJM Interconnection (50.0% of the county area)" in review["reason"]
    assert sorted(c["value"] for c in review["payload"]["candidates"]) == ["PJM", "non-RTO"]
    assert _facts(committed, "site:s-two") == {}  # two counties named: no single polygon
    assert grid_join.run()["facts_new"] == 0
