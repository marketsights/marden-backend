"""Sentinel-2 L2A scene search and selection on the Planetary Computer STAC API (pure functions).

- The area of interest is a ~1.5 km box (750 m each side of the subject's point).
- A search lists every L2A scene over the box with tile cloud cover < 20 % since the first day of
  the month 17 months before this one (18 calendar months including the current one).
- Scenes are compared on one pixel grid, so one MGRS tile is used per box: the tile with the most
  scenes whose data footprint contains the whole box (ties: the lower tile id).
- Each month's candidates are ordered by tile cloud cover, then newest first; the collector reads
  their scene classification (SCL) inside the box in that order and keeps the clearest one.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote, urlencode

from marden.collectors.rates_common import ParseError

STAC_ROOT = "https://planetarycomputer.microsoft.com/api/stac/v1"
DATA_ROOT = "https://planetarycomputer.microsoft.com/api/data/v1"
COLLECTION = "sentinel-2-l2a"
MAX_CLOUD_PCT = 20
BOX_HALF_M = 750.0
MONTHS = 18
PAGE_LIMIT = 1000
FINAL_LAG_DAYS = 5  # a month's pick is final once PC has had this long to ingest its last passes
METERS_PER_DEG_LAT = 111_320.0
SEARCH_FIELDS = (
    "id", "geometry", "properties.datetime", "properties.eo:cloud_cover", "properties.s2:mgrs_tile",
    "assets.SCL.href", "-links",
)


@dataclass(frozen=True)
class Box:
    west: float
    south: float
    east: float
    north: float

    @classmethod
    def around(cls, lon: float, lat: float, half_m: float = BOX_HALF_M) -> Box:
        dlat = half_m / METERS_PER_DEG_LAT
        dlon = half_m / (METERS_PER_DEG_LAT * math.cos(math.radians(lat)))
        return cls(round(lon - dlon, 5), round(lat - dlat, 5), round(lon + dlon, 5), round(lat + dlat, 5))

    def bounds(self) -> tuple[float, float, float, float]:
        return (self.west, self.south, self.east, self.north)

    def text(self) -> str:
        return ",".join(f"{v:.5f}" for v in self.bounds())

    def corners(self) -> list[tuple[float, float]]:
        return [(self.west, self.south), (self.east, self.south), (self.east, self.north), (self.west, self.north)]


@dataclass(frozen=True)
class Scene:
    id: str
    acquired: datetime
    cloud_pct: float
    tile: str
    contains_box: bool
    scl_href: str | None

    @property
    def day(self) -> date:
        return self.acquired.date()

    @property
    def month(self) -> str:
        return self.acquired.strftime("%Y-%m")


# ---------------------------------------------------------------- dates
def window_start(today: date, months: int = MONTHS) -> date:
    """First day of the month `months - 1` months before `today`'s month."""
    index = today.year * 12 + today.month - 1 - (months - 1)
    return date(index // 12, index % 12 + 1, 1)


def shift_month(month: str, delta: int) -> str:
    year, mon = (int(p) for p in month.split("-"))
    index = year * 12 + mon - 1 + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_end(month: str) -> date:
    nxt = shift_month(month, 1)
    return date.fromordinal(date(int(nxt[:4]), int(nxt[5:]), 1).toordinal() - 1)


def month_final(month: str, today: date, lag_days: int = FINAL_LAG_DAYS) -> bool:
    """No further scene of `month` is expected: its last day plus the ingest lag has passed."""
    return (today - month_end(month)).days > lag_days


def base_months(month: str) -> list[str]:
    """Months whose pick a month is compared with, in order of preference (~3 months earlier)."""
    return [shift_month(month, -3), shift_month(month, -4), shift_month(month, -2)]


# ---------------------------------------------------------------- URLs
def search_url(box: Box, start: date) -> str:
    """GET /search for the box since `start` (open-ended, so the URL is stable within a month)."""
    params = {
        "collections": COLLECTION,
        "bbox": box.text(),
        "datetime": f"{start.isoformat()}T00:00:00Z/..",
        "filter": f"eo:cloud_cover < {MAX_CLOUD_PCT}",
        "filter-lang": "cql2-text",
        "sortby": "datetime,id",
        "limit": str(PAGE_LIMIT),
        "fields": ",".join(SEARCH_FIELDS),
    }
    return f"{STAC_ROOT}/search?{urlencode(params)}"


def item_url(scene_id: str) -> str:
    return f"{STAC_ROOT}/collections/{COLLECTION}/items/{quote(scene_id, safe='')}"


def imagery_url(scene_id: str, box: Box) -> str:
    """The PC data API's true-colour rendering of the scene cropped to the box (no token in the URL:
    the data API reads the blobs itself). Same rendering as the item's `rendered_preview` asset."""
    params = {
        "collection": COLLECTION,
        "item": scene_id,
        "assets": "visual",
        "asset_bidx": "visual|1,2,3",
        "nodata": "0",
    }
    return f"{DATA_ROOT}/item/bbox/{box.text()}.png?{urlencode(params)}"


def caption(day: date, clear_fraction: float, view_km: float = 1.5) -> str:
    """The image's caption: its day, the view's size (its longer side) and how clear the scene is over the
    ~1.5 km box the pick was ranked on."""
    return (f"Sentinel-2 L2A, {day.day} {day:%b %Y}: {view_km:g} km view, {clear_fraction:.0%} cloud-free. "
            f"Contains modified Copernicus Sentinel data {day.year}")


def view_km(box: Box) -> float:
    """The box's longer side in km, to 0.1."""
    lat = math.radians((box.south + box.north) / 2)
    w = (box.east - box.west) * METERS_PER_DEG_LAT * math.cos(lat)
    h = (box.north - box.south) * METERS_PER_DEG_LAT
    return round(max(w, h) / 1000, 1)


# ---------------------------------------------------------------- parse
def _inside(lon: float, lat: float, ring: list[list[float]]) -> bool:
    """Ray casting; points on an edge may go either way, which only matters for a box on a footprint edge."""
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def contains_box(geometry: dict[str, Any] | None, box: Box) -> bool:
    """Whether a footprint (Polygon or MultiPolygon, lon/lat) contains all four corners of the box."""
    if not geometry:
        return False
    if geometry.get("type") == "Polygon":
        polygons = [geometry.get("coordinates") or []]
    elif geometry.get("type") == "MultiPolygon":
        polygons = geometry.get("coordinates") or []
    else:
        return False
    for rings in polygons:
        if rings and all(_inside(x, y, rings[0]) for x, y in box.corners()) and not any(
            any(_inside(x, y, hole) for x, y in box.corners()) for hole in rings[1:]
        ):
            return True
    return False


def _acquired(text: Any) -> datetime:
    if not isinstance(text, str):
        raise ParseError(f"scene datetime is not a string: {text!r}")
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ParseError(f"bad scene datetime {text!r}") from None
    return when.astimezone(UTC) if when.tzinfo else when.replace(tzinfo=UTC)


def parse_search(data: bytes, box: Box) -> tuple[list[Scene], str | None]:
    """One /search page -> (scenes, next page URL or None)."""
    try:
        payload = json.loads(data)
    except ValueError as exc:
        raise ParseError(f"STAC search response is not JSON: {exc}") from None
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise ParseError(f"STAC search response is not a FeatureCollection: {str(payload)[:200]}")
    features = payload.get("features")
    if not isinstance(features, list):
        raise ParseError("STAC search response has no features list")
    scenes = []
    for f in features:
        props = f.get("properties") or {}
        sid, cloud, tile = f.get("id"), props.get("eo:cloud_cover"), props.get("s2:mgrs_tile")
        if not isinstance(sid, str) or not sid or not isinstance(tile, str):
            raise ParseError(f"STAC feature without id or s2:mgrs_tile: {str(f)[:200]}")
        if isinstance(cloud, bool) or not isinstance(cloud, int | float):
            raise ParseError(f"{sid}: eo:cloud_cover is not a number: {cloud!r}")
        scl = ((f.get("assets") or {}).get("SCL") or {}).get("href")
        scenes.append(Scene(sid, _acquired(props.get("datetime")), float(cloud), tile,
                            contains_box(f.get("geometry"), box), scl if isinstance(scl, str) else None))
    nxt = next((link.get("href") for link in payload.get("links") or []
                if link.get("rel") == "next" and link.get("method", "GET") == "GET"), None)
    return scenes, nxt


# ---------------------------------------------------------------- select
def choose_tile(scenes: list[Scene]) -> str | None:
    counts = Counter(s.tile for s in scenes if s.contains_box and s.scl_href)
    if not counts:
        return None
    return min(counts, key=lambda t: (-counts[t], t))


def candidates_by_month(scenes: list[Scene], tile: str) -> dict[str, list[Scene]]:
    """month -> the tile's scenes that contain the box, lowest cloud cover first, then newest."""
    out: dict[str, list[Scene]] = {}
    seen: set[str] = set()
    for s in scenes:
        if s.tile == tile and s.contains_box and s.scl_href and s.id not in seen:
            seen.add(s.id)
            out.setdefault(s.month, []).append(s)
    for month in out:
        out[month].sort(key=lambda s: (s.cloud_pct, -s.acquired.timestamp(), s.id))
    return dict(sorted(out.items()))


def parse_item(data: bytes) -> dict[str, Any]:
    """A stored item JSON -> {id, acquired, baseline, hrefs{SCL,B04,B08,B11}}."""
    try:
        item = json.loads(data)
    except ValueError as exc:
        raise ParseError(f"STAC item is not JSON: {exc}") from None
    props = item.get("properties") or {}
    assets = item.get("assets") or {}
    hrefs = {b: (assets.get(b) or {}).get("href") for b in ("SCL", "B04", "B08", "B11")}
    if not item.get("id") or not all(isinstance(h, str) for h in hrefs.values()):
        raise ParseError(f"STAC item {item.get('id')!r} lacks the SCL/B04/B08/B11 assets")
    baseline = props.get("s2:processing_baseline")
    if not isinstance(baseline, str):
        raise ParseError(f"STAC item {item['id']} has no s2:processing_baseline")
    return {"id": item["id"], "acquired": _acquired(props.get("datetime")), "baseline": baseline, "hrefs": hrefs,
            "cloud_pct": props.get("eo:cloud_cover"), "tile": props.get("s2:mgrs_tile")}
