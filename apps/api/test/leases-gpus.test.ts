// Lease map (spec 25) and GPU index (spec 19): GET /api/leases?format=geojson places each lease at
// the campus it sits on (its own campusId, else its site's campuses), as of the snapshot date; the
// lease export applies the page's URL filters; the GPU exports carry the tables on /gpus and
// /gpus/:model with their Sourced values.
import ExcelJS from 'exceljs';
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { clearReadModels, makeApp, putReadModel, sourced, testDb } from './helpers.js';

const AS_OF = '2026-10-15';
const LATER = '2026-10-18';
const m = <T>(value: T | null) => ({ ...sourced(value, 'M'), methodVersion: 'leases v0' });

function lease(id: string, lessee: string, extra: Record<string, unknown> = {}) {
  return {
    id, lessee, lessor: sourced('Landlord', 'R'), siteSlug: null, campusId: null, kind: 'Lease', status: 'Signed, not commenced',
    totalB: sourced(null, 'F'), annualB: sourced(null, 'F'), commencement: sourced(null, 'F'), termYears: sourced(null, 'F'),
    attribution: 'Filed', confidence: null, turnsAdded: m(null), headroomBeforeTurns: m(null), triggerLease: false,
    lesseeBand: m(null), priorityScore: m(null), dependentDebt: [], evidence: [], ...extra,
  };
}

const LEASES = {
  asOf: AS_OF,
  totals: { disclosedB: 0, attributedB: 22.4, unattributedB: 0, attributedShare: 0, triggerLeases: 1, debtExposedB: 18 },
  reconciliation: [],
  leases: [
    lease('ora-jupiter', 'Oracle', {
      siteSlug: 'lm-jupiter', totalB: sourced(12.4, 'F'), triggerLease: true, lesseeBand: m('High'),
      dependentDebt: [{ instrumentId: 'I1', name: 'Jupiter loan', amountB: sourced(18, 'F'), ratingLinked: true, holdersCount: 7 },
        { instrumentId: 'I2', name: 'Notes', amountB: sourced(null, 'F'), ratingLinked: false, holdersCount: 3 }],
      evidence: [{ date: '2026-10-01', text: 'Lease signed', tag: 'F', sourceName: 'SEC EDGAR', sourceUrl: 'https://www.sec.gov/x' }],
    }),
    lease('ms-jupiter', 'Microsoft', { siteSlug: 'lm-jupiter', kind: 'Compute contract', attribution: 'Inferred', confidence: 'Low' }),
    lease('meta-direct', 'Meta', { campusId: 'lm-campus-b', totalB: sourced(10, 'R'), lesseeBand: m('Watch') }),
    lease('nowhere', 'Anthropic', { siteSlug: 'lm-no-campus' }),
    lease('unlinked', 'Nebius'),
  ],
};

const campus = (id: string, lat: number | null, mw: number | null) => ({
  id, name: `Campus ${id}`, aliases: [], county: 'Test', state: 'TX', lat, lon: lat === null ? null : -100,
  developer: sourced('Dev', 'R'), tenant: sourced('Oracle', 'R'), status: sourced('Under construction', 'M'),
  facilityMw: sourced(mw, 'R'), itMw: sourced(null, 'R'), buildings: { built: 0, total: 2 },
  powerSource: sourced('Grid', 'R'), utility: sourced(null, 'R'), iso: sourced(null, 'R'), plantId: null,
  onlineYear: sourced(null, 'R'), financed: null, lastChange: { date: AS_OF, text: 'x', tag: 'R', sourceName: 'x', sourceUrl: '' }, firstSeen: AS_OF,
});

const GPUS = {
  asOf: AS_OF,
  index: [
    { date: '2026-10-10', model: 'H100', term: 'on-demand', region: 'all', medianUsdHr: 4.8, p25: 4.2, p75: 5.4, nProviders: 3 },
    { date: '2026-10-14', model: 'H100', term: 'on-demand', region: 'all', medianUsdHr: 4.5, p25: 4.1, p75: 5.3, nProviders: 3 },
    { date: '2026-10-14', model: 'H100', term: '1y', region: 'all', medianUsdHr: 2.9, p25: 2.8, p75: 3.1, nProviders: 2 },
    { date: '2026-10-14', model: 'B200', term: 'on-demand', region: 'all', medianUsdHr: 8.5, p25: 7.6, p75: 8.6, nProviders: 3 },
  ],
  providers: [
    { provider: 'Lambda', model: 'H100', term: 'on-demand', region: 'all', usdHr: sourced(3.99, 'R', AS_OF, 'https://lambda.ai/pricing'), capturedAt: AS_OF, pageUrl: 'https://lambda.ai/pricing' },
    { provider: 'Nebius', model: 'B200', term: 'on-demand', region: 'all', usdHr: sourced(8.5, 'R', AS_OF, 'https://nebius.com/prices'), capturedAt: AS_OF, pageUrl: 'https://nebius.com/prices' },
  ],
  valueByAge: [
    { model: 'H100', launch: sourced('2022-10', 'R'), newPrice: sourced(40625, 'M'), points: [{ age: 0, usd: m(40625) }, { age: 1, usd: m(28787) }] },
  ],
};

const DEPLOYMENT = {
  asOf: AS_OF,
  totals: { deliveredUnits: m(null), energizedUnits: m(null), idleShare: m(null) },
  companies: [],
  facilities: [{
    scope: 'facility', id: 'EX-1', name: 'DDTL 1.0', model: 'H100', unitsFinanced: m(43000), unitsDelivered: m(null), unitsEnergized: m(null),
    idleUnits: m(null), idleShare: m(null), monthsIdle: m(null), idleInterestM: m(null), valueLostWhileIdleM: m(null),
    ltvToday: m(3.26), ltvIfOnSchedule: m(null), status: 'n/d', missing: ['units delivered', 'coupon'], evidence: [],
    assumptions: { kwPerUnit: m(null), pue: 1.32, couponPct: sourced(null, 'F'), methodVersion: 'gpu_deployment v0.1 (model v0)' },
  }],
};

const MODEL = {
  model: 'H100', launch: sourced('2022-10', 'R'), newPrice: sourced(40625, 'M'), index: GPUS.index.slice(0, 2), providers: GPUS.providers.slice(0, 1),
  resale: [{ date: '2026-08-15', usd: 14250, source: 'Amcompute', url: 'https://www.amcompute.com/appraisals/h100' }],
  valueByAgeYears: GPUS.valueByAge[0]?.points ?? [],
  assumptions: { utilization: 0.16, annualDecline: 0.22, haircut: 0.2, usefulLifeYears: m(5), methodVersion: 'gpu_model v0.1' },
  facilities: [{ instrumentId: 'EX-1', name: 'DDTL 1.0', units: m(43000) }],
};

const VIEWS = ['leases', 'site_tab:buildings', 'dc_row', 'sites', 'gpus', 'gpu_deployment', 'gpu_model'];

describe('lease map and GPU index', () => {
  let db: Db;
  let app: FastifyInstance;

  beforeAll(async () => {
    db = testDb();
    app = await makeApp({ mode: 'dev', db });
    await clearReadModels(db, VIEWS);
    await putReadModel(db, { view: 'leases', key: 'all', asOf: AS_OF }, LEASES);
    await putReadModel(db, { view: 'site_tab:buildings', key: 'lm-jupiter', asOf: AS_OF }, { campuses: [{ id: 'lm-campus-a', name: 'A' }], buildings: [] });
    await putReadModel(db, { view: 'site_tab:buildings', key: 'lm-no-campus', asOf: AS_OF }, { campuses: [], buildings: [] });
    await putReadModel(db, { view: 'dc_row', key: 'lm-campus-a', asOf: AS_OF }, campus('lm-campus-a', 31.8, 2462));
    await putReadModel(db, { view: 'dc_row', key: 'lm-campus-b', asOf: AS_OF }, campus('lm-campus-b', 32.5, null));
    // A later campus row moves the point; a snapshot read before it keeps the old one.
    await putReadModel(db, { view: 'dc_row', key: 'lm-campus-a', asOf: LATER }, campus('lm-campus-a', 31.9, 2500));
    await putReadModel(db, { view: 'sites', key: 'all', asOf: AS_OF }, [{ slug: 'lm-jupiter', name: 'Project Jupiter', gw: sourced('2.46 GW', 'R') }]);
    await putReadModel(db, { view: 'gpus', key: 'all', asOf: AS_OF }, GPUS);
    await putReadModel(db, { view: 'gpu_deployment', key: 'all', asOf: AS_OF }, DEPLOYMENT);
    await putReadModel(db, { view: 'gpu_model', key: 'H100', asOf: AS_OF }, MODEL);
  });
  afterAll(async () => {
    await clearReadModels(db, VIEWS);
    await app.close();
    await db.close();
  });

  const get = (url: string) => app.inject({ method: 'GET', url });

  it('serves the lease map as published and places leases on their campuses for the map', async () => {
    expect((await get('/api/leases')).json()).toEqual(LEASES);
    const geo = (await get('/api/leases?format=geojson')).json();
    expect(geo).toMatchObject({ type: 'FeatureCollection', asOf: AS_OF, total: 5, located: 3 });
    expect(geo.features.map((f: { id: string }) => f.id)).toEqual(['lm-campus-a', 'lm-campus-b']);
    const [a, b] = geo.features;
    expect(a.geometry.coordinates).toEqual([-100, 31.9]);
    expect(a.properties).toMatchObject({
      name: 'Campus lm-campus-a', leaseIds: ['ora-jupiter', 'ms-jupiter'], facilityMw: { value: 2500, tag: 'R' },
      sites: [{ slug: 'lm-jupiter', name: 'Project Jupiter', gw: { value: '2.46 GW', tag: 'R' } }],
    });
    expect(b.properties).toMatchObject({ leaseIds: ['meta-direct'], sites: [], facilityMw: { value: null } });
  });

  it('reads the campus rows as of the snapshot date and refuses an unknown format', async () => {
    const geo = (await get(`/api/leases?format=geojson&asOf=${AS_OF}`)).json();
    expect(geo.features[0].geometry.coordinates).toEqual([-100, 31.8]);
    expect(geo.features[0].properties.facilityMw.value).toBe(2462);
    expect((await get('/api/leases?format=csv')).statusCode).toBe(400);
  });

  async function sheet(url: string) {
    const res = await get(url);
    expect(res.statusCode).toBe(200);
    const wb = new ExcelJS.Workbook();
    await wb.xlsx.load(res.rawPayload as unknown as ArrayBuffer);
    const data = wb.getWorksheet('Data')!;
    const header = (data.getRow(1).values as unknown[]).slice(1) as string[];
    const rows = Array.from({ length: data.rowCount - 1 }, (_, i) => {
      const values = (data.getRow(i + 2).values as unknown[]).slice(1);
      return Object.fromEntries(header.map((h, j) => [h, values[j]]));
    });
    return { rows, sources: wb.getWorksheet('Sources')!.getSheetValues().slice(2) as unknown[][] };
  }

  it('exports the lease table with the page filters, its columns and the evidence', async () => {
    expect((await sheet('/api/export/leases.xlsx')).rows).toHaveLength(5);
    const { rows, sources } = await sheet('/api/export/leases.xlsx?lessee=oracle,Meta&band=High');
    expect(rows.map((r) => r.id)).toEqual(['ora-jupiter']);
    expect(rows[0]).toMatchObject({ lessee: 'Oracle', totalB: 12.4, triggerLease: true, lesseeBand: 'High', debtExposedB: 18, holders: 10, evidence: '2026-10-01 · Lease signed' });
    expect(sources.some((l) => l[1] === 'ora-jupiter' && l[2] === 'evidence[0]' && l[6] === 'https://www.sec.gov/x')).toBe(true);
    expect((await sheet('/api/export/leases.xlsx?attribution=Inferred')).rows.map((r) => r.id)).toEqual(['ms-jupiter']);
    expect((await sheet('/api/export/leases.xlsx?trigger=1')).rows.map((r) => r.id)).toEqual(['ora-jupiter']);
    // "Signed, not commenced" holds a comma: the status filter keys on the part before it.
    expect((await sheet('/api/export/leases.xlsx?status=signed&kind=Compute%20contract')).rows.map((r) => r.id)).toEqual(['ms-jupiter']);
    expect((await sheet('/api/export/leases.xlsx?q=jupiter&band=n/d')).rows.map((r) => r.id)).toEqual(['ms-jupiter']);
    expect((await get('/api/export/leases.xlsx?band=Severe')).statusCode).toBe(400);
  });

  it('exports the GPU tables: provider prices, models, delivered vs energized and a model page', async () => {
    const providers = await sheet('/api/export/gpus.xlsx?model=H100');
    expect(providers.rows).toEqual([expect.objectContaining({ provider: 'Lambda', model: 'H100', usdHr: 3.99 })]);
    expect(providers.sources.some((l) => l[2] === 'usdHr' && l[4] === 'R' && l[6] === 'https://lambda.ai/pricing')).toBe(true);

    const models = await sheet('/api/export/gpu-models.xlsx');
    expect(models.rows.map((r) => [r.model, r.indexDate, r.medianUsdHr, r.valueAge0y, r.valueAge1y])).toEqual([
      ['B200', '2026-10-14', 8.5, '', ''],
      ['H100', '2026-10-14', 4.5, 40625, 28787],
    ]);
    expect((await sheet('/api/export/gpu-models.xlsx?term=1y')).rows.find((r) => r.model === 'H100')?.medianUsdHr).toBe(2.9);
    expect((await get('/api/export/gpu-models.xlsx?term=5y')).statusCode).toBe(400);

    const deployment = await sheet('/api/export/gpu-deployment.xlsx');
    expect(deployment.rows[0]).toMatchObject({ id: 'EX-1', unitsFinanced: 43000, unitsDelivered: 'n/d', status: 'n/d', missing: 'units delivered; coupon' });

    expect((await sheet('/api/export/gpu-model.xlsx?model=H100&part=resale')).rows).toEqual([
      expect.objectContaining({ model: 'H100', usd: 14250, source: 'Amcompute' }),
    ]);
    expect((await sheet('/api/export/gpu-model.xlsx?model=H100&part=facilities')).rows[0]).toMatchObject({ instrumentId: 'EX-1', units: 43000 });
    expect((await get('/api/export/gpu-model.xlsx')).statusCode).toBe(400);
    expect((await get('/api/export/gpu-model.xlsx?model=H100&part=bogus')).statusCode).toBe(400);
    expect((await get('/api/export/gpu-model.xlsx?model=Z9')).statusCode).toBe(404);
    expect((await sheet('/api/export/gpu-model.xlsx?model=h100&part=resale')).rows).toHaveLength(1);
  });

  it('serves a GPU model page by its case-insensitive name; 404 for a model the index does not list', async () => {
    const h100 = await get('/api/gpus/h100');
    expect(h100.statusCode).toBe(200);
    expect(h100.json()).toEqual(MODEL);
    expect((await get('/api/gpus/H100')).json()).toEqual(MODEL);
    const unknown = await get('/api/gpus/Z9');
    expect(unknown.statusCode).toBe(404);
    expect(unknown.json()).toMatchObject({ error: 'not_found' });
    // Listed by the index but not computed by the GPU model yet: not ready, under the index's name.
    expect((await get('/api/gpus/b200')).json()).toEqual({ error: 'not_ready', view: 'gpu_model', key: 'B200' });
    expect((await get('/api/gpus/b200')).statusCode).toBe(503);
    // Before the index exists nothing can be ruled out: not ready.
    const early = await get('/api/gpus/Z9?asOf=2026-10-01');
    expect(early.statusCode).toBe(503);
    expect(early.json()).toEqual({ error: 'not_ready', view: 'gpu_model', key: 'Z9' });
  });
});
