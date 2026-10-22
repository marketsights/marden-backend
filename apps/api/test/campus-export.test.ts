// GET /api/export/data-center.xlsx?id=&tab=[&type=]: the campus detail header's Excel exports the table
// on the tab the user is looking at (spec 14 header actions, spec 00 "Exports"), from the tab's own read
// model, with a sources sheet. Before this route the button answered 404 ("export_table").
import ExcelJS from 'exceljs';
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { CAMPUS_EXPORT_TABS, overviewRows, recordRows } from '../src/lib/campus-export.js';
import { makeApp, putReadModel, sourced, testDb } from './helpers.js';

const AS_OF = '2026-10-15';
const ID = 'xl-campus';

const rec = (date: string, type: string, text: string, tag: 'F' | 'R' | 'M' = 'F') => ({
  date, type, text, tag, agency: tag === 'F' ? 'TCEQ' : 'Press', sourceName: tag === 'F' ? 'TCEQ' : 'Press', sourceUrl: `https://example.org/${date}`,
});

const detail = {
  id: ID, name: 'Export test campus', aliases: [], county: 'Taylor', state: 'TX', lat: 32.5, lon: -99.78,
  developer: sourced('Crusoe', 'R'), tenant: sourced('Oracle', 'R'), status: sourced('Operating', 'M'),
  facilityMw: sourced(1200, 'R'), itMw: sourced(null, 'F'), buildings: { built: 2, total: 8 },
  powerSource: sourced('Hybrid', 'M'), utility: sourced('AEP', 'R'), iso: sourced('ERCOT', 'F'), plantId: null,
  onlineYear: sourced(2025, 'R'), financed: null, lastChange: rec('2026-10-10', 'Press', 'Status: Operating', 'R'), firstSeen: '2024-05-31',
  facts: { 'Square feet': sourced(4000000, 'R'), Address: sourced('5502 Spinks Rd, Abilene, TX', 'R') },
  ownerOfRecord: sourced(null, 'F'), parcelIds: [], balancingAuthority: sourced('ERCO', 'F'),
  backupGenerators: { count: sourced(26, 'M'), mw: sourced(null, 'F'), fuel: sourced(null, 'F') },
  keyDates: [{ label: 'First building operational', date: sourced('2025-09-26', 'R') }],
  imagery: [], tabCounts: { buildings: 1, records: 3, history: 1 },
};
const records = [
  rec('2026-09-10', 'Filing', 'Oracle Q1 FY27 call: buildings delivered', 'R'),
  rec('2026-08-05', 'Permit', 'Draft permit 182126 noticed'),
  rec('2026-07-28', 'Imagery', 'Sentinel-2 L2A, 28 Jul 2026', 'R'),
];
const history = [{ date: '2026-05-23', field: 'buildings_operational', before: '2', after: '4', evidence: rec('2026-05-23', 'Press', 'Epoch AI', 'R') }];
const buildings = [
  {
    id: `${ID}-b1`, label: 'Building 1', footprint: { type: 'Polygon', coordinates: [[[-99.78, 32.5], [-99.77, 32.5], [-99.77, 32.51], [-99.78, 32.5]]] },
    sqft: sourced(null, 'F'), facilityMw: sourced(null, 'F'), itMw: sourced(null, 'F'), status: sourced('Operating', 'M'),
    buildingPermit: sourced(null, 'F'), coDate: sourced(null, 'F'), firstVisible: '2024-07-12', inService: sourced('2025-06-28', 'R'),
  },
];

describe('campus detail export', () => {
  let db: Db;
  let app: FastifyInstance;

  beforeAll(async () => {
    db = testDb();
    app = await makeApp({ mode: 'dev', db });
    await db.query(`DELETE FROM read_model WHERE key = $1`, [ID]);
    await db.query(`INSERT INTO campus (id, name, state) VALUES ($1, 'Export test campus', 'TX') ON CONFLICT DO NOTHING`, [ID]);
    await putReadModel(db, { view: 'dc_detail', key: ID, asOf: AS_OF }, detail);
    await putReadModel(db, { view: 'dc_tab:records', key: ID, asOf: AS_OF }, records);
    await putReadModel(db, { view: 'dc_tab:history', key: ID, asOf: AS_OF }, history);
    await putReadModel(db, { view: 'dc_tab:buildings', key: ID, asOf: AS_OF }, buildings);
  });
  afterAll(async () => {
    await db.query(`DELETE FROM read_model WHERE key = $1`, [ID]);
    await db.query(`DELETE FROM campus WHERE id = $1`, [ID]);
    await app.close();
    await db.close();
  });

  async function sheet(url: string) {
    const res = await app.inject({ method: 'GET', url });
    expect(res.statusCode, res.body).toBe(200);
    expect(res.headers['content-disposition']).toContain('marden-data-center-');
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

  it('Overview: the facts card rows with their values, then every other fact and the key dates', async () => {
    const { rows, sources } = await sheet(`/api/export/data-center.xlsx?id=${ID}&tab=overview`);
    const byField = Object.fromEntries(rows.map((r) => [r.field, r.value]));
    expect(byField).toMatchObject({ 'Developer · operator': 'Crusoe', 'Facility MW': 1200, 'IT MW': 'n/d', 'Parcel IDs': 'n/d', 'Square feet': 4000000 });
    expect(byField['Buildings']).toBe('2 of 8 built');
    expect(rows.at(-1)).toMatchObject({ table: 'Key dates', field: 'First building operational', value: '2025-09-26' });
    expect(sources.some((l) => l.includes('https://example.test/doc'))).toBe(true);
  });

  it('Records: all records, or one type chip (case-insensitive); an unknown type is a 400', async () => {
    expect((await sheet(`/api/export/data-center.xlsx?id=${ID}&tab=records`)).rows.map((r) => r.type)).toEqual(['Filing', 'Permit', 'Imagery']);
    const permits = await sheet(`/api/export/data-center.xlsx?id=${ID}&tab=records&type=permit`);
    expect(permits.rows).toHaveLength(1);
    expect(permits.rows[0]).toMatchObject({ date: '2026-08-05', text: 'Draft permit 182126 noticed', tag: 'F', agency: 'TCEQ' });
    const bad = await app.inject({ method: 'GET', url: `/api/export/data-center.xlsx?id=${ID}&tab=records&type=Rumour` });
    expect(bad.statusCode).toBe(400);
  });

  it('Buildings, History and Power: the tab tables (no footprints); Power lists the grid fields', async () => {
    const b = await sheet(`/api/export/data-center.xlsx?id=${ID}&tab=buildings`);
    expect(b.rows[0]).toMatchObject({ id: `${ID}-b1`, label: 'Building 1', status: 'Operating', inService: '2025-06-28' });
    expect(Object.keys(b.rows[0] ?? {})).not.toContain('footprint');
    const h = await sheet(`/api/export/data-center.xlsx?id=${ID}&tab=history`);
    expect(h.rows[0]).toMatchObject({ date: '2026-05-23', field: 'buildings_operational', before: '2', after: '4' });
    const p = await sheet(`/api/export/data-center.xlsx?id=${ID}&tab=power`);
    expect(p.rows.slice(0, 3).map((r) => [r.field, r.value])).toEqual([['Utility', 'AEP'], ['ISO/RTO', 'ERCOT'], ['Balancing authority', 'ERCO']]);
    expect(p.rows.find((r) => r.field === 'Count')).toMatchObject({ table: 'Backup generators', value: 26 });
  });

  it('answers 400 without an id or with an unknown tab, 404 for an unknown campus, 503 before the view is built', async () => {
    expect((await app.inject({ method: 'GET', url: '/api/export/data-center.xlsx?tab=overview' })).statusCode).toBe(400);
    expect((await app.inject({ method: 'GET', url: `/api/export/data-center.xlsx?id=${ID}&tab=lease` })).statusCode).toBe(400);
    expect((await app.inject({ method: 'GET', url: '/api/export/data-center.xlsx?id=no-such-campus' })).statusCode).toBe(404);
    await db.query(`INSERT INTO campus (id, name) VALUES ('xl-campus-unbuilt', 'Unbuilt') ON CONFLICT DO NOTHING`);
    try {
      expect((await app.inject({ method: 'GET', url: '/api/export/data-center.xlsx?id=xl-campus-unbuilt' })).statusCode).toBe(503);
    } finally {
      await db.query(`DELETE FROM campus WHERE id = 'xl-campus-unbuilt'`);
    }
  });

  it('maps the same tabs as the web app and keeps the row helpers pure', () => {
    expect(Object.keys(CAMPUS_EXPORT_TABS)).toEqual(['overview', 'buildings', 'power', 'records', 'history']);
    expect(recordRows(records, undefined)).toHaveLength(3);
    expect(overviewRows({}).filter((r) => r.table === 'Facts').length).toBeGreaterThan(10);
  });
});
