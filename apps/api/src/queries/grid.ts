// ISO/RTO boundaries for the Data centers map layer (spec 14 Map view: "Layers (toggle): ISO/RTO
// boundaries"), from the `grid_region` rows the HIFLD collector writes (kind 'iso_rto', US only).
// The source polygons hold about a million points; the map needs a few thousand, so the geometry is
// simplified in PostGIS (0.02 degrees, about 2 km) and the result is kept until the rows change.
import type { IsoBoundaries, IsoBoundaryProps } from '../contracts/app.js';
import type { Db } from '../db.js';
import { businessDay } from '../lib/business-day.js';

/** Simplification tolerance in degrees and coordinate precision (3 decimals, about 100 m). */
export const SIMPLIFY_DEGREES = 0.02;
const DECIMALS = 3;

interface BoundaryRow {
  id: string;
  name: string;
  label: string | null;
  as_of: string | null;
  source_url: string | null;
  updated: Date;
  label_point: { coordinates: [number, number] } | null;
  geometry: IsoBoundaries['features'][number]['geometry'];
}

let cache: { stamp: string; body: IsoBoundaries } | null = null;

/** Clears the in-memory copy (tests). */
export function resetIsoBoundaryCache(): void {
  cache = null;
}

function feature(row: BoundaryRow): IsoBoundaries['features'][number] {
  const label = row.label ?? row.id.replace(/^iso:/, '');
  const properties: IsoBoundaryProps = {
    id: row.id,
    label,
    name: row.name,
    labelPoint: row.label_point?.coordinates ?? null,
    source: {
      value: row.name,
      tag: 'F',
      sourceName: 'HIFLD Independent System Operators',
      ...(row.source_url ? { sourceUrl: row.source_url } : {}),
      asOf: row.as_of ?? businessDay(row.updated), // the US Eastern day of the update, not its UTC date
    },
  };
  return { type: 'Feature', id: row.id, geometry: row.geometry, properties };
}

/** US ISO/RTO service areas with a polygon, simplified; the same object while grid_region is unchanged. */
export async function isoBoundaries(db: Db): Promise<IsoBoundaries> {
  const stampRes = await db.query<{ stamp: string }>(
    `SELECT count(*)::text || '|' || coalesce(max(updated_at)::text, '') AS stamp
       FROM grid_region WHERE kind = 'iso_rto' AND country = 'US' AND geom IS NOT NULL`,
  );
  const stamp = stampRes.rows[0]?.stamp ?? '';
  if (cache && cache.stamp === stamp) return cache.body;
  const res = await db.query<BoundaryRow>(
    `SELECT id, name, meta ->> 'label' AS label, meta #>> '{hifld,as_of}' AS as_of,
            meta #>> '{hifld,source_url}' AS source_url, updated_at AS updated,
            -- One label per ISO/RTO: a point inside its largest part (not one per island or exclave).
            (SELECT ST_AsGeoJSON(ST_PointOnSurface(d.geom), $2)::jsonb FROM ST_Dump(geom) d
              ORDER BY ST_Area(d.geom) DESC LIMIT 1) AS label_point,
            ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom, $1), $2)::jsonb AS geometry
       FROM grid_region WHERE kind = 'iso_rto' AND country = 'US' AND geom IS NOT NULL
      ORDER BY id`,
    [SIMPLIFY_DEGREES, DECIMALS],
  );
  const body: IsoBoundaries = { type: 'FeatureCollection', features: res.rows.map(feature) };
  cache = { stamp, body };
  return body;
}
