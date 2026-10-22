// GET /api/export/data-center.xlsx?id=&tab=[&type=]: the table on the campus detail tab the user is
// looking at (spec 14 header "Excel", spec 00 "Exports": exactly the rows shown, with a sources sheet).
// Each tab reads the same read model as its own endpoint: GET /api/data-centers/:id (Overview, Power),
// /buildings, /records, /history; Power adds the plants serving the campus (GET /api/power-plants?servesId=).
import type { Db } from '../db.js';
import { badRequest, notFound, notReady } from '../errors.js';
import { queryRows, subjectExists } from '../queries/rows.js';
import { first, type Query } from './params.js';
import { PLANT_ROWS } from './row-views.js';

type Json = Record<string, unknown>;
type Rows = Json[];

const list = (v: unknown): Json[] => (Array.isArray(v) ? (v as Json[]) : []);
const obj = (v: unknown): Json => (typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Json) : {});
const num = (v: unknown): number => (typeof v === 'number' ? v : 0);
const str = (v: unknown, fallback = ''): string => (typeof v === 'string' && v !== '' ? v : typeof v === 'number' ? String(v) : fallback);

/** Record type chips of the Records tab (DocType, types.ts). */
export const DOC_TYPES = ['Permit', 'Zoning', 'Filing', 'Utility', 'Court', 'Press', 'Imagery'] as const;

// The Overview facts card's named rows, in its order (web app components/data-centers/campus-facts.tsx).
const NAMED: readonly (readonly [string, string])[] = [
  ['developer', 'Developer · operator'],
  ['ownerOfRecord', 'Owner of record'],
  ['tenant', 'Tenant'],
  ['status', 'Status'],
  ['facilityMw', 'Facility MW'],
  ['itMw', 'IT MW'],
  ['utility', 'Utility'],
  ['iso', 'ISO/RTO'],
  ['balancingAuthority', 'Balancing authority'],
  ['powerSource', 'Power source'],
  ['onlineYear', 'Online (first or expected)'],
];

/** Overview: every field with its value (sourced), then every other published fact, then the key dates. */
export function overviewRows(doc: Json): Rows {
  const named = NAMED.map(([key, field]) => ({ table: 'Facts', field, value: doc[key] ?? null }));
  const parcels = (Array.isArray(doc.parcelIds) ? (doc.parcelIds as unknown[]) : []).map((p) => str(p)).filter(Boolean);
  const buildings = obj(doc.buildings);
  const extra = [
    { table: 'Facts', field: 'Buildings', value: `${num(buildings.built)} of ${num(buildings.total)} built` },
    { table: 'Facts', field: 'Parcel IDs', value: parcels.length > 0 ? parcels.join(', ') : 'n/d' },
  ];
  const facts = Object.entries(obj(doc.facts)).map(([field, value]) => ({ table: 'Facts', field, value }));
  const dates = list(doc.keyDates).map((d) => ({ table: 'Key dates', field: d.label, value: d.date ?? null }));
  return [...named, ...extra, ...facts, ...dates];
}

/** Power: the campus's grid fields, then one row per plant serving it (units by OEM and model). */
async function powerRows(ctx: CampusExportCtx, id: string, doc: Json): Promise<Rows> {
  const load = obj(doc.largeLoad);
  const gens = obj(doc.backupGenerators);
  const grid: Rows = [
    { table: 'Grid connection', field: 'Utility', value: doc.utility ?? null },
    { table: 'Grid connection', field: 'ISO/RTO', value: doc.iso ?? null },
    { table: 'Grid connection', field: 'Balancing authority', value: doc.balancingAuthority ?? null },
    { table: 'Grid connection', field: 'Large-load request', value: load.request ?? 'n/d' },
    { table: 'Grid connection', field: 'Queue position', value: load.queuePosition ?? 'n/d' },
    { table: 'Backup generators', field: 'Count', value: gens.count ?? 'n/d' },
    { table: 'Backup generators', field: 'MW', value: gens.mw ?? 'n/d' },
    { table: 'Backup generators', field: 'Fuel', value: gens.fuel ?? 'n/d' },
  ];
  const plants = await queryRows<Json>(
    ctx.db, PLANT_ROWS, { q: { servesId: id }, orgId: ctx.orgId, asOf: ctx.asOf, hidden: ctx.hidden }, { limit: 500, offset: 0 },
  );
  const plantRows = plants.rows.map((p) => {
    const units = obj(p.units);
    return {
      table: 'On-site plant', field: p.name, value: p.permittedMw ?? null, id: p.id, status: p.status ?? null, technology: p.technology ?? null,
      units: units.count ?? null,
      byModel: list(units.byModel).map((u) => `${num(u.count)} × ${str(u.oem, 'n/d')} ${str(u.model)}`).join('; '),
      airPermit: p.airPermit ?? null, gridMode: p.gridMode ?? null,
    };
  });
  return [...grid, ...plantRows];
}

/** Records tab: one type chip's records (?type=Permit), or all. */
export function recordRows(records: unknown, type: string | undefined): Rows {
  const rows = list(records);
  if (type === undefined) return rows;
  const chip = DOC_TYPES.find((t) => t.toLowerCase() === type.toLowerCase());
  if (!chip) throw badRequest(`type must be one of ${DOC_TYPES.join(', ')}`);
  return rows.filter((r) => r.type === chip);
}

interface TabExport {
  view: string;
  rows: (doc: unknown, ctx: CampusExportCtx, id: string) => Rows | Promise<Rows>;
}

export const CAMPUS_EXPORT_TABS: Readonly<Record<string, TabExport>> = {
  overview: { view: 'dc_detail', rows: (doc) => overviewRows(obj(doc)) },
  buildings: { view: 'dc_tab:buildings', rows: (doc) => list(doc).map(({ footprint: _footprint, ...rest }) => rest) },
  power: { view: 'dc_detail', rows: (doc, ctx, id) => powerRows(ctx, id, obj(doc)) },
  records: { view: 'dc_tab:records', rows: (doc, ctx) => recordRows(doc, first(ctx.q, 'type')) },
  history: { view: 'dc_tab:history', rows: (doc) => list(doc) },
};

export interface CampusExportCtx { db: Db; q: Query; orgId: string; asOf: string | null; hidden: readonly string[] }

export async function campusExport(ctx: CampusExportCtx): Promise<{ view: string; asOf: string | null; rows: Rows }> {
  const id = first(ctx.q, 'id');
  if (!id) throw badRequest('id is required');
  const tab = first(ctx.q, 'tab') ?? 'overview';
  const spec = Object.hasOwn(CAMPUS_EXPORT_TABS, tab) ? CAMPUS_EXPORT_TABS[tab] : undefined;
  if (!spec) throw badRequest(`tab must be one of ${Object.keys(CAMPUS_EXPORT_TABS).join(', ')}`);
  const doc = await ctx.db.readModel<unknown>(spec.view, id, ctx.asOf);
  if (doc === null) {
    if (!(await subjectExists(ctx.db, 'campus', 'id', id))) throw notFound('campus');
    throw notReady(spec.view, id);
  }
  return { view: spec.view, asOf: ctx.asOf, rows: await spec.rows(doc, ctx, id) };
}
