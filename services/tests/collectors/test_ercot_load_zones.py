"""ercot_zone_map / ercot_load_zones: ERCOT's Load Zone Map read at our Texas campuses (crops of the real map at
their true pixel positions), county reads (Denton; Andrews, where gray beside LZ_WEST is set aside), the gray
outside ERCOT, the affine fit on a drawn map, the ERCOT membership rule, the facts written (once), stale zones
listed, a changed map refused, and the live map (network).

The crops are lossless PNG copies of ERCOT-Maps_Load-Zone.jpg (sha256 ercot_zone_map.MAP_SHA256), named
map_crop_<name>_<col>_<row>.png by their top-left pixel; the EPSG:3081 points are those PostGIS gives for the
campuses' coordinates.
"""

from __future__ import annotations

import itertools
import json
from datetime import date
from pathlib import Path

import httpx
import numpy as np
import pytest

from marden.collectors import ercot_load_zones as zones
from marden.collectors import ercot_prices
from marden.collectors import ercot_zone_map as zmap
from marden.collectors.rates_common import ParseError, RunStats
from marden.core import db, documents, sources
from marden.core.facts import FactWriter
from marden.core.http import Fetcher

DATA = Path(__file__).parent / "data" / "ercot_prices"
MAP_SHAPE = (3825, 4207, 3)
# EPSG:3081 points (ST_Transform of the campus coordinates) and lon/lat.
POINTS = {
    "meta-temple": ((1250586.580869156, 999031.1391239128), (-97.3667185, 31.130952)),
    "sb-energy-milam": ((1284894.773851064, 985002.3582628407), (-97.01034, 30.99631)),
    "edgeconnex-cedar-creek": ((1234174.643469706, 889325.8393245132), (-97.5645, 30.143)),
    "microsoft-sat14": ((1127723.993129488, 813623.7436356316), (-98.6808627, 29.475229623224855)),
    "stargate-abilene": ((1020541.2834731268, 1148123.1463322432), (-99.781, 32.50506)),
    "vantage-frontier": ((1042606.2388035293, 1152174.0364356064), (-99.54558, 32.54105)),
    "meta-el-paso": ((400249.9379867029, 1108797.6282342703), (-106.36305, 31.99403)),
}


def canvas() -> np.ndarray:
    img = np.zeros(MAP_SHAPE, dtype=np.uint8)  # black: no legend colour
    for path in DATA.glob("map_crop_*.png"):
        col, row = (int(v) for v in path.stem.split("_")[-2:])
        crop = zmap.decode(path.read_bytes())
        img[row:row + crop.shape[0], col:col + crop.shape[1]] = crop
    return img


def denton_rings(county: str = "denton") -> list[list[tuple[float, float]]]:
    g = json.loads((DATA / f"{county}_county_3081.json").read_text())["geom_3081"]
    polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
    return [[tuple(p) for p in poly[0]] for poly in polys]


def test_gray_beside_one_zone_is_set_aside_for_a_subject_in_ercot():
    """Andrews County (Stingray) is all LZ_WEST on the map; its 2 km margin reaches Gaines County and New Mexico
    (gray). Gray decides nothing for a subject whose market facts put it in ERCOT; two zones, or only gray, still
    leave it unassigned."""
    img = canvas()
    andrews = zmap.over_polygon(img, denton_rings("andrews"))
    assert set(andrews.present) == {"LZ_WEST", zmap.OUTSIDE} and andrews.zone()[0] is None
    zone, why = andrews.zone(in_ercot=True)
    assert zone == "LZ_WEST" and f"{andrews.counts[zmap.OUTSIDE]} px set aside" in why
    assert zmap.over_polygon(img, denton_rings()).zone(in_ercot=True)[0] is None  # North and a Rayburn patch
    assert zmap.at_point(img, *POINTS["meta-el-paso"][0]).zone(in_ercot=True) == (
        None, "the map shows this area outside ERCOT (gray)")
    temple = zmap.at_point(img, *POINTS["meta-temple"][0], in_ercot=True)
    assert temple.zone(in_ercot=True)[0] == "LZ_SOUTH" and temple.nearest_other is None


def test_zones_at_our_campuses_on_the_real_map():
    img = canvas()
    got = {name: zmap.at_point(img, *xy).zone() for name, (xy, _) in POINTS.items()}
    assert {k: v[0] for k, v in got.items()} == {
        "meta-temple": "LZ_SOUTH", "sb-energy-milam": "LZ_NORTH", "edgeconnex-cedar-creek": None,
        "microsoft-sat14": "LZ_CPS", "stargate-abilene": "LZ_WEST", "vantage-frontier": "LZ_WEST",
        "meta-el-paso": None}
    # Cedar Creek sits on the Travis / Bastrop line, within 2 km of Austin Energy's zone: left unassigned.
    assert "LZ_AEN" in got["edgeconnex-cedar-creek"][1] and "LZ_SOUTH" in got["edgeconnex-cedar-creek"][1]
    assert got["meta-el-paso"][1] == "the map shows this area outside ERCOT (gray)"
    milam = zmap.at_point(img, *POINTS["sb-energy-milam"][0])
    assert milam.nearest_other == ("LZ_SOUTH", 2830.0)  # outside the 2 km margin
    temple = zmap.at_point(img, *POINTS["meta-temple"][0])
    assert temple.pixel == (2887, 1942) and temple.area_px == 113 and zmap.margin_px() == 6
    assert temple.nearest_other is None  # no other colour within 10 km
    county = zmap.over_polygon(img, denton_rings())  # North, with a Rayburn patch at its north-east corner
    assert county.zone()[0] is None and county.counts["LZ_RAYBN"] >= zmap.MIN_PIXELS
    assert county.counts["LZ_NORTH"] > 20_000


def test_labels_and_refusals():
    px = np.array([[zmap.ZONE_COLORS["LZ_CPS"], (10, 70, 110), (255, 255, 255), (90, 120, 200)]], dtype=np.uint8)
    assert [zmap.LABELS[i] if i >= 0 else None for i in zmap.label(px)[0]] == ["LZ_CPS", "LZ_CPS", None, None]
    with pytest.raises(ParseError, match="not a readable image"):
        zmap.decode(b"<html>Access denied</html>")
    with pytest.raises(ParseError, match="falls off the map"):
        zmap.at_point(np.zeros((50, 50, 3), np.uint8), *POINTS["meta-temple"][0])


def test_fit_recovers_a_drawn_affine():
    """Squares of 25 km drawn at a known affine as white lines on one fill colour; the fit starts 2% and 5 px off."""
    true = (0.003, 0.0, 50.0, 0.0, -0.003, 350.0)
    img = np.full((400, 400, 3), 255, np.uint8)
    img[50:351, 50:351] = zmap.ZONE_COLORS["LZ_WEST"]
    rings = []
    for x0 in range(0, 100_000, 25_000):
        for y0 in range(0, 100_000, 25_000):
            corners = [(x0, y0), (x0 + 25_000, y0), (x0 + 25_000, y0 + 25_000), (x0, y0 + 25_000), (x0, y0)]
            ring = np.array([np.array(p) + t * (np.array(q) - np.array(p)) for p, q in itertools.pairwise(corners)
                             for t in np.linspace(0, 1, 13, endpoint=False)])
            rings.append(ring)
            for x, y in ring:
                c, r = (round(v) for v in zmap.to_pixel(x, y, true))
                img[r - 1:r + 2, c - 1:c + 2] = 255
    fitted, residual = zmap.fit_affine(img, rings, (0.00306, 0.0, 55.0, 0.0, -0.00294, 345.0))
    for x, y in ((0, 0), (100_000, 100_000), (50_000, 25_000)):
        assert np.hypot(*np.subtract(zmap.to_pixel(x, y, fitted), zmap.to_pixel(x, y, true))) < 1.5
    assert residual < 1.0


@pytest.mark.parametrize(("facts", "expected"), [
    ({"iso": "ERCOT"}, True),
    ({"iso": "SPP (not ERCOT)"}, False),
    ({"iso": "WECC (El Paso Electric; not ERCOT)"}, False),
    ({"power_iso": "ERCOT (Oncor area)"}, True),
    ({"power_iso": "PJM (VA); ERCOT (Dallas)"}, False),
    ({}, False),
])
def test_ercot_membership(facts, expected):
    rows = {k: {"value": v, "valid_from": date(2026, 10, 1)} for k, v in facts.items()}
    assert zones.in_ercot(rows)[0] is expected


def _subject(committed, table: str, sid: str, lonlat: tuple[float, float] | None, county: str | None = None,
             facts: dict[str, str] | None = None) -> None:
    key = "slug" if table == "site" else "id"
    geom = "ST_SetSRID(ST_MakePoint(%s, %s), 4326)" if lonlat else "NULL"
    committed.execute(f"INSERT INTO {table} ({key}, name, state, county, geom) VALUES (%s, %s, 'TX', %s, {geom})",
                      (sid, sid, county, *(lonlat or ())))
    stype = {"site": "site", "campus": "campus", "power_plant": "plant"}[table]
    with db.transaction() as conn:
        for name, value in (facts or {}).items():
            FactWriter(conn).add(stype, sid, name, value, tag="F", source_name="HIFLD", valid_from=date(2026, 10, 21),
                                 producer="collector:grid_join", status="published")


@pytest.fixture
def mapped(committed, monkeypatch):
    sources.upsert_spec(ercot_prices.SOURCE)
    img = canvas()

    def fake_map(stats, source_id, fetcher):
        doc = documents.record_document(source_id, zmap.MAP_URL, b"map bytes", content_type="image/jpeg")
        stats.document(doc)
        return doc.id, img

    monkeypatch.setattr(zones, "map_document", fake_map)
    for name in ("meta-temple", "sb-energy-milam", "microsoft-sat14", "stargate-abilene"):
        _subject(committed, "campus", name, POINTS[name][1], facts={"iso": "ERCOT"})
    _subject(committed, "campus", "meta-el-paso", POINTS["meta-el-paso"][1],
             facts={"iso": "WECC (El Paso Electric; not ERCOT)"})
    _subject(committed, "campus", "no-facts", POINTS["meta-temple"][1])
    _subject(committed, "power_plant", "n-abilene-onsite", None, facts={"iso": "ERCOT"})
    committed.execute("INSERT INTO plant_serves (plant_id, campus_id) VALUES ('n-abilene-onsite', 'stargate-abilene')")
    _subject(committed, "power_plant", "p-099", None, facts={"iso": "ERCOT"})
    _subject(committed, "site", "coreweave-ddtl-3", None, county="Denton County", facts={"power_iso": "ERCOT"})
    _subject(committed, "site", "stingray", None, county="Andrews", facts={"power_iso": "ERCOT (Oncor)"})
    for county in ("denton", "andrews"):
        g = json.loads((DATA / f"{county}_county_3081.json").read_text())
        committed.execute(
            """INSERT INTO us_county (geoid, statefp, countyfp, stusps, name, namelsad, geom)
               VALUES (%s, '48', %s, 'TX', %s, %s, ST_Multi(ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%s), 3081),
                       4326)))""",
            (g["geoid"], g["geoid"][2:], g["namelsad"].removesuffix(" County"), g["namelsad"],
             json.dumps(g["geom_3081"])))


def test_assign_writes_zones_once(committed, mapped):
    stats = RunStats()
    zones.assign(stats, ercot_prices.SOURCE_ID, Fetcher())
    out = stats.extra["ercot_load_zones"]
    assert out["assigned"] == {"campus:meta-temple": "LZ_SOUTH", "campus:microsoft-sat14": "LZ_CPS",
                               "campus:stargate-abilene": "LZ_WEST", "plant:n-abilene-onsite": "LZ_WEST",
                               "campus:sb-energy-milam": "LZ_NORTH", "site:stingray": "LZ_WEST"}
    assert out["stale"] == {}
    assert set(out["unassigned"]) == {"site:coreweave-ddtl-3", "plant:p-099"}
    assert "LZ_RAYBN" in out["unassigned"]["site:coreweave-ddtl-3"]
    assert "county Denton County (us_county 48121)" in out["unassigned"]["site:coreweave-ddtl-3"]
    milam = committed.execute("SELECT quote_loc FROM fact_current WHERE subject_id = 'sb-energy-milam' AND field = %s",
                              (zones.FIELD,)).fetchone()
    assert "nearest other colour LZ_SOUTH at 2.8 km" in milam["quote_loc"]
    assert out["not_in_ercot"] == {"campus:meta-el-paso": "iso='WECC (El Paso Electric; not ERCOT)'",
                                   "campus:no-facts": "no market fact"}
    row = committed.execute("SELECT * FROM fact_current WHERE subject_id = 'n-abilene-onsite' AND field = %s",
                            (zones.FIELD,)).fetchone()
    assert (row["value"], row["tag"], row["producer"], row["method_version"], row["source_url"]) == (
        "LZ_WEST", "M", zones.PRODUCER, zones.METHOD_VERSION, zmap.MAP_URL)
    assert "campus stargate-abilene it serves" in row["quote_loc"] and row["valid_from"] == date(2026, 10, 21)
    stingray = committed.execute("SELECT quote_loc FROM fact_current WHERE subject_id = 'stingray' AND field = %s",
                                 (zones.FIELD,)).fetchone()
    assert "county Andrews County (us_county 48003)" in stingray["quote_loc"] and "set aside" in stingray["quote_loc"]
    assert stats.facts_new == 6
    again = RunStats()
    zones.assign(again, ercot_prices.SOURCE_ID, Fetcher())
    assert again.facts_new == 0 and again.facts_seen == 6


def test_a_zone_the_run_no_longer_supports_is_listed_stale(committed, mapped):
    with db.transaction() as conn:
        FactWriter(conn).add("campus", "meta-el-paso", zones.FIELD, "LZ_WEST", tag="M", source_name="ERCOT",
                             source_url=zmap.MAP_URL, valid_from=date(2026, 10, 21), producer=zones.PRODUCER,
                             status="published", method_version=zones.METHOD_VERSION)
    stats = RunStats()
    zones.assign(stats, ercot_prices.SOURCE_ID, Fetcher())
    assert stats.extra["ercot_load_zones"]["stale"] == {"campus:meta-el-paso": "LZ_WEST (now: not assigned)"}


def test_a_changed_map_is_refused(committed):
    sources.upsert_spec(ercot_prices.SOURCE)
    served = Fetcher(client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"another picture", headers={"content-type": "image/jpeg"}))))
    with pytest.raises(ParseError, match="changed its Load Zone Map"):
        zones.map_document(RunStats(), ercot_prices.SOURCE_ID, served)


def test_map_page_names_the_map():
    page = (DATA / "mediakit_maps_excerpt.html").read_text()
    assert "ERCOT Load Zone Map" in page and zmap.MAP_URL in page


@pytest.mark.network
def test_live_map_is_the_fitted_one(committed):
    sources.upsert_spec(ercot_prices.SOURCE)
    doc_id, img = zones.map_document(RunStats(), ercot_prices.SOURCE_ID, Fetcher())
    assert img.shape == MAP_SHAPE and documents.get_document(doc_id).sha256 == zmap.MAP_SHA256
    assert zmap.at_point(img, *POINTS["meta-temple"][0]).zone()[0] == "LZ_SOUTH"
    assert zmap.at_point(img, *POINTS["microsoft-sat14"][0]).zone()[0] == "LZ_CPS"
