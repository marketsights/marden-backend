"""Building construction stage from Sentinel-2 L2A, per building footprint and capture (method
building-stage@0.1): a Marden model observation (tag M) of what 10 m optical imagery shows on each
building's footprint. It measures construction progress only. It never says a building is operating:
10 m optical bands cannot see whether a hall is energized, and Marden holds no thermal imagery
(no Landsat, ECOSTRESS or other heat source is collected), so operation stays with filed certificates
of occupancy, energization records and reports.

For every campus with building footprints and stored Sentinel-2 imagery facts (sentinel2_store), and
every stored capture (one per month, the scene the collector picked):
1. Read the capture's bands over the image box from the Planetary Computer data API (the same /bbox/
   rendering the stored image URL uses, as a GeoTIFF of B02, B03, B04, B08, B11, B12 and SCL on one
   lon/lat grid; no token). The GeoTIFF is not stored: the scene id and the box reproduce it, and the
   scene's item JSON is the fact's document.
2. Per building whose footprint lies at least MIN_INSIDE inside the box: the footprint's pixels less a
   10 m edge (mixed pixels), kept when the scene classification calls them clear (SCL 2, 4, 5, 6, 7, as the
   change score). Fewer than MIN_PIXELS clear pixels, or under MIN_CLEAR of the footprint clear: no
   observation for that capture.
3. Each clear pixel is one of: vegetation (NDVI >= 0.30); white roof membrane (visible reflectance
   >= 0.40, blue / red >= 0.80, NDVI < 0.15: the bright neutral surface of a finished white roof);
   bare soil (NDVI < 0.30, SWIR 1.6 um / NIR >= 1.25, not white: graded or cleared earth); other built
   surface (anything else: slab, steel, roof insulation boards, non-white roofs, equipment).
4. Stage of the footprint at that capture (first rule that holds):
   - bare_ground   vegetation >= 50 % ("natural ground, no works visible"; confidence 0.8), or
                   bare soil >= 60 % ("cleared or graded ground, no structure"; 0.6);
   - roofed        white roof >= 60 % ("white roof over most of the footprint"; 0.6, 0.8 when the
                   capture before was also roofed), or white + other built >= 70 % and the footprint's
                   mean reflectance moved less than STABLE_DELTA from each of the two captures before,
                   which were built-over too ("uniform built cover, unchanged over three captures"; 0.4:
                   a finished slab or a non-white roof look alike at 10 m);
   - under_construction  built-over but neither of the above ("roof not confirmed"; 0.4: a non-white
                   roof that changes with the light reads here), else a mix of soil, slab, steel and
                   partial roof (0.6).
   `roofedBy` is not stored per capture: readers take the first roofed capture that no later capture
   contradicts with bare_ground.

Facts (published; subject building; field `imagery_stage`; valid_from = the capture day; tag M;
method_version building-stage@0.1; producer model:building_stage; confidence as above; document = the
scene's item JSON stored by the Sentinel-2 collector):
  {stage, detail, scene_id, month, pixels, clear, shares: {vegetation, white_roof, bare_soil, built}}
quote_loc names the scene, the box and the pixel counts. A new reading of the same building and capture
supersedes the earlier one. `python -m marden collect sentinel2-buildings
[--campus ID ...]`; runs after each Sentinel-2 collection.
"""

from __future__ import annotations

import argparse
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np

from marden import cli
from marden.collectors.rates_common import RunStats, collect, print_stats
from marden.collectors.sentinel2 import SOURCE as S2_SOURCE
from marden.collectors.sentinel2_stac import item_url
from marden.collectors.sentinel2_store import SOURCE_NAME
from marden.core import db
from marden.core.db import Conn
from marden.core.facts import FactWriter
from marden.core.http import Fetcher, default_fetcher
from marden.core.queue import app

log = logging.getLogger(__name__)

METHOD_VERSION = "building-stage@0.1"
FIELD = "imagery_stage"
PRODUCER = "model:building_stage"
BANDS = ("B02", "B03", "B04", "B08", "B11", "B12", "SCL")
CLEAR_SCL = (2, 4, 5, 6, 7)
BOA_OFFSET_FROM = date(2022, 1, 25)  # processing baseline 04.00: reflectance = (DN - 1000) / 10000
MIN_INSIDE = 0.8
MIN_PIXELS = 12
MIN_CLEAR = 0.8
STABLE_DELTA = 0.03

VEG_NDVI = 0.30
WHITE_VIS, WHITE_BLUE_RED, WHITE_NDVI = 0.40, 0.80, 0.15
SOIL_SWIR_NIR = 1.25

STAGES = ("bare_ground", "under_construction", "roofed")
_BBOX = re.compile(r"/bbox/([^/]+)\.png")


@dataclass(frozen=True)
class Shares:
    pixels: int        # footprint pixels (edge removed) inside the box
    clear: float       # share of them the scene classification calls clear
    vegetation: float  # shares of the clear pixels
    white_roof: float
    bare_soil: float
    built: float
    mean: tuple[float, ...]  # mean clear reflectance per band B02..B12, for the stability test


@dataclass(frozen=True)
class Stage:
    stage: str
    detail: str
    confidence: float


@dataclass
class Capture:
    day: date
    month: str
    scene_id: str
    url: str
    box: tuple[float, float, float, float]
    document_id: int | None


@dataclass
class Building:
    id: str
    label: str
    footprint: dict[str, Any]


# ---------------------------------------------------------------- pure parts
def band_url(image_url: str) -> str:
    """The stored true-colour /bbox/ PNG URL -> the same box and scene as a GeoTIFF of BANDS."""
    base, _, query = image_url.partition("?")
    keep = [p for p in query.split("&") if p.startswith(("collection=", "item="))]
    keep += [f"assets={b}" for b in BANDS]
    return base[: -len(".png")] + ".tif?" + "&".join(keep)


def image_box(url: str) -> tuple[float, float, float, float] | None:
    m = _BBOX.search(url)
    if not m:
        return None
    try:
        w, s, e, n = (float(x) for x in m[1].split(","))
    except ValueError:
        return None
    return (w, s, e, n) if w < e and s < n else None


def reflectance(raw: np.ndarray, day: date) -> np.ndarray:
    offset = 1000.0 if day >= BOA_OFFSET_FROM else 0.0
    return (raw.astype(np.float64) - offset) / 10000.0


def footprint_masks(footprint: dict[str, Any], box: tuple[float, float, float, float], shape: tuple[int, int]
                    ) -> tuple[np.ndarray, float]:
    """(core mask: footprint pixels less a one-pixel edge, share of the footprint inside the box)."""
    from rasterio.features import rasterize
    from rasterio.transform import from_bounds
    from scipy import ndimage

    h, w = shape
    inside = rasterize([(footprint, 1)], out_shape=(h, w), transform=from_bounds(*box, w, h)).astype(bool)
    # The footprint's own size on the same grid, unclipped: a box 3x as large around the footprint's bounds.
    xs = [p[0] for p in footprint["coordinates"][0]]
    ys = [p[1] for p in footprint["coordinates"][0]]
    px, py = (box[2] - box[0]) / w, (box[3] - box[1]) / h
    fw, fh = max(1, int(np.ceil((max(xs) - min(xs)) / px)) + 4), max(1, int(np.ceil((max(ys) - min(ys)) / py)) + 4)
    own_box = (min(xs) - 2 * px, max(ys) - fh * py + 2 * py, min(xs) - 2 * px + fw * px, max(ys) + 2 * py)
    whole = rasterize([(footprint, 1)], out_shape=(fh, fw), transform=from_bounds(*own_box, fw, fh)).sum()
    share = float(inside.sum() / whole) if whole else 0.0
    core = ndimage.binary_erosion(inside, iterations=1)
    return (core if core.sum() >= MIN_PIXELS else inside), min(share, 1.0)


def pixel_shares(bands: dict[str, np.ndarray], scl: np.ndarray, mask: np.ndarray) -> Shares:
    """Classify the footprint's clear pixels (rules in the module docstring)."""
    n = int(mask.sum())
    clear = mask & np.isin(scl, CLEAR_SCL)
    for b in bands.values():
        clear &= np.isfinite(b)
    k = int(clear.sum())
    if n == 0 or k == 0:
        return Shares(n, 0.0, 0.0, 0.0, 0.0, 0.0, ())
    b2, b3, b4, b8, b11 = (bands[x][clear] for x in ("B02", "B03", "B04", "B08", "B11"))
    ndvi = (b8 - b4) / np.maximum(b8 + b4, 1e-6)
    vis = (b2 + b3 + b4) / 3
    veg = ndvi >= VEG_NDVI
    white = ~veg & (vis >= WHITE_VIS) & (b2 / np.maximum(b4, 1e-6) >= WHITE_BLUE_RED) & (ndvi < WHITE_NDVI)
    soil = ~veg & ~white & (b11 / np.maximum(b8, 1e-6) >= SOIL_SWIR_NIR)
    built = ~veg & ~white & ~soil
    mean = tuple(float(bands[x][clear].mean()) for x in ("B02", "B03", "B04", "B08", "B11", "B12"))
    return Shares(n, k / n, float(veg.mean()), float(white.mean()), float(soil.mean()), float(built.mean()), mean)


def _built_over(s: Shares) -> bool:
    return s.white_roof + s.built >= 0.7


def classify(s: Shares, before: list[tuple[Shares, Stage]]) -> Stage | None:
    """Stage at this capture from its shares and the earlier captures' (oldest first). None = no reading."""
    if s.pixels < MIN_PIXELS or s.clear < MIN_CLEAR:
        return None
    pct = lambda x: f"{x:.0%}"  # noqa: E731
    if s.vegetation >= 0.5:
        return Stage("bare_ground", f"natural ground, no works visible (vegetation on {pct(s.vegetation)})", 0.8)
    if s.bare_soil >= 0.6:
        return Stage("bare_ground", f"cleared or graded ground, no structure (bare soil on {pct(s.bare_soil)})", 0.6)
    if s.white_roof >= 0.6:
        again = bool(before) and before[-1][1].stage == "roofed"
        return Stage("roofed", f"white roof over {pct(s.white_roof)} of the footprint", 0.8 if again else 0.6)
    last2 = before[-2:]
    if (_built_over(s) and len(last2) == 2 and all(_built_over(b) for b, _ in last2)
            and all(_delta(s, b) < STABLE_DELTA for b, _ in last2)):
        return Stage("roofed", f"uniform built cover over {pct(s.white_roof + s.built)} of the footprint, "
                               "unchanged over three captures (a finished slab looks the same at 10 m)", 0.4)
    if _built_over(s):
        return Stage("under_construction", f"built surface over {pct(s.white_roof + s.built)} of the footprint, "
                                           "roof not confirmed (not a white roof, still changing)", 0.4)
    return Stage("under_construction", f"mixed ground and structure: built {pct(s.built + s.white_roof)}, "
                                       f"bare soil {pct(s.bare_soil)}", 0.6)


def _delta(a: Shares, b: Shares) -> float:
    if not a.mean or not b.mean:
        return float("inf")
    return float(np.mean(np.abs(np.array(a.mean) - np.array(b.mean))))


def read_bands(content: bytes, day: date) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """GeoTIFF bytes (BANDS in order, then the data API's mask band) -> reflectance by band, SCL."""
    from rasterio.io import MemoryFile

    with MemoryFile(io.BytesIO(content)) as mem, mem.open() as src:
        raw = src.read()
        nodata = src.nodata
    if raw.shape[0] < len(BANDS):
        raise ValueError(f"expected {len(BANDS)} bands, got {raw.shape[0]}")
    bands: dict[str, np.ndarray] = {}
    for i, name in enumerate(BANDS[:-1]):
        r = reflectance(raw[i], day)
        bad = raw[i] == 0 if nodata is None else raw[i] == nodata
        bands[name] = np.where(bad, np.nan, r)
    return bands, raw[len(BANDS) - 1].astype(np.int64)


# ---------------------------------------------------------------- run
@dataclass
class Plan:
    campus_id: str
    buildings: list[Building]
    captures: list[Capture] = field(default_factory=list)


def load_plans(conn: Conn, campuses: list[str] | None) -> list[Plan]:
    rows = conn.execute(
        """SELECT b.campus_id, b.id, b.label, ST_AsGeoJSON(b.footprint)::jsonb AS footprint FROM building b
            WHERE b.footprint IS NOT NULL AND (%s::text[] IS NULL OR b.campus_id = ANY(%s::text[]))
            ORDER BY b.campus_id, b.id""",
        (campuses, campuses),
    ).fetchall()
    plans: dict[str, Plan] = {}
    for r in rows:
        plans.setdefault(r["campus_id"], Plan(r["campus_id"], [])).buildings.append(
            Building(r["id"], r["label"], r["footprint"]))
    for plan in plans.values():
        seen: set[str] = set()
        for f in conn.execute(
            """SELECT value, valid_from, document_id FROM fact WHERE subject_type = 'campus' AND subject_id = %s
                 AND field = 'imagery' AND status = 'published' ORDER BY valid_from DESC, id DESC""",
            (plan.campus_id,),
        ).fetchall():
            v = f["value"] if isinstance(f["value"], dict) else {}
            month, url, scene = v.get("month"), v.get("url"), v.get("scene_id")
            box = image_box(url) if isinstance(url, str) else None
            if not (isinstance(month, str) and isinstance(scene, str) and box) or month in seen:
                continue
            seen.add(month)
            plan.captures.append(Capture(f["valid_from"], month, scene, url, box, f["document_id"]))
        plan.captures.sort(key=lambda c: c.day)
    return [p for p in plans.values() if p.captures]


def observe(plan: Plan, fetcher: Fetcher, stats: RunStats) -> list[tuple[Building, Capture, Shares, Stage]]:
    """Every (building, capture, shares, stage) reading of a campus, oldest capture first."""
    history: dict[str, list[tuple[Shares, Stage]]] = {b.id: [] for b in plan.buildings}
    out = []
    for cap in plan.captures:
        resp = fetcher.get(band_url(cap.url))
        stats.extra["band_reads"] = stats.extra.get("band_reads", 0) + 1
        bands, scl = read_bands(resp.content, cap.day)
        shape = scl.shape
        for b in plan.buildings:
            mask, inside = footprint_masks(b.footprint, cap.box, shape)
            if inside < MIN_INSIDE:
                stats.extra["outside_box"] = stats.extra.get("outside_box", 0) + 1
                continue
            shares = pixel_shares(bands, scl, mask)
            stage = classify(shares, history[b.id])
            if stage is None:
                stats.extra["not_clear"] = stats.extra.get("not_clear", 0) + 1
                continue
            history[b.id].append((shares, stage))
            out.append((b, cap, shares, stage))
    return out


def fact_value(cap: Capture, shares: Shares, stage: Stage) -> dict[str, Any]:
    r = lambda x: round(x, 3)  # noqa: E731
    return {
        "stage": stage.stage, "detail": stage.detail, "scene_id": cap.scene_id, "month": cap.month,
        "pixels": shares.pixels, "clear": r(shares.clear),
        "shares": {"vegetation": r(shares.vegetation), "white_roof": r(shares.white_roof),
                   "bare_soil": r(shares.bare_soil), "built": r(shares.built)},
    }


def write(conn: Conn, readings: list[tuple[Building, Capture, Shares, Stage]], stats: RunStats) -> None:
    facts = FactWriter(conn)
    for b, cap, shares, stage in readings:
        box = ",".join(f"{x:.5f}" for x in cap.box)
        loc = (f"{cap.scene_id}; box {box}; {shares.pixels} footprint px (10 m edge removed), "
               f"{shares.clear:.0%} clear; B02,B03,B04,B08,B11,B12,SCL")
        res = facts.add(
            "building", b.id, FIELD, fact_value(cap, shares, stage), tag="M", source_name=SOURCE_NAME,
            source_url=item_url(cap.scene_id), valid_from=cap.day, producer=PRODUCER, status="published",
            document_id=cap.document_id, quote_loc=loc, method_version=METHOD_VERSION,
            confidence=stage.confidence,
        )
        stats.facts_seen += 1
        if res.created:
            stats.facts_new += 1
            stats.new_by_series[FIELD] = stats.new_by_series.get(FIELD, 0) + 1
        old = conn.execute(
            """SELECT id FROM fact WHERE subject_type = 'building' AND subject_id = %s AND field = %s
                 AND status = 'published' AND valid_from = %s AND id <> %s""",
            (b.id, FIELD, cap.day, res.id),
        ).fetchall()
        if old:  # the same capture read again (a wider view, a new method version)
            facts.supersede([r["id"] for r in old], "building stage re-read for the same capture",
                            replaced_by={r["id"]: res.id for r in old})


def run(*, campuses: list[str] | None = None, fetcher: Fetcher | None = None) -> dict[str, Any]:
    def body(stats: RunStats) -> None:
        with db.transaction() as conn:
            plans = load_plans(conn, campuses)
        stats.extra.update(campuses=len(plans), captures=sum(len(p.captures) for p in plans))
        f = fetcher or default_fetcher()
        for plan in plans:
            readings = observe(plan, f, stats)
            with db.transaction() as conn:
                write(conn, readings, stats)

    return collect(S2_SOURCE, body)


@app.task(name="collect.sentinel2_buildings", queue="collect", queueing_lock="collect.sentinel2_buildings",
          lock="collect.sentinel2_buildings")
def collect_task() -> dict[str, Any]:
    return run()


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--campus", action="append", dest="campuses", default=None,
                        help="only this campus id (repeatable)")


@cli.action("collect", "sentinel2-buildings", help="Building construction stage from Sentinel-2 (model, tag M)",
            configure=_configure)
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run(campuses=args.campuses))
