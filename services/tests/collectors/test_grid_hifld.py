"""hifld_grid: STAC discovery and the GeoPackage reader on real HIFLD Next responses, the BA crosswalk on real
Control Areas attributes, boundary upserts and reruns, schedule, live discovery of the four layers.

The four grid GeoPackages are 17-104 MB, too large to keep as fixtures; the reader is tested on a real small
GeoPackage from the same host (USACE IENC wreck areas, 10 polygons) and the layers are read by the live run.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from marden.collectors import eia861_ba, eia930_ref, gpkg, grid_registry, hifld_grid, hifld_layers
from marden.collectors.grid_store import Region, get, name_key, upsert
from marden.collectors.rates_common import ParseError, RunStats
from marden.core import db, documents, sources
from marden.core.queue import app

DATA = Path(__file__).parent / "data"
HIFLD = DATA / "hifld_grid"


def _json(name: str) -> dict:
    return json.loads((HIFLD / name).read_text())


def _control_area_rows() -> list[dict[str, str]]:
    text = (HIFLD / "control-areas-items-limit40-offset0.csv").read_text(encoding="utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def _load_eia_bas() -> None:
    sources.upsert_spec(eia930_ref.SOURCE)
    sources.upsert_spec(eia861_ba.SOURCE)
    d930 = documents.record_document(eia930_ref.SOURCE_ID, eia930_ref.URL,
                                     (DATA / "eia930_ref" / "EIA930_Reference_Tables.xlsx").read_bytes(),
                                     content_type="application/vnd.ms-excel")
    eia930_ref.load(d930, RunStats())
    xlsx = (DATA / "eia861_ba" / "Balancing_Authority_2025.xlsx").read_bytes()
    d861 = documents.record_document(eia861_ba.SOURCE_ID, eia861_ba.zip_url(2025), xlsx,
                                     content_type="application/vnd.ms-excel")
    with db.transaction() as conn:
        grid_registry.ensure_registry(conn)
        for code, entry in eia861_ba.parse_xlsx(xlsx).items():
            upsert(conn, eia861_ba.region_for(code, entry, d861), prefer_existing=True)


def test_stac_links_and_collection_are_read_from_real_responses():
    dataset_url = f"{hifld_grid.API}/api/collections/hifld/datasets/control-areas"
    child = hifld_grid.link(_json("control-areas-dataset.json"), "child", dataset_url)
    assert child.endswith("/hifld/control-areas/control-areas/catalog.json")
    latest = hifld_grid.link(_json("control-areas-catalog.json"), "latest-version", child)
    assert latest.endswith("/control-areas/v1.0.0/collection.json")
    v = hifld_grid.parse_collection(_json("control-areas-v1.0.0-collection.json"), "control-areas", latest)
    assert (v.version, v.modified, v.issued, v.feature_count) == ("v1.0.0", "2025-04-08", "2022-12-09", 71)
    assert v.gpkg_url.endswith("/control-areas/v1.0.0/geopackage/control-areas.gpkg")
    assert (v.agency, v.office) == ("Department of Energy", "Oak Ridge National Laboratory")
    with pytest.raises(ParseError, match="no 'latest-version' link"):
        hifld_grid.link({"links": []}, "latest-version", "u")
    with pytest.raises(ParseError, match="no geopackage asset"):
        hifld_grid.parse_collection({"assets": {}}, "control-areas", "u")


def test_reads_a_real_geopackage_and_stores_its_polygons(committed):
    tables, features = gpkg.read((HIFLD / "wreck-areas-usace-ienc.gpkg").read_bytes())
    assert [(t.geometry_column, t.geometry_type, t.epsg) for t in tables] == [("geom", "POLYGON", 4326)]
    assert len(features) == 10 and all(f.wkb and f.srs_id == 4326 for f in features)
    with db.transaction() as conn:
        assert upsert(conn, Region("utility:t1", "utility", "Test", "US"), wkb=features[0].wkb) == "inserted"
        assert upsert(conn, Region("utility:t1", "utility", "Test", "US"), wkb=features[0].wkb) == "unchanged"
        assert upsert(conn, Region("utility:t1", "utility", "Test", "US"), wkb=features[1].wkb) == "changed"
    row = committed.execute(
        "SELECT GeometryType(geom) AS t, ST_SRID(geom) AS srid, ST_IsValid(geom) AS ok FROM grid_region"
    ).fetchone()
    assert (row["t"], row["srid"], row["ok"]) == ("MULTIPOLYGON", 4326, True)
    with pytest.raises(gpkg.GpkgError, match="not an SQLite"):
        gpkg.read(b"PK\x03\x04 not a geopackage")
    with pytest.raises(gpkg.GpkgError, match="'GP' magic"):
        gpkg.wkb_from_blob(b"XX\x00\x01\x00\x00\x00\x00\x01")
    assert gpkg.wkb_from_blob(b"GP\x00\x11\xe6\x10\x00\x00") == (None, 4326)  # empty-geometry flag


def test_every_real_control_area_maps_to_an_eia_code(committed):
    _load_eia_bas()
    with db.transaction() as conn:
        index = hifld_layers.BaIndex.load(conn)
    got = {row["NAME"]: index.match(hifld_layers.feature_id(row), row["NAME"]) for row in _control_area_rows()}
    assert len(got) == 40 and all(rid for rid, _ in got.values())
    assert got["ISO NEW ENGLAND INC."] == ("ba:ISNE", "eia861_ba_id")
    assert got["NEW BRUNSWICK SYSTEM OPERATOR"] == ("ba:NBSO", "eia861_ba_id")
    assert got["BALANCING AUTHORITY OF NORTHERN CALIFORNIA"] == ("ba:BANC", "name")  # EIA ids differ
    assert got["NEW HARQUAHALA GENERATING COMPANY, LLC - HGBA"] == ("ba:HGMA", "name")
    assert got["WESTERN AREA POWER ADMINISTRATION UGP WEST"][0] == "ba:WAUW"
    assert name_key("Duke Energy Florida, Inc.") == name_key("DUKE ENERGY FLORIDA INC")


def test_layer_rows_keep_eia_names_and_carry_their_source(committed):
    _load_eia_bas()
    sources.upsert_spec(hifld_grid.SOURCE)
    doc = documents.record_document(hifld_grid.SOURCE_ID, "https://example.test/control-areas.gpkg", b"x",
                                    content_type="application/geopackage+sqlite3")
    ctx = hifld_layers.LayerContext("control-areas", "v1.0.0", doc.id, doc.url, "2025-04-08", "2022-12-09",
                                    "Department of Energy", "Oak Ridge National Laboratory", 4326)
    rows = _control_area_rows()
    with db.transaction() as conn:
        index = hifld_layers.BaIndex.load(conn)
        region, how = hifld_layers.control_area_region(next(r for r in rows if r["source_ID"] == "5723"), ctx, index)
        assert (region.id, how) == ("ba:ERCO", "eia861_ba_id")
        upsert(conn, region, prefer_existing=True)
    erco = get(committed, "ba:ERCO")
    assert erco["name"] == "Electric Reliability Council of Texas, Inc."  # EIA-930's name kept
    assert erco["meta"]["hifld"]["as_of"] == "2025-04-08" and erco["meta"]["hifld"]["document_id"] == doc.id
    assert erco["meta"]["eia930"]["code"] == "ERCO"  # other sources' meta kept


def test_nerc_and_iso_features_map_to_registry_rows():
    ctx = hifld_layers.LayerContext("nerc-regions", "v1.0.0", 1, "u", "2022-12-07", None, None, None, 3857)
    wecc = hifld_layers.nerc_region({"NAME": "WESTERN ELECTRICITY COORDINATING COUNCIL (WECC)", "SUBNAME": "RMPA",
                                     "ID": "7"}, ctx)
    assert (wecc.id, wecc.parent_id) == ("nerc:wecc-rmpa", grid_registry.WESTERN)
    tre = hifld_layers.nerc_region({"NAME": "TEXAS RELIABILITY ENTITY (TRE)", "SUBNAME": "ERCOT"}, ctx)
    assert tre.parent_id == grid_registry.ERCOT
    assert hifld_layers.nerc_region({"NAME": "SERC RELIABILITY CORPORATION (SERC)", "SUBNAME": "VACAR"},
                                    ctx).parent_id == grid_registry.EASTERN
    iso = hifld_layers.iso_region({"ID": "56669", "NAME": "MIDCONTINENT INDEPENDENT TRANSMISSION SYSTEM OPERATOR"},
                                  ctx)
    assert (iso.id, iso.parent_id) == ("iso:MISO", grid_registry.EASTERN)
    util = hifld_layers.utility_region({"source_ID": "44372", "NAME": "ONCOR ELECTRIC DELIVERY COMPANY LLC",
                                        "CNTRL_AREA": "NOT AVAILABLE", "TYPE": "INVESTOR OWNED"}, ctx, {"ba:ERCO"})
    assert (util.id, util.parent_id, util.meta["hifld"]["type"]) == ("utility:44372", None, "INVESTOR OWNED")
    assert hifld_layers.utility_region({"source_ID": "5701", "NAME": "EL PASO ELECTRIC CO", "CNTRL_AREA": "EPE"},
                                       ctx, {"ba:EPE"}).parent_id == "ba:EPE"


def test_scheduled_monthly_in_the_collect_queue():
    assert app.tasks["collect.hifld_grid"].queue == "collect"
    assert {t.cron for (n, _), t in app.periodic_registry.periodic_tasks.items() if n == "collect.hifld_grid"} == {
        hifld_grid.CRON_UTC}


@pytest.mark.network
def test_live_discovery_finds_the_four_layers(committed):
    sources.upsert_spec(hifld_grid.SOURCE)
    expected = {"nerc-regions": 16, "independent-systems-operators": 7, "control-areas": 71,
                "electric-retail-service-territories": 2931}
    for layer, n in expected.items():
        v = hifld_grid.discover(hifld_grid.SOURCE_ID, layer, None, RunStats())
        assert v.gpkg_url.endswith(".gpkg") and v.feature_count == n and v.modified
    again = RunStats()
    hifld_grid.discover(hifld_grid.SOURCE_ID, "control-areas", None, again)
    assert again.documents_new == 0  # metadata unchanged: nothing new stored
