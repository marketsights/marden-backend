"""Where each subject of the grid spatial join is, and which grid regions it falls in (used by grid_join).

A subject is placed by, in order: its own point (campus, site, plant; a building's footprint), a stand-in point
(a building's campus point; a site's one approved linked campus with a point; a plant's one served campus with a
point in the same state), or its county polygon (`us_county`, from census_counties) when it names one county of
one US state and has no point. For a county, each intersecting region's share of the county area is measured
(`share`); regions under MIN_SHARE are dropped as edge noise between generalized boundaries.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from marden.collectors.census_counties import county_key
from marden.core.db import Conn, Row

MIN_SHARE, FULL_SHARE = 0.02, 0.98  # county basis: share of the county's area inside a region

KINDS = "('interconnection', 'iso_rto', 'balancing_authority', 'utility', 'reliability_region')"

SUBJECTS = """
SELECT 'campus' AS subject_type, id AS subject_id, geom AS pt, 'campus point' AS basis, true AS own
FROM campus WHERE geom IS NOT NULL
UNION ALL
SELECT 'building', b.id, ST_PointOnSurface(b.footprint), 'point on the building footprint', true
FROM building b WHERE b.footprint IS NOT NULL
UNION ALL
SELECT 'building', b.id, c.geom, 'campus point (building has no footprint)', true
FROM building b JOIN campus c ON c.id = b.campus_id WHERE b.footprint IS NULL AND c.geom IS NOT NULL
UNION ALL
SELECT 'site', slug, geom, 'site point', true FROM site WHERE geom IS NOT NULL
UNION ALL
SELECT 'site', l.site_slug, c.geom, 'approved linked campus ' || c.id || ' point (site has no point)', false
FROM site s JOIN site_link l ON l.site_slug = s.slug AND l.status = 'approved' JOIN campus c ON c.id = l.campus_id
WHERE s.geom IS NULL AND c.geom IS NOT NULL
  AND (SELECT count(*) FROM site_link l2 JOIN campus c2 ON c2.id = l2.campus_id
       WHERE l2.site_slug = s.slug AND l2.status = 'approved' AND c2.geom IS NOT NULL) = 1
UNION ALL
SELECT 'plant', id, geom, 'plant point', true FROM power_plant WHERE geom IS NOT NULL
UNION ALL
SELECT 'plant', p.id, c.geom, 'served campus ' || c.id || ' point (plant has no location)', false
FROM power_plant p JOIN plant_serves ps ON ps.plant_id = p.id JOIN campus c ON c.id = ps.campus_id
WHERE p.geom IS NULL AND c.geom IS NOT NULL AND (p.state IS NULL OR upper(trim(p.state)) = upper(trim(c.state)))
  AND (SELECT count(*) FROM plant_serves ps2 JOIN campus c2 ON c2.id = ps2.campus_id
       WHERE ps2.plant_id = p.id AND c2.geom IS NOT NULL) = 1
"""
MATCHES = f"""
WITH s AS ({SUBJECTS})
SELECT s.subject_type, s.subject_id, s.basis, s.own, ST_X(s.pt) AS lon, ST_Y(s.pt) AS lat,
       r.id AS region_id, r.kind, r.name, r.parent_id, r.meta, NULL::float AS share
FROM s LEFT JOIN grid_region r ON r.geom IS NOT NULL AND ST_Intersects(r.geom, s.pt) AND r.kind IN {KINDS}
ORDER BY s.subject_type, s.subject_id, r.kind, r.id
"""
LOCATED = """
SELECT 'site' AS subject_type, slug AS subject_id, state, county FROM site
UNION ALL SELECT 'campus', id, state, county FROM campus
UNION ALL SELECT 'plant', id, state, county FROM power_plant
"""
AREA_MATCHES = f"""
WITH u AS (SELECT * FROM unnest(%s::text[], %s::text[], %s::text[]) AS t(subject_type, subject_id, geoid)),
c AS (SELECT u.*, k.namelsad, k.stusps, k.meta AS county_meta, k.geom, ST_Area(k.geom::geography) AS area,
             ST_PointOnSurface(k.geom) AS pt
      FROM u JOIN us_county k USING (geoid))
SELECT c.subject_type, c.subject_id, c.geoid, c.namelsad, c.stusps, c.county_meta, ST_X(c.pt) AS lon,
       ST_Y(c.pt) AS lat, r.id AS region_id, r.kind, r.name, r.parent_id, r.meta,
       ST_Area(ST_Intersection(r.geom, c.geom)::geography) / nullif(c.area, 0) AS share
FROM c LEFT JOIN grid_region r ON r.geom IS NOT NULL AND r.geom && c.geom AND ST_Intersects(r.geom, c.geom)
 AND r.kind IN {KINDS}
ORDER BY c.subject_type, c.subject_id, r.kind, r.id
"""
ISO_INTERCONNECTIONS = """
SELECT i.id, array_agg(DISTINCT b.parent_id) FILTER (WHERE b.parent_id IS NOT NULL) AS inter
FROM grid_region i CROSS JOIN LATERAL jsonb_array_elements_text(coalesce(i.meta -> 'ba_codes', '[]')) AS c(code)
JOIN grid_region b ON b.id = 'ba:' || c.code
WHERE i.kind = 'iso_rto' GROUP BY i.id
"""


@dataclass
class Subject:
    subject_type: str
    subject_id: str
    basis: str
    lon: float
    lat: float
    regions: dict[str, list[Row]]
    own: bool = True  # False: a stand-in point (no utility facts)
    county: str | None = None  # county basis: 'Winkler County, TX (us_county 48495, Census 2025 1:5M, doc 7)'
    iso_inter: dict[str, list[str]] = field(default_factory=dict)  # ISO region id -> its interconnections

    def where(self) -> str:
        if self.county:
            return f"county polygon {self.county}, {self.basis}"
        return f"point ({self.lon:.5f}, {self.lat:.5f}), {self.basis}"


def _county_label(r: Row) -> str:
    m = r["county_meta"] or {}
    return (f"{r['namelsad']}, {r['stusps']} (us_county {r['geoid']}, Census {m.get('vintage')} "
            f"{m.get('scale')}, document {m.get('document_id')})")


def county_subjects(conn: Conn, located: set[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """(subject_type, subject_id, geoid) for subjects without a point that name one county of one state."""
    index: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in conn.execute("SELECT geoid, stusps, name, namelsad FROM us_county").fetchall():
        for text in (r["name"], r["namelsad"]):
            index[(r["stusps"], county_key(text))].add(r["geoid"])
    out = []
    for r in conn.execute(LOCATED).fetchall():
        state, county = (r["state"] or "").strip().upper(), r["county"] or ""
        if (r["subject_type"], r["subject_id"]) in located or len(state) != 2 or not county.strip():
            continue
        if any(sep in county.lower() for sep in (";", "/", ",", "(", " or ", " and ", "multiple")):
            continue  # several counties, or a hedged one: no single polygon
        geoids = index.get((state, county_key(county)), set())
        if len(geoids) == 1:
            out.append((r["subject_type"], r["subject_id"], next(iter(geoids))))
    return out


def load_subjects(conn: Conn) -> list[Subject]:
    out: dict[tuple[str, str], Subject] = {}
    for r in conn.execute(MATCHES).fetchall():
        key = (r["subject_type"], r["subject_id"])
        s = out.get(key)
        if s is None:
            s = out[key] = Subject(r["subject_type"], r["subject_id"], r["basis"], r["lon"], r["lat"],
                                   defaultdict(list), r["own"])
        if r["region_id"] is not None:
            s.regions[r["kind"]].append(r)
    areas = county_subjects(conn, set(out))
    if areas:
        rows = conn.execute(AREA_MATCHES, ([a[0] for a in areas], [a[1] for a in areas],
                                           [a[2] for a in areas])).fetchall()
        for r in rows:
            key = (r["subject_type"], r["subject_id"])
            s = out.get(key)
            if s is None:
                s = out[key] = Subject(r["subject_type"], r["subject_id"], "subject has a county but no point",
                                       r["lon"], r["lat"], defaultdict(list), True, _county_label(r))
            if r["region_id"] is not None and (r["share"] or 0) >= MIN_SHARE:
                s.regions[r["kind"]].append(r)
    inter = {r["id"]: r["inter"] or [] for r in conn.execute(ISO_INTERCONNECTIONS).fetchall()}
    for s in out.values():
        s.iso_inter = inter
    return list(out.values())
