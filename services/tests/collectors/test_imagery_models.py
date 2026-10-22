"""Pure parts of the building-stage model (sentinel2_buildings) and the NAIP collector (naip): the pixel
rules on synthetic reflectance, the stage sequence, the band and render URLs, and the choice of NAIP items."""

from __future__ import annotations

from datetime import date

import numpy as np

from marden.collectors import naip
from marden.collectors import sentinel2_buildings as sb
from marden.collectors.sentinel2_stac import Box

PNG = ("https://planetarycomputer.microsoft.com/api/data/v1/item/bbox/-99.78899,32.49832,-99.77301,32.51180.png"
       "?collection=sentinel-2-l2a&item=S2B_X&assets=visual&asset_bidx=visual%7C1%2C2%2C3&nodata=0")


def bands(**refl: float) -> dict[str, np.ndarray]:
    base = {"B02": 0.1, "B03": 0.1, "B04": 0.1, "B08": 0.3, "B11": 0.2, "B12": 0.15}
    base.update(refl)
    return {k: np.full((10, 10), v) for k, v in base.items()}


SCL = np.full((10, 10), 5)
MASK = np.ones((10, 10), dtype=bool)


def test_band_url_keeps_scene_and_box_and_asks_for_the_bands():
    url = sb.band_url(PNG)
    assert url.startswith("https://planetarycomputer.microsoft.com/api/data/v1/item/bbox/-99.78899,32.49832,"
                          "-99.77301,32.51180.tif?collection=sentinel-2-l2a&item=S2B_X")
    assert [p for p in url.split("&") if p.startswith("assets=")] == [f"assets={b}" for b in sb.BANDS]
    assert sb.image_box(PNG) == (-99.78899, 32.49832, -99.77301, 32.5118)


def test_pixel_rules():
    veg = sb.pixel_shares(bands(B04=0.05, B08=0.4), SCL, MASK)
    assert veg.vegetation == 1.0
    white = sb.pixel_shares(bands(B02=0.6, B03=0.6, B04=0.62, B08=0.6, B11=0.5), SCL, MASK)
    assert white.white_roof == 1.0
    soil = sb.pixel_shares(bands(B02=0.1, B03=0.14, B04=0.18, B08=0.22, B11=0.35), SCL, MASK)
    assert soil.bare_soil == 1.0
    slab = sb.pixel_shares(bands(B02=0.2, B03=0.22, B04=0.24, B08=0.26, B11=0.27), SCL, MASK)
    assert slab.built == 1.0
    cloudy = sb.pixel_shares(bands(), np.full((10, 10), 9), MASK)
    assert cloudy.clear == 0.0 and sb.classify(cloudy, []) is None


def test_stage_sequence_and_confidence():
    veg = sb.pixel_shares(bands(B04=0.05, B08=0.4), SCL, MASK)
    soil = sb.pixel_shares(bands(B02=0.1, B03=0.14, B04=0.18, B08=0.22, B11=0.35), SCL, MASK)
    white = sb.pixel_shares(bands(B02=0.6, B03=0.6, B04=0.62, B08=0.6, B11=0.5), SCL, MASK)
    slab = sb.pixel_shares(bands(B02=0.2, B03=0.22, B04=0.24, B08=0.26, B11=0.27), SCL, MASK)
    assert sb.classify(veg, []).stage == "bare_ground"
    assert sb.classify(soil, []).stage == "bare_ground"
    first = sb.classify(white, [])
    assert (first.stage, first.confidence) == ("roofed", 0.6)
    assert sb.classify(white, [(white, first)]).confidence == 0.8
    # a built cover that is not a white roof: under construction until it holds still for three captures
    one = sb.classify(slab, [])
    assert (one.stage, one.confidence) == ("under_construction", 0.4)
    three = sb.classify(slab, [(slab, one), (slab, one)])
    assert (three.stage, three.confidence) == ("roofed", 0.4)
    assert {sb.classify(s, []).stage for s in (veg, soil, white, slab)} <= set(sb.STAGES)  # never "operating"


def test_reflectance_offset_from_baseline_04():
    raw = np.array([2000], dtype=np.uint16)
    assert sb.reflectance(raw, date(2026, 1, 1))[0] == 0.1
    assert sb.reflectance(raw, date(2021, 1, 1))[0] == 0.2


BOX = Box(-99.78899, 32.49832, -99.77301, 32.5118)


def item(id_: str, day: str, bbox: tuple[float, float, float, float], gsd: float = 0.6) -> naip.Item:
    return naip.Item(id_, date.fromisoformat(day), gsd, bbox)


def test_naip_choice_prefers_one_covering_item_of_the_newest_flight():
    cover22 = item("a22", "2022-07-26", (-99.82, 32.49, -99.74, 32.57))
    half22 = item("b22", "2022-07-26", (-99.82, 32.43, -99.74, 32.503))
    cover20 = item("a20", "2020-10-07", (-99.82, 32.49, -99.74, 32.57))
    assert naip.choose([cover20, half22, cover22], BOX) == [cover22]
    # newest flight only in parts: both parts, each over its share of the box
    north = item("n24", "2024-06-01", (-99.82, 32.503, -99.74, 32.57))
    south = item("s24", "2024-06-01", (-99.82, 32.43, -99.74, 32.5035))
    got = naip.choose([cover22, north, south], BOX)
    assert [i.id for i in got] == ["n24", "s24"]
    value = naip.fact_value(got, BOX)
    assert [t["bbox"] for t in value["tiles"]] == [[-99.78899, 32.503, -99.77301, 32.5118],
                                                   [-99.78899, 32.49832, -99.77301, 32.5035]]
    assert value["date"] == "2024-06-01" and value["caption"].startswith("USDA NAIP aerial photo, 1 Jun 2024: 0.6 m")
    assert naip.choose([], BOX) == []


def test_naip_render_url_is_about_1_2_m_per_pixel():
    url = naip.render_url("a22", BOX.bounds())
    assert "/item/bbox/-99.78899,32.49832,-99.77301,32.51180/1250x1250.webp?" in url
    assert "collection=naip" in url and "item=a22" in url
