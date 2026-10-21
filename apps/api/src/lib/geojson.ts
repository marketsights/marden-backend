// GeoJSON for the Data centers and On-site power maps: one Point per row, the row as properties.
import type * as GeoJSON from 'geojson';

interface Located { id: string; lat: number; lon: number }

const located = (row: unknown): row is Located & Record<string, unknown> => {
  const r = row as Partial<Located> | null;
  return !!r && typeof r.id === 'string' && Number.isFinite(r.lat) && Number.isFinite(r.lon);
};

/** Rows without coordinates are left off the map (they still appear in the table). */
export function toFeatureCollection(
  rows: unknown[],
  meta: { asOf: string | null; total: number },
): GeoJSON.FeatureCollection<GeoJSON.Point> & { asOf: string | null; total: number } {
  const features = rows.filter(located).map(
    (row): GeoJSON.Feature<GeoJSON.Point> => ({
      type: 'Feature',
      id: row.id,
      geometry: { type: 'Point', coordinates: [row.lon, row.lat] },
      properties: row,
    }),
  );
  return { type: 'FeatureCollection', features, asOf: meta.asOf, total: meta.total };
}
