"""ERCOT's Load Zone Map read by position: which load zone ERCOT draws at a point or over a county.

ERCOT publishes no county-to-load-zone table. A load's zone is set by the electrical bus it is connected to
(Nodal Protocols Section 3.4); the bus -> load zone list (NP4-160-SG "Settlement Points List and Electrical
Buses Mapping") is posted only on the MIS, whose /misapp and /misdownload paths www.ercot.com/robots.txt
disallows, and the Public API does not offer it ("does not seem to be available", checked 2026-10-21). ERCOT's
geographic statement of the zones is its Load Zone Map (MAP_URL, listed on https://www.ercot.com/news/mediakit/
maps): the competitive load zones North, West, South and Houston and the NOIE load zones Austin Energy (AEN),
CPS Energy (CPS), LCRA and Rayburn (RAYB), drawn over county lines, with Texas outside ERCOT in gray. Zone
borders do not follow county lines, and the NOIE zones are patches (LCRA and Rayburn many small ones).

Georeference. The map is drawn in Texas Centric Lambert Conformal Conic (EPSG:3081). AFFINE maps EPSG:3081
metres to map pixels; `fit_affine` fitted it to the map's white county lines using the Census Bureau 2025
county boundaries (us_county): residual median 3.2 px (one pixel is about 318 m) over 26,325 boundary samples,
against 4.8 px for plain longitude/latitude, 6.8 px for Web Mercator and 4.5 px for Texas Albers. AFFINE is
valid for the map whose sha256 is MAP_SHA256; another map is refused (ParseError) until it is refitted.

Reading. Pixels are labelled by the nearest legend fill colour within COLOR_TOLERANCE (county lines, labels and
JPEG edge blends stay unlabelled). A reading covers the point (or county polygon) plus MARGIN_M around it, the
fit error and the map's drawing precision; a colour counts when it covers at least MIN_PIXELS pixels there.
The zone is decided only when exactly one load zone colour is present; several zones, or a zone next to gray,
leave the subject unassigned with the colours seen.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import date

import numpy as np
from rasterio.features import rasterize
from rasterio.io import MemoryFile
from scipy import ndimage, optimize

from marden.collectors.rates_common import ParseError

MAP_URL = "https://www.ercot.com/files/assets/2023/06/05/ERCOT-Maps_Load-Zone.jpg"
MAP_PAGE_URL = "https://www.ercot.com/news/mediakit/maps"
MAP_POSTED = date(2023, 6, 5)  # the asset path date; Last-Modified: Mon, 05 Jun 2023 18:43:00 GMT
MAP_SHA256 = "1665957408e850d84cb624948b294f73f3e762fc5a978016a31f419312e7dae2"
MAP_SRID = 3081
# col = a*x + b*y + c, row = d*x + e*y + f for EPSG:3081 x, y (metres); fitted by fit_affine on 2026-10-21.
AFFINE = (0.0031431748819500324, 2.3097593419732867e-05, -1064.4071206368712,
          2.8868157565684345e-05, -0.0031821807066781593, 5086.274301408085)
FIT_RESIDUAL_PX = 3.2
OUTSIDE = "outside_ercot"
ZONE_COLORS = {  # map fill colours (the legend dots are drawn lighter)
    "LZ_NORTH": (43, 94, 160), "LZ_WEST": (66, 130, 217), "LZ_SOUTH": (44, 164, 102),
    "LZ_HOUSTON": (114, 55, 173), "LZ_AEN": (138, 12, 86), "LZ_CPS": (1, 55, 102),
    "LZ_LCRA": (255, 207, 0), "LZ_RAYBN": (255, 126, 0), OUTSIDE: (124, 133, 140),
}
LABELS = tuple(ZONE_COLORS)
COLOR_TOLERANCE = 30.0  # half the distance between the two closest fills (North / West, 71)
MARGIN_M = 2000.0
MIN_PIXELS = 3


def decode(data: bytes) -> np.ndarray:
    """The JPEG as an (rows, cols, 3) uint8 array."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # NotGeoreferencedWarning: the georeference is AFFINE
            with MemoryFile(data) as mem, mem.open() as src:
                bands = src.read()
    except Exception as exc:  # rasterio raises its own error types for undecodable bytes
        raise ParseError(f"ERCOT load zone map is not a readable image: {type(exc).__name__}") from None
    if bands.ndim != 3 or bands.shape[0] < 3:
        raise ParseError(f"ERCOT load zone map has an unexpected shape {bands.shape}")
    return np.ascontiguousarray(np.transpose(bands[:3], (1, 2, 0)))


def to_pixel(x: float, y: float, affine: tuple[float, ...] = AFFINE) -> tuple[float, float]:
    a, b, c, d, e, f = affine
    return a * x + b * y + c, d * x + e * y + f


def margin_px(affine: tuple[float, ...] = AFFINE, margin_m: float = MARGIN_M) -> int:
    return max(1, round(margin_m * float(np.hypot(affine[0], affine[3]))))


def label(pixels: np.ndarray) -> np.ndarray:
    """Index into LABELS of the nearest fill colour within COLOR_TOLERANCE, else -1."""
    palette = np.array([ZONE_COLORS[n] for n in LABELS], dtype=np.float32)
    dist = np.sqrt(((pixels.astype(np.float32)[..., None, :] - palette) ** 2).sum(-1))
    out = dist.argmin(-1)
    out[dist.min(-1) > COLOR_TOLERANCE] = -1
    return out


@dataclass(frozen=True)
class Reading:
    counts: dict[str, int]  # labelled pixels by zone (or OUTSIDE) within the reading area
    pixel: tuple[int, int]  # (col, row) of the point, or of the polygon's bounding-box centre
    area_px: int
    unlabelled: int = 0

    @property
    def present(self) -> dict[str, int]:
        return {k: v for k, v in sorted(self.counts.items()) if v >= MIN_PIXELS}

    def zone(self) -> tuple[str | None, str]:
        """(load zone, why) or (None, why not)."""
        present = self.present
        if not present:
            return None, "no load zone colour within the reading area"
        if set(present) == {OUTSIDE}:
            return None, "the map shows this area outside ERCOT (gray)"
        if len(present) > 1:
            return None, "the map shows several colours within the reading area: " + ", ".join(
                f"{k} {v} px" for k, v in present.items())
        return next(iter(present)), f"every labelled pixel within the reading area is {next(iter(present))}"


def _read(img: np.ndarray, mask: np.ndarray, r0: int, c0: int, pixel: tuple[int, int]) -> Reading:
    rows, cols = mask.shape
    window = img[r0:r0 + rows, c0:c0 + cols]
    if window.shape[:2] != mask.shape:
        raise ParseError(f"reading area at pixel {pixel} falls off the map")
    labels = label(window)[mask]
    counts = {LABELS[i]: int(n) for i, n in enumerate(np.bincount(labels[labels >= 0], minlength=len(LABELS)))
              if n}
    return Reading(counts, pixel, int(mask.sum()), int((labels < 0).sum()))


def at_point(img: np.ndarray, x: float, y: float, *, affine: tuple[float, ...] = AFFINE) -> Reading:
    """The colours within MARGIN_M of an EPSG:3081 point."""
    col, row = (round(v) for v in to_pixel(x, y, affine))
    r = margin_px(affine)
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    return _read(img, xx ** 2 + yy ** 2 <= r * r, row - r, col - r, (col, row))


def over_polygon(img: np.ndarray, rings: list[list[tuple[float, float]]], *,
                 affine: tuple[float, ...] = AFFINE) -> Reading:
    """The colours inside a polygon (EPSG:3081 outer rings) grown by MARGIN_M."""
    px = [[to_pixel(x, y, affine) for x, y in ring] for ring in rings]
    r = margin_px(affine)
    cols = [p[0] for ring in px for p in ring]
    rows = [p[1] for ring in px for p in ring]
    c0, r0 = int(min(cols)) - r - 1, int(min(rows)) - r - 1
    shape = (int(max(rows)) + r + 2 - r0, int(max(cols)) + r + 2 - c0)
    shapes = [({"type": "Polygon", "coordinates": [[(c - c0, rr - r0) for c, rr in ring]]}, 1) for ring in px]
    mask = rasterize(shapes, out_shape=shape, fill=0, dtype="uint8").astype(bool)
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    mask = ndimage.binary_dilation(mask, structure=xx ** 2 + yy ** 2 <= r * r)
    centre = (c0 + shape[1] // 2, r0 + shape[0] // 2)
    return _read(img, mask, r0, c0, centre)


def fit_affine(img: np.ndarray, rings: list[np.ndarray], start: tuple[float, ...],
               *, cap_steps: tuple[float, ...] = (60.0, 30.0, 15.0, 8.0)) -> tuple[tuple[float, ...], float]:
    """Fit AFFINE: move EPSG:3081 county boundaries (`rings`, densified to every ~2 km) onto the map's white
    county lines (truncated chamfer distance, Nelder-Mead, coarse to fine). `start` is a first guess (e.g. from
    the bounding boxes). Returns the affine and the median residual in pixels."""
    total = img.astype(int).sum(axis=2)
    land = (total < 3 * 235) & (total > 3 * 30)
    interior = ndimage.binary_erosion(ndimage.binary_fill_holes(ndimage.binary_dilation(land, iterations=4)),
                                      iterations=6)
    lines = (total >= 3 * 200) & interior
    dist = ndimage.distance_transform_edt(~lines)
    pts = np.concatenate(rings)
    rows, cols = img.shape[:2]

    def residuals(p: np.ndarray) -> np.ndarray:
        a, b, c, d, e, f = p
        x, y = pts[:, 0] * a + pts[:, 1] * b + c, pts[:, 0] * d + pts[:, 1] * e + f
        ok = (x >= 0) & (x < cols - 1) & (y >= 0) & (y < rows - 1)
        inside = ndimage.map_coordinates(interior.astype(float), [y[ok], x[ok]], order=0) > 0.5
        return ndimage.map_coordinates(dist, [y[ok][inside], x[ok][inside]], order=1)

    best = np.array(start, dtype=float)
    sx, sy = abs(best[0]) * 0.01, abs(best[4]) * 0.01  # search steps: 1% of the scale, 20 px of offset
    scale = np.array([sx, sx, 20.0, sy, sy, 20.0])
    for cap in cap_steps:
        res = optimize.minimize(lambda z, cap=cap, base=best: float(np.minimum(residuals(base + z * scale),
                                                                               cap).mean()),
                                np.zeros(6), method="Nelder-Mead", options={"xatol": 1e-4, "fatol": 1e-4,
                                                                             "maxiter": 6000})
        best = best + res.x * scale
    return tuple(float(v) for v in best), float(np.median(residuals(best)))
