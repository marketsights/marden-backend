"""census_counties: vintage discovery on the real Census directory listings, the county layer of a real
cartographic boundary GeoPackage (six counties cut from cb_2025_us_all_5m.gpkg, rows unchanged), loads,
reruns and the 304 path, schedule, and live discovery.

The national file is ~30 MB zipped, too large for a fixture; the full file is read by the live run.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from marden.collectors import census_counties as cc
from marden.collectors.rates_common import ParseError
from marden.core import db, documents, sources
from marden.core.http import Fetcher, fetch_document
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "census_counties"
INDEX = (DATA / "geo-tiger-index.html").read_bytes()
LISTING = (DATA / "GENZ2025-gpkg-index.html").read_bytes()
ZIP = (DATA / "cb_2025_us_all_5m-six-counties.zip").read_bytes()
ZIP_URL = f"{cc.INDEX_URL}GENZ2025/gpkg/cb_2025_us_all_5m.zip"
LAST_MODIFIED = "Thu, 23 Apr 2026 13:45:29 GMT"


def _fetcher(seen: list[str]) -> Fetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if url == cc.INDEX_URL:
            return httpx.Response(200, content=INDEX, headers={"content-type": "text/html"})
        if url == f"{cc.INDEX_URL}GENZ2025/gpkg/":
            return httpx.Response(200, content=LISTING, headers={"content-type": "text/html"})
        if url == ZIP_URL:
            if request.headers.get("if-modified-since") == LAST_MODIFIED:
                return httpx.Response(304, headers={"last-modified": LAST_MODIFIED})
            return httpx.Response(200, content=ZIP, headers={"content-type": "application/zip",
                                                             "last-modified": LAST_MODIFIED})
        return httpx.Response(404)

    return Fetcher(client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=0)


def test_discovers_the_newest_vintage_from_the_real_listings():
    years = cc.vintages(INDEX)
    assert years[0] == 2025 and 2024 in years and years == sorted(years, reverse=True)
    assert cc.has_file(LISTING, 2025) and not cc.has_file(LISTING, 2024)
    with pytest.raises(ParseError, match="no GENZ"):
        cc.vintages(b"<html></html>")


def test_reads_the_county_layer_of_a_real_geopackage():
    features, srid = cc.read_counties(ZIP, 2025)
    assert srid == 4269  # NAD83, as Census publishes it; stored in EPSG:4326
    by_geoid = {f.properties["geoid"]: f.properties for f in features}
    assert by_geoid["48495"]["namelsad"] == "Winkler County" and by_geoid["48495"]["stusps"] == "TX"
    assert by_geoid["35013"]["name"] == "Doña Ana" and by_geoid["22083"]["namelsad"] == "Richland Parish"
    assert by_geoid["51683"]["namelsad"] == "Manassas city"
    assert all(f.wkb for f in features)
    with pytest.raises(ParseError, match="no cb_2024_us_county_5m layer"):
        cc.read_counties(ZIP, 2024)
    with pytest.raises(ParseError, match="not a zip"):
        cc.read_counties(b"not a zip", 2025)


def test_county_keys_match_how_our_records_write_counties():
    assert cc.county_key("Doña Ana County") == cc.county_key("Dona Ana") == "dona ana"
    assert cc.county_key("Richland Parish") == "richland" and cc.county_key("Denton County") == "denton"
    assert cc.county_key(None) == ""


def test_run_stores_raw_loads_counties_and_reruns_without_rewriting(committed):
    seen: list[str] = []
    first = cc.run(fetcher=_fetcher(seen), join=False, min_counties=6)
    assert first["ok"], first
    assert first["counties"]["inserted"] == 6 and first["counties"]["vintage"] == 2025
    rows = committed.execute(
        """SELECT geoid, stusps, namelsad, ST_SRID(geom) AS srid, ST_IsValid(geom) AS ok,
                  ST_Contains(geom, ST_SetSRID(ST_MakePoint(-103.16, 31.85), 4326)) AS has_wink, meta
           FROM us_county ORDER BY geoid"""
    ).fetchall()
    assert [r["geoid"] for r in rows] == ["22083", "35013", "48003", "48121", "48495", "51683"]
    assert all(r["srid"] == 4326 and r["ok"] for r in rows)
    winkler = next(r for r in rows if r["geoid"] == "48495")
    assert winkler["has_wink"] and winkler["meta"]["source_url"] == ZIP_URL and winkler["meta"]["vintage"] == 2025
    raw = documents.latest_document(cc.SOURCE_ID, ZIP_URL)
    assert raw is not None and raw.read() == ZIP and winkler["meta"]["document_id"] == raw.id

    second = cc.run(fetcher=_fetcher(seen), join=False, min_counties=6)
    assert second["ok"] and second["documents_new"] == 0 and second["facts_new"] == 0
    assert second["counties"]["skipped"] == "already loaded from this document"
    assert seen.count(ZIP_URL) == 2  # the second request was conditional and answered 304


def test_a_truncated_file_is_refused(committed):
    sources.upsert_spec(cc.SOURCE)
    doc = fetch_document(cc.SOURCE_ID, ZIP_URL, fetcher=_fetcher([]))
    features, srid = cc.read_counties(doc.read(), 2025)
    with pytest.raises(ParseError, match="only 6 counties"), db.transaction() as conn:
        cc.load(conn, features, srid, doc, 2025)
    assert committed.execute("SELECT count(*) AS n FROM us_county").fetchone()["n"] == 0


def test_scheduled_monthly_in_the_collect_queue():
    task = app.tasks["collect.census_counties"]
    assert task.queue == "collect"
    assert {t.cron for (n, _), t in app.periodic_registry.periodic_tasks.items() if n == "collect.census_counties"} == {
        cc.CRON_UTC}


@pytest.mark.network
def test_live_discovery_finds_the_national_geopackage(committed):
    sources.upsert_spec(cc.SOURCE)
    index = fetch_document(cc.SOURCE_ID, cc.INDEX_URL)
    year = cc.vintages(index.read())[0]
    found = []
    for y in (year, year - 1):
        listing = fetch_document(cc.SOURCE_ID, f"{cc.INDEX_URL}GENZ{y}/gpkg/")
        if cc.has_file(listing.read(), y):
            found.append(y)
    assert found and found[0] >= 2025
