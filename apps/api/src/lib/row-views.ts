// Filters and sorts for the Data centers (spec 14) and On-site power (spec 15) tables.
// Each row view holds one DataCenterRow / PowerPlantRow doc per campus / plant id.
import type { PlantDebt } from '../contracts/types.js';
import { withPlantDebt } from './plant-debt-row.js';
import { arrAt, inList, low, numAt, txt, type FilterCtx, type FilterDef, type RowViewDef } from './row-query.js';

const PERMIT_FIELD = 'air_permit_number';

/** Watchlist only: the row's financed site is on the caller's org watchlist. */
const watchlistPred: FilterDef['pred'] = (_p, ctx) =>
  `${txt('financed,siteSlug')} IN (SELECT w.ref_id FROM watchlist_item w WHERE w.org_id = ${ctx.org()} AND w.kind = 'site')`;

const financedPred: FilterDef['pred'] = () => `jsonb_typeof(r.doc -> 'financed') = 'object'`;

/**
 * Any of the requested values found inside the field, case-insensitive. Developer, tenant and utility
 * are written as the record states them ("Oracle (end user OpenAI)", "AEP Texas North"), so "Oracle"
 * or "AEP" typed into the chip has to match part of the text (strpos: no LIKE wildcards to escape).
 */
const containsAny = (p: string): FilterDef['pred'] => (ph) =>
  `EXISTS (SELECT 1 FROM unnest(${ph}::text[]) v WHERE strpos(${low(p)}, v) > 0)`;

const anyText = (arrayExpr: string, ph: string): string =>
  `EXISTS (SELECT 1 FROM jsonb_array_elements_text(${arrayExpr}) x WHERE x ILIKE ${ph})`;

const parcelMatch = (table: string, keyColumn: string, ph: string): string =>
  `EXISTS (SELECT 1 FROM ${table} t WHERE t.${keyColumn} = r.key AND EXISTS (SELECT 1 FROM unnest(t.parcel_ids) pid WHERE pid ILIKE ${ph}))`;

/** Permit numbers as known on the as-of day (fact_current, or fact_asof for ?asOf reads). */
const permitMatch = (subjects: string, ph: string, ctx: FilterCtx): string =>
  `EXISTS (SELECT 1 FROM ${ctx.facts()} f WHERE f.field = '${PERMIT_FIELD}'
     AND (f.value #>> '{}') ILIKE ${ph} AND ${subjects})`;

/** Search box: name, aliases, developer, parcel ID (campus or building), permit number. */
const dcSearch: FilterDef['pred'] = (ph, ctx) =>
  [
    `r.doc ->> 'name' ILIKE ${ph}`,
    anyText(arrAt('aliases'), ph),
    `${txt('developer,value')} ILIKE ${ph}`,
    parcelMatch('campus', 'id', ph),
    parcelMatch('building', 'campus_id', ph),
    permitMatch(
      `((f.subject_type = 'campus' AND f.subject_id = r.key)
        OR (f.subject_type = 'building' AND f.subject_id IN (SELECT b.id FROM building b WHERE b.campus_id = r.key))
        OR (f.subject_type = 'plant' AND f.subject_id IN (SELECT ps.plant_id FROM plant_serves ps WHERE ps.campus_id = r.key)))`,
      ph,
      ctx,
    ),
  ].join(' OR ');

export const DC_ROWS: RowViewDef = {
  view: 'dc_row',
  idTable: { table: 'campus', column: 'id' },
  pageSize: 50,
  defaultSort: '-lastChange',
  filters: [
    { param: 'state', kind: 'list', pred: inList('state') },
    { param: 'county', kind: 'list', pred: inList('county') },
    { param: 'status', kind: 'list', pred: inList('status,value') },
    { param: 'developer', kind: 'list', pred: containsAny('developer,value') },
    { param: 'tenant', kind: 'list', pred: containsAny('tenant,value') },
    { param: 'utility', kind: 'list', pred: containsAny('utility,value') },
    { param: 'iso', kind: 'list', pred: inList('iso,value') },
    { param: 'powerSource', kind: 'list', pred: inList('powerSource,value') },
    { param: 'mwMin', kind: 'number', pred: (p) => `${numAt('facilityMw,value')} >= ${p}` },
    { param: 'mwMax', kind: 'number', pred: (p) => `${numAt('facilityMw,value')} <= ${p}` },
    { param: 'firstSeenSince', kind: 'date', pred: (p) => `left(${txt('firstSeen')}, 10) >= ${p}::text` },
    {
      param: 'opposition',
      kind: 'flag',
      pred: (_p, ctx) => `EXISTS (SELECT 1 FROM event e WHERE e.subject_type = 'campus' AND e.subject_id = r.key
        AND e.kind = 'opposition' AND e.status = 'published' AND e.event_date <= ${ctx.day()}::date
        AND ${ctx.known('e.learned_at')})`,
    },
    { param: 'financed', kind: 'flag', pred: financedPred },
    { param: 'watchlist', kind: 'flag', pred: watchlistPred },
    { param: 'q', kind: 'text', pred: dcSearch },
  ],
  sorts: {
    name: low('name'),
    state: low('state'),
    county: low('county'),
    developer: low('developer,value'),
    status: low('status,value'),
    facilityMw: numAt('facilityMw,value'),
    itMw: numAt('itMw,value'),
    buildings: numAt('buildings,total'),
    onlineYear: numAt('onlineYear,value'),
    firstSeen: txt('firstSeen'),
    lastChange: txt('lastChange,date'),
  },
};

const unitMatch = (field: 'oem' | 'model'): FilterDef['pred'] => (ph) =>
  `EXISTS (SELECT 1 FROM jsonb_array_elements(${arrAt('units,byModel')}) u WHERE lower(u ->> '${field}') = ANY(${ph}::text[]))`;

/** Search box: plant name, permit number, OEM model. */
const plantSearch: FilterDef['pred'] = (ph, ctx) =>
  [
    `r.doc ->> 'name' ILIKE ${ph}`,
    `${txt('airPermit,number')} ILIKE ${ph}`,
    `EXISTS (SELECT 1 FROM jsonb_array_elements(${arrAt('units,byModel')}) u WHERE u ->> 'model' ILIKE ${ph} OR u ->> 'oem' ILIKE ${ph})`,
    permitMatch(`f.subject_type = 'plant' AND f.subject_id = r.key`, ph, ctx),
  ].join(' OR ');

export const PLANT_ROWS: RowViewDef = {
  view: 'plant_row',
  idTable: { table: 'power_plant', column: 'id' },
  pageSize: 50,
  defaultSort: '-lastChange',
  filters: [
    { param: 'state', kind: 'list', pred: inList('state') },
    { param: 'status', kind: 'list', pred: inList('status,value') },
    { param: 'tech', kind: 'list', pred: inList('technology') },
    { param: 'oem', kind: 'list', pred: unitMatch('oem') },
    { param: 'model', kind: 'list', pred: unitMatch('model') },
    { param: 'operator', kind: 'list', pred: inList('operator,value') },
    { param: 'servesId', kind: 'list', pred: inList('serves,id') },
    { param: 'agency', kind: 'list', pred: inList('airPermit,agency') },
    { param: 'permitType', kind: 'list', pred: inList('airPermit,type') },
    { param: 'gridMode', kind: 'list', pred: inList('gridMode') },
    { param: 'financed', kind: 'flag', pred: financedPred },
    { param: 'watchlist', kind: 'flag', pred: watchlistPred },
    { param: 'q', kind: 'text', pred: plantSearch },
  ],
  sorts: {
    name: low('name'),
    state: low('state'),
    operator: low('operator,value'),
    status: low('status,value'),
    technology: low('technology'),
    units: numAt('units,count'),
    permittedMw: numAt('permittedMw,value'),
    lastChange: txt('lastChange,date'),
  },
  // Spec 20 columns (Owner, Financed by, Plant debt, Power buyer) from the plant's debt document.
  companion: { view: 'plant_debt', merge: (doc: object, debt: PlantDebt | null) => withPlantDebt(doc, debt) },
};
