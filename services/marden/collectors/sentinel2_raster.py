"""Windowed reads of Sentinel-2 L2A COGs and the change score (method change-score@0.1).

Reads: the box is cut from each band's COG with rasterio (HTTP range reads of the tiles that cover
it; the ~100-200 MB files are never downloaded). Hrefs are signed with `planetary_computer` in
memory only: SAS tokens may not be shared, so a signed URL is never stored or logged.
The 20 m grid (SCL, B11) is the reference; the 10 m bands (B04, B08) are read over exactly twice
that window and averaged 2 x 2, so all bands sit on the same 20 m pixels.

change-score@0.1: mean absolute difference of bottom-of-atmosphere reflectance in B04 (red),
B08 (NIR) and B11 (SWIR 1.6 um), averaged over the three bands, over the pixels both scenes classify
as clear land or water (SCL 2, 4, 5, 6, 7) with valid data in every band. Not computed when fewer
than half of the box's pixels are clear in both scenes. Reflectance = (DN + offset) / 10000 with
offset -1000 from processing baseline 04.00 on (ESA BOA_ADD_OFFSET), 0 before.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

import numpy as np

from marden.collectors.sentinel2_stac import Box
from marden.core import http
from marden.core.http import FetchError

log = logging.getLogger(__name__)

METHOD_VERSION = "change-score@0.1"
SCORE_BANDS = ("B04", "B08", "B11")
TEN_M = frozenset({"B04", "B08"})
VALID_SCL = frozenset({2, 4, 5, 6, 7})  # dark area, vegetation, not vegetated, water, unclassified
CLEAR_ENOUGH = 0.98  # a month's first candidate this clear inside the box is kept without reading more
MIN_OVERLAP = 0.5
BLOB_HOST = "blob.core.windows.net"
GDAL_OPTIONS = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MAX_RETRY": "4",
    "GDAL_HTTP_RETRY_DELAY": "2",
    "GDAL_HTTP_TIMEOUT": "60",
    "VSI_CACHE": "TRUE",
}

http.set_rate_limit(BLOB_HOST, 4.0)


class ReadError(RuntimeError):
    """A COG window could not be read (counted per scene; the scene is skipped)."""


class SceneReader(Protocol):
    def read(self, hrefs: Mapping[str, str], box: Box) -> dict[str, np.ndarray]:
        """band -> raw DN array over the box. `hrefs` must include SCL (the 20 m reference grid)."""
        ...


@dataclass(frozen=True)
class ChangeResult:
    score: float
    valid_pixels: int
    valid_fraction: float


# ---------------------------------------------------------------- pure functions
def clear_mask(scl: np.ndarray) -> np.ndarray:
    return np.isin(scl, sorted(VALID_SCL))


def clear_fraction(scl: np.ndarray) -> float:
    return float(clear_mask(scl).mean()) if scl.size else 0.0


def boa_offset(baseline: str) -> int:
    try:
        major = float(baseline)
    except ValueError:
        raise ValueError(f"unknown processing baseline {baseline!r}") from None
    return -1000 if major >= 4.0 else 0


def _to_20m(band: np.ndarray, shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    """A 10 m window (2x the 20 m one) -> (mean DN on the 20 m grid, all four sub-pixels valid)."""
    h, w = shape
    if band.shape != (2 * h, 2 * w):
        raise ValueError(f"10 m window {band.shape} is not twice the 20 m window {shape}")
    blocks = band.astype(np.float64).reshape(h, 2, w, 2)
    valid = (band.reshape(h, 2, w, 2) > 0).all(axis=(1, 3))
    return blocks.mean(axis=(1, 3)), valid


def reflectance(raw: Mapping[str, np.ndarray], baseline: str) -> tuple[np.ndarray, np.ndarray]:
    """Raw windows -> (reflectance stack (3, h, w) for SCORE_BANDS on the 20 m grid, valid mask)."""
    scl = raw["SCL"]
    valid = clear_mask(scl)
    offset = boa_offset(baseline)
    layers = []
    for band in SCORE_BANDS:
        if band in TEN_M:
            dn, ok = _to_20m(raw[band], scl.shape)
        else:
            if raw[band].shape != scl.shape:
                raise ValueError(f"{band} window {raw[band].shape} differs from SCL {scl.shape}")
            dn, ok = raw[band].astype(np.float64), raw[band] > 0
        valid &= ok
        layers.append((dn + offset) / 10000.0)
    return np.stack(layers), valid


def change_score(
    later: Mapping[str, np.ndarray], later_baseline: str, earlier: Mapping[str, np.ndarray], earlier_baseline: str,
) -> ChangeResult | None:
    """change-score@0.1 between two scenes' windows (None: too few pixels clear in both)."""
    a, va = reflectance(later, later_baseline)
    b, vb = reflectance(earlier, earlier_baseline)
    if a.shape != b.shape:
        raise ValueError(f"windows differ in shape: {a.shape} vs {b.shape}")
    valid = va & vb
    total = valid.size
    n = int(valid.sum())
    if total == 0 or n / total < MIN_OVERLAP:
        return None
    diff = np.abs(a - b).mean(axis=0)[valid]
    score = float(diff.mean())
    if not math.isfinite(score):
        return None
    return ChangeResult(round(score, 4), n, round(n / total, 4))


# ---------------------------------------------------------------- window maths
def pixel_window(bounds: tuple[float, float, float, float], transform: tuple[float, ...]) -> tuple[int, int, int, int]:
    """(col_off, row_off, width, height) of the pixels covering projected `bounds` on a north-up grid."""
    minx, miny, maxx, maxy = bounds
    res_x, x0, res_y, y0 = transform[0], transform[2], -transform[4], transform[5]
    col0, row0 = math.floor((minx - x0) / res_x), math.floor((y0 - maxy) / res_y)
    col1, row1 = math.ceil((maxx - x0) / res_x), math.ceil((y0 - miny) / res_y)
    return col0, row0, col1 - col0, row1 - row0


# ---------------------------------------------------------------- rasterio reader
class CogReader:
    """Reads band windows from signed COG hrefs (rasterio/GDAL range requests)."""

    def __init__(self, user_agent: str):
        self.user_agent = user_agent
        self.reads = 0
        self.errors = 0

    def _sign(self, href: str) -> str:
        import planetary_computer
        import requests

        try:
            return planetary_computer.sign(href)
        except requests.RequestException as exc:
            raise FetchError("https://planetarycomputer.microsoft.com/api/sas/v1/token/sentinel-2-l2a", None,
                             f"SAS token request failed: {type(exc).__name__}") from None

    def read(self, hrefs: Mapping[str, str], box: Box) -> dict[str, np.ndarray]:
        import rasterio
        from rasterio.errors import RasterioError
        from rasterio.warp import transform_bounds
        from rasterio.windows import Window

        order = ["SCL", *[b for b in hrefs if b != "SCL"]]
        out: dict[str, np.ndarray] = {}
        ref: tuple[tuple[float, ...], tuple[int, int, int, int]] | None = None
        try:
            with rasterio.Env(GDAL_HTTP_USERAGENT=self.user_agent, **GDAL_OPTIONS):
                for band in order:
                    signed = self._sign(hrefs[band])
                    http.bucket_for(urlparse(hrefs[band]).hostname or BLOB_HOST)[1].acquire()
                    self.reads += 1
                    with rasterio.open(signed) as ds:
                        if ref is None:
                            bounds = transform_bounds("EPSG:4326", ds.crs, *box.bounds())
                            ref = (tuple(ds.transform)[:6], pixel_window(bounds, tuple(ds.transform)))
                        (_, _, x0, _, _, y0), (c, r, w, h) = ref
                        if (ds.transform.c, ds.transform.f) != (x0, y0):
                            raise ReadError(f"{band} grid origin differs from SCL")
                        k = 2 if band in TEN_M else 1
                        win = Window(c * k, r * k, w * k, h * k)
                        arr = ds.read(1, window=win, boundless=True, fill_value=0)
                    out[band] = arr
        except RasterioError as exc:
            self.errors += 1
            raise ReadError(f"{type(exc).__name__}: {str(exc)[:200]}".split("?", 1)[0]) from None
        except ReadError:
            self.errors += 1
            raise
        return out
