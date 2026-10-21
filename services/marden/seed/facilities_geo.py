"""County and state lookup for the `facilities` loader (Census boundaries in the archive).

The archive ships us-atlas 3.0.1 (`aiwc/q3x/geo/us-atlas-3.0.1/counties-10m.json`, ISC licence,
US Census Bureau cartographic boundaries). The loader decodes that TopoJSON (quantized,
delta-encoded arcs) and finds the county that contains a campus point with an even-odd ray test.
The result is a Marden derivation (tag M, ``COUNTY_METHOD``). Nothing in the file is executed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

COUNTIES = "aiwc/q3x/geo/us-atlas-3.0.1/counties-10m.json"
COUNTY_METHOD = "us-atlas-3.0.1-counties-10m-point-in-polygon"

US_STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN",
    "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO",
    "Montana": "MT", "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ",
    "New Mexico": "NM", "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH",
    "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI",
    "Wyoming": "WY", "Puerto Rico": "PR",
}
US_CODES = frozenset(US_STATES.values())

Ring = list[tuple[float, float]]


def state_code(state: str | None) -> str | None:
    """'Texas' / 'TX' -> 'TX'; None for anything that is not a US state name or code."""
    if not state:
        return None
    s = state.strip()
    if s.upper() in US_CODES:
        return s.upper()
    return US_STATES.get(s)


@dataclass(frozen=True)
class County:
    fips: str
    name: str
    state: str | None  # USPS code
    bbox: tuple[float, float, float, float]
    polygons: tuple[tuple[Ring, ...], ...]  # polygon -> rings (outer first, then holes)

    def contains(self, lon: float, lat: float) -> bool:
        x0, y0, x1, y1 = self.bbox
        if not (x0 <= lon <= x1 and y0 <= lat <= y1):
            return False
        return any(_inside(lon, lat, rings) for rings in self.polygons)


def _inside(x: float, y: float, rings: Iterable[Ring]) -> bool:
    """Even-odd rule over all rings of one polygon (holes flip the parity back)."""
    inside = False
    for ring in rings:
        n = len(ring)
        j = n - 1
        for i in range(n):
            xi, yi = ring[i]
            xj, yj = ring[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


def _decode_arcs(topo: dict[str, Any]) -> list[Ring]:
    (sx, sy), (tx, ty) = topo["transform"]["scale"], topo["transform"]["translate"]
    out: list[Ring] = []
    for arc in topo["arcs"]:
        x = y = 0
        pts: Ring = []
        for dx, dy in arc:
            x += dx
            y += dy
            pts.append((x * sx + tx, y * sy + ty))
        out.append(pts)
    return out


def _ring(arcs: list[Ring], refs: list[int]) -> Ring:
    pts: Ring = []
    for ref in refs:
        seg = arcs[ref] if ref >= 0 else list(reversed(arcs[~ref]))
        pts.extend(seg if not pts else seg[1:])  # consecutive arcs share their joining point
    return pts


def counties(topo: dict[str, Any]) -> list[County]:
    """Every county polygon of the us-atlas topology, with its state code from the FIPS prefix."""
    arcs = _decode_arcs(topo)
    state_names = {g["id"]: g["properties"]["name"] for g in topo["objects"]["states"]["geometries"]}
    out: list[County] = []
    for g in topo["objects"]["counties"]["geometries"]:
        if g.get("type") == "Polygon":
            polys = [g["arcs"]]
        elif g.get("type") == "MultiPolygon":
            polys = g["arcs"]
        else:
            continue
        rings = tuple(tuple(_ring(arcs, r) for r in poly) for poly in polys)
        xs = [p[0] for poly in rings for r in poly for p in r]
        ys = [p[1] for poly in rings for r in poly for p in r]
        fips = str(g["id"])
        out.append(County(fips, g["properties"]["name"], US_STATES.get(state_names.get(fips[:2], "")),
                          (min(xs), min(ys), max(xs), max(ys)), rings))
    return out


def county_at(index: list[County], lon: float | None, lat: float | None) -> County | None:
    if lon is None or lat is None:
        return None
    for c in index:
        if c.contains(lon, lat):
            return c
    return None
