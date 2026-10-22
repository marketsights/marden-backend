// GET /api/export/data-centers.xlsx: spec 14 acceptance "Excel exports the filtered rows (campus
// sheet, building sheet, sources sheet)". The campus sheet is the table's rows; this is the building
// sheet for the same campuses, in the table's order, read from each campus's `dc_tab:buildings`
// read model as of the same date (the expanded row and the campus Buildings tab read it too).
import type { Db } from '../db.js';

type Json = Record<string, unknown>;

/** The building columns, as the expanded row and the Buildings tab show them (no footprint geometry). */
const FIELDS = ['label', 'sqft', 'facilityMw', 'itMw', 'status', 'buildingPermit', 'coDate', 'firstVisible', 'inService', 'changeScore'] as const;

export async function campusBuildingRows(db: Db, campuses: Json[], asOf: string | null): Promise<Json[]> {
  const ids = campuses.map((c) => c.id).filter((id): id is string => typeof id === 'string');
  if (ids.length === 0) return [];
  const res = await db.query<{ key: string; doc: unknown }>(
    `SELECT DISTINCT ON (key) key, doc FROM read_model
      WHERE view = 'dc_tab:buildings' AND key = ANY($1::text[]) AND ($2::date IS NULL OR as_of <= $2::date)
      ORDER BY key, as_of DESC`,
    [ids, asOf],
  );
  const byCampus = new Map(res.rows.map((r) => [r.key, Array.isArray(r.doc) ? (r.doc as Json[]) : []]));
  const out: Json[] = [];
  for (const campus of campuses) {
    const id = campus.id as string;
    for (const b of byCampus.get(id) ?? []) {
      const row: Json = { id: b.id, campusId: id, campus: campus.name };
      for (const f of FIELDS) if (f in b) row[f] = b[f];
      out.push(row);
    }
  }
  return out;
}
