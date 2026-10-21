"""plant_locate: EIA-860M matches (real August 2026 rows, trimmed), the on-site served-campus point, counties from
the point and from the queue, column sync, change-only reruns; and eia_860m's matcher using the served-campus
point."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from psycopg.types.json import Jsonb

from marden.collectors import eia_860m, plant_locate
from marden.collectors import eia_860m_store as store
from marden.collectors import plant_locate_eia as eia
from marden.collectors.eia_860m_parse import parse_workbook
from marden.collectors.rates_common import RunStats
from marden.core import db
from marden.core.documents import record_document
from marden.core.facts import FactWriter
from marden.core.queue import app
from marden.core.sources import upsert_spec

DATA = Path(__file__).parent / "data" / "eia_860m"
WORKBOOK = (DATA / "august_generator2026_trimmed.xlsx").read_bytes()
NEWEST = "https://www.eia.gov/electricity/data/eia860m/xls/august_generator2026.xlsx"
SEED = "seed:facilities"
HOMER = (40.512825, -79.19611)  # EIA-860M August 2026, plant 3122
CAMPUS = (32.50506, -99.781)


def _store_workbook():
    upsert_spec(eia_860m.SOURCE)
    return record_document("eia_860m", NEWEST, WORKBOOK, content_type=eia_860m.XLSX, doc_type="eia860m_xlsx",
                           doc_date=date(2026, 8, 31), external_id="august_generator2026.xlsx")


def _county(conn, geoid: str, stusps: str, name: str, box: tuple[float, float, float, float]) -> None:
    conn.execute(
        """INSERT INTO us_county (geoid, statefp, countyfp, stusps, name, namelsad, geom, meta)
           VALUES (%s, %s, %s, %s, %s, %s, ST_Multi(ST_MakeEnvelope(%s, %s, %s, %s, 4326)), %s)""",
        (geoid, geoid[:2], geoid[2:], stusps, name, f"{name} County", *box,
         Jsonb({"vintage": 2025, "layer": "cb_2025_us_county_5m", "document_id": None,
                "source_url": "https://www2.census.gov/geo/tiger/GENZ2025/gpkg/cb_2025_us_all_5m.zip"})))


def _plant(conn, pid: str, name: str, state: str, **facts) -> None:
    conn.execute("INSERT INTO power_plant (id, name, state) VALUES (%s, %s, %s)", (pid, name, state))
    writer = FactWriter(conn)
    for field, value in facts.items():
        writer.add("plant", pid, field, value, tag="R", source_name="Marden research archive",
                   valid_from=date(2026, 10, 20), producer=SEED, status="published")


def _serves(conn, pid: str, campus: str, state: str, point: tuple[float, float]) -> None:
    conn.execute("INSERT INTO campus (id, name, state, geom) VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), "
                 "4326)) ON CONFLICT DO NOTHING", (campus, campus, state, point[1], point[0]))
    fid = FactWriter(conn).add("plant", pid, "serves_campus", {"campus_id": campus, "basis": "dcmap.pw"}, tag="R",
                               source_name="Marden research archive", valid_from=date(2026, 10, 20), producer=SEED,
                               status="published").id
    conn.execute("INSERT INTO plant_serves (plant_id, campus_id, evidence_fact_id) VALUES (%s, %s, %s)",
                 (pid, campus, fid))


def _setup() -> None:
    _store_workbook()
    with db.transaction() as conn:
        _county(conn, "42063", "PA", "Indiana", (-79.5, 40.3, -78.9, 40.8))
        _county(conn, "42079", "PA", "Luzerne", (-76.4, 40.9, -75.6, 41.4))
        _county(conn, "48441", "TX", "Taylor", (-100.2, 32.0, -99.6, 32.7))
        _county(conn, "48139", "TX", "Ellis", (-97.1, 32.0, -96.4, 32.6))
        _plant(conn, "p-homer", "Homer City Energy Campus gas plant (PJM C01-1097 3,000 MW)", "PA",
               operator="Homer City Generation, L.P. (Knighthead)", technology_type="gas plant", capacity_mw=4400)
        _plant(conn, "p-susq", "Talen Susquehanna - Amazon 1.92 GW PPA", "PA", operator="Talen Energy",
               technology_type="co-location", capacity_mw=1920)
        _plant(conn, "p-gaia", "Gaia data center campus power", "TX", operator="Someone Else LLC",
               technology_type="data center campus power")  # EIA 'Gaia Hybrid' shares the name, nothing else
        _plant(conn, "p-onsite", "Stargate Abilene on-site gas generation & expansion", "TX", operator="Crusoe",
               technology_type="data center campus power")
        _serves(conn, "p-onsite", "c-abilene", "TX", CAMPUS)
        _plant(conn, "p-utility", "Utility gas unit for the Abilene campus", "TX", operator="Utility",
               technology_type="gas plant")
        _serves(conn, "p-utility", "c-abilene2", "TX", CAMPUS)
        _plant(conn, "p-lateral", "On-site gas lateral to the campus", "TX", technology_type="data center campus power")
        _serves(conn, "p-lateral", "c-abilene3", "TX", CAMPUS)
        _plant(conn, "p-queue", "Digital Ranch Gas (MCV 1 LLC) - ERCOT 28INR0100", "TX", operator="MCV 1 LLC",
               technology_type="gas plant", queue_ids=["28INR0100"])
        conn.execute("INSERT INTO queue_project (id, iso, queue_id) VALUES ('ercot:28INR0100', 'ercot', '28INR0100')")
        for field, value in (("county", "Ellis"), ("state", "TX")):
            FactWriter(conn).add("queue_project", "ercot:28INR0100", field, value, tag="F", source_name="ERCOT",
                                 source_url="https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId=1",
                                 valid_from=date(2026, 7, 1), producer="collector:ercot_gis", status="published")


def _current(committed, pid: str) -> dict[str, dict]:
    rows = committed.execute(
        "SELECT * FROM fact WHERE subject_type = 'plant' AND subject_id = %s AND producer = %s ORDER BY field",
        (pid, plant_locate.PRODUCER)).fetchall()
    return {r["field"]: r for r in rows}


def _row(committed, pid: str) -> dict:
    return committed.execute("SELECT county, eia_plant_id, ST_Y(geom) AS lat, ST_X(geom) AS lon FROM power_plant "
                             "WHERE id = %s", (pid,)).fetchone()


def test_plants_are_located_by_eia_then_on_site_campus_and_counties_follow(committed):
    _setup()
    out = plant_locate.run()
    assert out["ok"], out
    assert out["counts"]["located_by_eia_860m"] == 2 and out["counts"]["located_by_onsite_campus"] == 1

    homer = _current(committed, "p-homer")
    assert homer["eia_plant_id"]["value"] == 3122 and homer["eia_plant_id"]["tag"] == "F"
    assert homer["eia_plant_id"]["method_version"] == eia.METHOD and "operator:" in homer["eia_plant_id"]["quote_loc"]
    point = homer["coordinates"]
    assert (point["value"]["lat"], point["value"]["lon"]) == HOMER and point["tag"] == "F"
    assert (point["source_name"], point["source_url"], point["valid_from"]) == ("EIA-860M", NEWEST, date(2026, 8, 31))
    assert point["document_id"] is not None and "Latitude 40.512825 | Longitude -79.19611" in point["quote"]
    assert "august_generator2026.xlsx!" in point["quote_loc"]
    assert homer["county"]["value"] == {"name": "Indiana", "fips": "42063", "state": "PA"}
    assert (homer["county"]["tag"], homer["county"]["method_version"]) == ("R", plant_locate.county.COUNTY_METHOD)
    assert _row(committed, "p-homer") == {"county": "Indiana", "eia_plant_id": 3122, "lat": HOMER[0], "lon": HOMER[1]}

    assert _row(committed, "p-susq")["eia_plant_id"] == 6103  # 'TalenEnergy Susquehanna LLC' runs it: Talen

    onsite = _current(committed, "p-onsite")
    assert onsite["coordinates"]["tag"] == "R" and onsite["coordinates"]["method_version"] == plant_locate.ONSITE_METHOD
    assert onsite["coordinates"]["value"]["basis"] == "on-site at served campus c-abilene"
    assert onsite["coordinates"]["quote"] == "Stargate Abilene on-site gas generation & expansion"
    assert "eia_plant_id" not in onsite and onsite["county"]["value"]["name"] == "Taylor"
    assert _row(committed, "p-onsite") == {"county": "Taylor", "eia_plant_id": None, "lat": CAMPUS[0],
                                           "lon": CAMPUS[1]}

    for pid in ("p-gaia", "p-utility", "p-lateral"):  # name only; not on-site; a fuel line
        assert _current(committed, pid) == {} and _row(committed, pid)["lat"] is None, pid
    why = {u["plant"]: " ".join(u["why"]) for u in out["unplaced"]}
    assert "name only" in why["p-gaia"] and "do not say it is on-site" in why["p-utility"]
    assert "fuel line" in why["p-lateral"]

    queue = _current(committed, "p-queue")
    assert set(queue) == {"county"} and queue["county"]["tag"] == "F" and queue["county"]["source_name"] == "ERCOT"
    assert queue["county"]["value"] == {"name": "Ellis", "state": "TX", "fips": "48139"}
    assert queue["county"]["quote"] == "28INR0100 | County Ellis"
    assert _row(committed, "p-queue") == {"county": "Ellis", "eia_plant_id": None, "lat": None, "lon": None}

    dirty = {r["key"] for r in committed.execute("SELECT key FROM read_model_dirty WHERE view = 'plant_row'")}
    assert {"p-homer", "p-susq", "p-onsite", "p-queue"} <= dirty

    again = plant_locate.run()
    assert again["ok"] and again["facts_new"] == 0 and again["columns_changed"] == 0


def test_an_eia_plant_id_already_on_the_plant_is_used_and_eia_keeps_its_county(committed):
    _store_workbook()
    with db.transaction() as conn:
        _county(conn, "42129", "PA", "Westmoreland", (-79.5, 40.3, -78.9, 40.8))  # generalized line on the point
        _plant(conn, "p-x", "Some plant", "PA")
        FactWriter(conn).add("plant", "p-x", "eia_plant_id", 3122, tag="M", source_name="EIA-860M",
                             valid_from=date(2026, 8, 31), producer=store.PRODUCER, status="published",
                             method_version=store.MATCH_METHOD)
    out = plant_locate.run()
    assert out["counts"]["eia_by_eia_plant_id"] == 1 and out["counts"]["county_eia_record_over_polygon"] == 1
    facts = _current(committed, "p-x")
    assert set(facts) == {"coordinates", "county"}  # the id is sourced already
    assert facts["county"]["tag"] == "F" and facts["county"]["value"] == {"name": "Indiana", "state": "PA"}
    assert "Westmoreland County" in facts["county"]["quote_loc"]
    assert _row(committed, "p-x")["eia_plant_id"] == 3122


def test_a_filed_point_beats_the_campus_and_served_sites_and_states_are_checked(committed):
    _store_workbook()
    permit = (32.40, -99.90)
    with db.transaction() as conn:
        _county(conn, "48441", "TX", "Taylor", (-100.2, 32.0, -99.6, 32.7))
        writer = FactWriter(conn)
        # an on-site plant with a filed point (an air permit): the permit's point, not the campus's
        _plant(conn, "p-permit", "Abilene on-site gas generation", "TX", technology_type="data center campus power")
        _serves(conn, "p-permit", "c-abilene", "TX", CAMPUS)
        writer.add("plant", "p-permit", "coordinates", {"lat": permit[0], "lon": permit[1]}, tag="F",
                   source_name="TCEQ", source_url="https://www.tceq.texas.gov/permit", valid_from=date(2026, 9, 1),
                   producer="collector:air_permits", status="published", quote_loc="permit application p. 3")
        # a filed coordinates value without a numeric lat/lon is no point (and does not stop the run)
        _plant(conn, "p-badpoint", "Some other on-site plant", "TX", technology_type="gas plant")
        writer.add("plant", "p-badpoint", "coordinates", {"note": "see map"}, tag="F", source_name="TCEQ",
                   valid_from=date(2026, 9, 1), producer="collector:air_permits", status="published")
        # on-site by its utility, one served site with a point in its state: the site's point
        conn.execute("INSERT INTO site (slug, name, state, geom) VALUES ('s-abilene', 'Abilene', 'TX', "
                     "ST_SetSRID(ST_MakePoint(%s, %s), 4326))", (CAMPUS[1], CAMPUS[0]))
        _plant(conn, "p-site", "Turbines for the Abilene site", "TX", utility="Behind-the-meter",
               serves_sites=["s-abilene"])
        # on-site, but its one served campus is in another state: not placed there
        _plant(conn, "p-xstate", "On-site turbines in Southaven", "MS", technology_type="gas plant")
        _serves(conn, "p-xstate", "c-memphis", "TN", (35.0, -90.03))
    out = plant_locate.run()
    assert out["ok"], out

    permit_facts = _current(committed, "p-permit")
    assert set(permit_facts) == {"county"}  # the point is the permit's own fact; only the county is written
    assert permit_facts["county"]["valid_from"] == date(2026, 9, 1)
    assert _row(committed, "p-permit") == {"county": "Taylor", "eia_plant_id": None, "lat": permit[0],
                                           "lon": permit[1]}
    placed = {p["plant"]: p["method"] for p in out["placed"]}
    assert placed["p-permit"] == "record" and placed["p-site"] == "onsite_site"
    assert "p-badpoint" not in placed and "p-xstate" not in placed

    site = _current(committed, "p-site")["coordinates"]
    assert site["tag"] == "R" and site["value"]["basis"] == "on-site at served site s-abilene"
    assert site["quote"] == "Behind-the-meter"
    assert _current(committed, "p-xstate") == {} and _row(committed, "p-xstate")["lat"] is None

    again = plant_locate.run()
    assert again["facts_new"] == 0 and again["columns_changed"] == 0


def test_dry_run_writes_nothing(committed):
    _setup()
    out = plant_locate.run(dry_run=True)
    assert out["ok"] and out["facts_new"] == 0 and len(out["placed"]) == 3
    assert committed.execute("SELECT count(*) AS n FROM power_plant WHERE geom IS NOT NULL").fetchone()["n"] == 0


def test_matching_rules():
    report = parse_workbook(WORKBOOK, (2026, 8))
    by_id, by_state = eia.index(report)
    homer = eia.OurPlant("p", "Homer City Energy Campus", "PA", "Knighthead", None)
    assert eia.match(homer, by_id, by_state).status == "name_only"  # no operator, location or capacity agrees
    assert eia.match(eia.OurPlant("p", "Homer City Energy Campus", "PA", "Knighthead", "gas"), by_id,
                     by_state).site.id == 3122  # 'Homer City' as a phrase, and the technology agrees
    near = eia.OurPlant("p", "Homer City Energy Campus", "PA", "Knighthead", None, None, (40.5218, -79.1961), "c")
    assert eia.match(near, by_id, by_state).site.id == 3122  # 1 km from the served campus point
    wrong_tech = eia.OurPlant("p", "Homer City nuclear", "PA", "Homer City Generation", "nuclear")
    assert eia.match(wrong_tech, by_id, by_state).status == "name_only"
    danville = eia.OurPlant("p", "Danville Kentuck Road gas plant", "VA", "City of Danville", "gas")
    assert eia.match(danville, by_id, by_state).candidates[0]["why"].startswith("technology")  # petroleum units
    assert eia.company_words("NextEra Energy Point Beach LLC", "Point Beach Nuclear Plant") == {"next", "era"}
    assert eia.company_words("TalenEnergy Montour LLC", "TalenEnergy Montour") == {"talen"}
    assert eia.acronym("Nebraska Public Power District") == "nppd"
    assert eia.acronym("Public Service Co of Oklahoma") == "pso"
    assert eia.our_tech("Fuel cell", "data center campus power") == "fuel_cell"
    assert eia.our_tech(None, "SMR") == "nuclear" and eia.our_tech(None, "co-location") is None


def test_eia_860m_matches_a_plant_without_a_point_by_its_served_campus(committed):
    doc = _store_workbook()
    with db.transaction() as conn:
        conn.execute("INSERT INTO power_plant (id, name, state) VALUES ('p-h', 'Homer City Energy Campus', 'PA'), "
                     "('p-far', 'Homer City Energy Campus', 'PA')")
        for pid, campus, pt in (("p-h", "c-near", (40.5218, -79.1961)), ("p-far", "c-far", (40.6028, -79.1961))):
            conn.execute("INSERT INTO campus (id, name, state, geom) VALUES (%s, %s, 'PA', ST_SetSRID(ST_MakePoint("
                         "%s, %s), 4326))", (campus, campus, pt[1], pt[0]))
            conn.execute("INSERT INTO plant_serves (plant_id, campus_id) VALUES (%s, %s)", (pid, campus))
        summary = store.write_report(conn, RunStats(), parse_workbook(WORKBOOK, (2026, 8)), doc)
    assert summary["plants"]["matched_by_name_served_campus"] == 1
    assert summary["plants"]["name_match_not_within_2km"] == 1
    row = committed.execute("SELECT value, tag, quote_loc FROM fact WHERE subject_id = 'p-h' AND field = "
                            "'eia_plant_id'").fetchone()
    assert row["value"] == 3122 and row["tag"] == "M" and "served campus c-near point" in row["quote_loc"]


def test_scheduled_daily_before_grid_join():
    task = app.tasks["collect.plant_locate"]
    assert task.queue == "collect"
    crons = {t.cron for (n, _), t in app.periodic_registry.periodic_tasks.items() if n == "collect.plant_locate"}
    assert crons == {plant_locate.CRON_UTC} and plant_locate.CRON_UTC < "47 10 * * *"
