// Completion (spec 03) and Sites (spec 05) Excel exports: the table's columns in screen order, the
// projection spread into P10 / P50 / P90 / outside date / cushion / Δ wk instead of a JSON cell, "—"
// where the screen shows "—" (no lease date), n/d where it shows n/d, and every Sourced value on the
// sources sheet. Filters are the screen's own (?state=…).
import ExcelJS from 'exceljs';
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { completionExportRow, sitesExportRow } from '../src/lib/list-export.js';
import { clearReadModels, makeApp, putReadModel, sourced, testDb } from './helpers.js';

const AS_OF = '2026-10-15';
const m = (value: number | null) => ({ ...sourced(value, 'M'), methodVersion: 'cushion v0.1', sourceUrl: undefined });
const chain = [
  { role: 'end_user', party: 'OpenAI', status: 'amber', reason: 'E1' },
  { role: 'tenant', party: 'Oracle', status: 'red', reason: 'T2' },
  { role: 'developer', party: 'Dev Co', status: 'green', reason: '' },
  { role: 'lenders', party: 'About 20 banks', status: 'amber', reason: 'L1' },
];
const evidence = { date: '2026-10-13', text: 'Pipeline in service Feb 1 2027', tag: 'F', sourceName: 'FERC', sourceUrl: 'https://example.test/ferc' };
const buildings = { ...sourced({ built: 2, total: 8, campusId: 'campus-a' }, 'M') };

const LEASED = {
  slug: 'le-leased', name: 'Leased site', state: 'NM', chain, driver: 'Power', leadLenders: 'About 20 banks',
  debtB: sourced(18, 'F'), lastEvidence: evidence, buildings,
  projection: {
    p10: '2028-07', p50: '2029-01', p90: '2032-05', outsideDate: sourced('2028', 'R'), outsideDateAssumed: '2028-12-31',
    cushionMonths: m(-1), deltaWeek: m(-5),
  },
};
const NO_LEASE = {
  slug: 'le-nolease', name: 'No lease site', state: 'TX', chain, driver: null, leadLenders: 'n/d',
  debtB: sourced(null, 'F'), lastEvidence: evidence, buildings: null,
  projection: { p10: '2027-05', p50: '2027-10', p90: '2028-11', outsideDate: m(null), cushionMonths: null, deltaWeek: m(null) },
};
const NO_PROJECTION = { ...NO_LEASE, slug: 'le-none', name: 'No projection site', projection: null };

const summary = (slug: string, state: string, cushion: number | null) => ({
  slug, name: `Site ${slug}`, county: 'Test Co.', state, endUser: 'OpenAI', tenant: null, developer: 'Dev Co',
  gw: sourced('1.2 GW', 'R'), chain, status: 'Strained', cushionMonths: cushion === null ? null : m(cushion),
  rentStartP50: cushion === null ? null : '2027-06', debtB: sourced(null, 'F'), leadLenders: 'n/d',
  buildings: cushion === null ? null : buildings,
});

describe('completion and sites exports', () => {
  let db: Db;
  let app: FastifyInstance;

  beforeAll(async () => {
    db = testDb();
    app = await makeApp({ mode: 'dev', db });
    await putReadModel(db, { view: 'completion', key: 'all', asOf: AS_OF }, {
      asOf: AS_OF,
      summary: { pastOutside: { count: 1, debtB: 18, names: ['Leased site'] }, under3: { count: 0, debtB: 0, names: [] },
        slippedThisWeek: { count: 1, months: -5 }, debtByBand: [{ band: '<0', debtB: 18 }] },
      rows: [LEASED, NO_LEASE, NO_PROJECTION],
    });
    await putReadModel(db, { view: 'sites', key: 'all', asOf: AS_OF }, [summary('le-a', 'TX', 4), summary('le-b', 'NM', null)]);
  });
  afterAll(async () => {
    await clearReadModels(db, ['completion', 'sites']);
    await app.close();
    await db.close();
  });

  async function sheet(url: string) {
    const res = await app.inject({ method: 'GET', url });
    expect(res.statusCode).toBe(200);
    const wb = new ExcelJS.Workbook();
    await wb.xlsx.load(res.rawPayload as unknown as ArrayBuffer);
    const data = wb.getWorksheet('Data')!;
    const header = (data.getRow(1).values as unknown[]).slice(1) as string[];
    const rows = Array.from({ length: data.rowCount - 1 }, (_, i) => {
      const values = (data.getRow(i + 2).values as unknown[]).slice(1);
      return Object.fromEntries(header.map((h, j) => [h, values[j]]));
    });
    return { header, rows, sources: wb.getWorksheet('Sources')!.getSheetValues().slice(2) as unknown[][] };
  }

  it('exports the completion table in screen order, the projection in its own columns', async () => {
    const { header, rows, sources } = await sheet('/api/export/completion.xlsx');
    expect(header).toEqual([
      'slug', 'name', 'state', 'buildings', 'chain', 'p10', 'p50', 'p90', 'outsideDate', 'cushionMonths', 'deltaWeek',
      'driver', 'lastEvidence', 'debtB', 'leadLenders',
    ]);
    expect(rows[0]).toMatchObject({
      slug: 'le-leased', buildings: '2 / 8', p10: '2028-07', p50: '2029-01', p90: '2032-05', outsideDate: '2028',
      cushionMonths: -1, deltaWeek: -5, driver: 'Power', debtB: 18, lastEvidence: '2026-10-13 · Pipeline in service Feb 1 2027',
    });
    expect(rows[0]?.chain).toBe('OpenAI (amber) → Oracle (red) → Dev Co (green) → About 20 banks (amber)');
    // No lease date: the band is still there; cushion, Δ wk "—"; the model named no driver: n/d.
    expect(rows[1]).toMatchObject({ p50: '2027-10', outsideDate: 'n/d', cushionMonths: '—', deltaWeek: '—', driver: 'n/d', buildings: 'n/d', debtB: 'n/d' });
    expect(rows[2]).toMatchObject({ p10: '—', p50: '—', outsideDate: '—', cushionMonths: '—', driver: '—' });
    expect(rows.every((r) => !String(r.p50).startsWith('{'))).toBe(true); // no JSON cells
    const line = (field: string) => sources.find((l) => l[1] === 'le-leased' && l[2] === field);
    expect(line('cushionMonths')?.[4]).toBe('M');
    expect(line('cushionMonths')?.[8]).toBe('cushion v0.1');
    expect(line('outsideDate')?.[4]).toBe('R');
    expect(line('buildings')?.[4]).toBe('M');
  });

  it('keeps the screen’s filters', async () => {
    const { rows } = await sheet('/api/export/completion.xlsx?state=TX');
    expect(rows.map((r) => r.slug)).toEqual(['le-nolease', 'le-none']);
  });

  it('exports the sites table in screen order, buildings after GW', async () => {
    const { header, rows } = await sheet('/api/export/sites.xlsx?state=TX');
    expect(header).toEqual([
      'slug', 'name', 'county', 'state', 'endUser', 'tenant', 'developer', 'gw', 'buildings', 'chain', 'status',
      'cushionMonths', 'rentStartP50', 'debtB', 'leadLenders',
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ slug: 'le-a', tenant: '—', gw: '1.2 GW', buildings: '2 / 8', cushionMonths: 4, rentStartP50: '2027-06' });
    const nm = (await sheet('/api/export/sites.xlsx?state=NM')).rows[0];
    expect(nm).toMatchObject({ slug: 'le-b', buildings: 'n/d', cushionMonths: '—', rentStartP50: '—', debtB: 'n/d' });
  });

  it('maps rows without changing the Sourced values the sources sheet reads', () => {
    const row = completionExportRow(LEASED as never);
    expect(row.cushionMonths).toBe(LEASED.projection.cushionMonths);
    expect(row.buildings).toBe(buildings);
    expect(sitesExportRow(summary('le-a', 'TX', 4) as never).gw).toEqual(sourced('1.2 GW', 'R'));
  });
});
