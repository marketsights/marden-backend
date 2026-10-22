// On-site power: a plant serving a campus that finances several sites (Galaxy Helios I and II) carries them all in
// `financedSites`, as the campus row does. The financed filter counts the plant once, the watchlist filter finds it
// through any of its sites, and the Excel export names every site instead of writing the list as JSON.
import ExcelJS from 'exceljs';
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { clearReadModels, makeApp, putReadModel, sourced, testDb } from './helpers.js';

const evidence = { date: '2026-10-01', text: 'Record', tag: 'F', sourceName: 'County', sourceUrl: 'https://example.test/r' };
const link = (slug: string, chainStatus: string, debt: number | null) =>
  ({ siteSlug: slug, siteName: `Site ${slug}`, chainStatus, debtB: sourced(debt, debt === null ? 'R' : 'F') });

function plantRow(id: string, sites: ReturnType<typeof link>[]) {
  return {
    id, name: `Plant ${id}`, county: 'Test', state: 'TX', lat: 32.4, lon: -99.7, serves: null, technology: 'Simple-cycle turbine',
    units: { count: 4, byModel: [] }, permittedMw: sourced(120, 'F'), status: sourced('Permitted', 'F'), airPermit: null,
    operator: sourced('Operator', 'R'), fuelSupply: sourced(null, 'F'), gridMode: 'Unknown', lastChange: evidence,
    financed: sites[0] ?? null, financedSites: sites,
  };
}

const ROWS = [
  plantRow('ppf-two', [link('ppf-big', 'Broken', 3.5), link('ppf-small', 'Strained', 1.27)]),
  plantRow('ppf-one', [link('ppf-solo', 'Holding', 9.4)]),
  plantRow('ppf-none', []),
];

describe('power plants serving several financed sites', () => {
  let db: Db;
  let app: FastifyInstance;

  beforeAll(async () => {
    db = testDb();
    app = await makeApp({ mode: 'dev', db });
    await clearReadModels(db, ['plant_row', 'plant_debt']);
    for (const r of ROWS) {
      await db.query(`INSERT INTO power_plant (id, name, state) VALUES ($1, $2, $3)`, [r.id, r.name, r.state]);
      await putReadModel(db, { view: 'plant_row', key: r.id, asOf: '2026-10-15' }, r);
    }
  });
  afterAll(async () => {
    await db.query(`DELETE FROM watchlist_item WHERE org_id = 'dev' AND ref_id LIKE 'ppf-%'`);
    await clearReadModels(db, ['plant_row']);
    await db.query(`DELETE FROM power_plant WHERE id = ANY($1)`, [ROWS.map((r) => r.id)]);
    await app.close();
    await db.close();
  });

  const json = async (url: string) => (await app.inject({ method: 'GET', url })).json();
  const ids = async (url: string) => ((await json(url)).rows as { id: string }[]).map((r) => r.id).sort();

  it('serves the list on the row and counts each financed plant once', async () => {
    const row = await json('/api/power-plants/ppf-two/row');
    expect(row.financedSites.map((s: { siteSlug: string }) => s.siteSlug)).toEqual(['ppf-big', 'ppf-small']);
    expect(row.financed).toEqual(row.financedSites[0]);
    expect(await ids('/api/power-plants?financed=true')).toEqual(['ppf-one', 'ppf-two']);
  });

  it('finds a plant on the watchlist through any of its financed sites', async () => {
    expect(await ids('/api/power-plants?watchlist=true')).toEqual([]);
    await db.query(`INSERT INTO watchlist_item (org_id, kind, ref_id) VALUES ('dev', 'site', 'ppf-small')`);
    expect(await ids('/api/power-plants?watchlist=true')).toEqual(['ppf-two']);
  });

  it('exports the financed sites by name', async () => {
    const res = await app.inject({ method: 'GET', url: '/api/export/power-plants.xlsx?sort=name' });
    expect(res.statusCode).toBe(200);
    const wb = new ExcelJS.Workbook();
    await wb.xlsx.load(res.rawPayload as unknown as ArrayBuffer);
    const data = wb.getWorksheet('Data')!;
    const header = (data.getRow(1).values as unknown[]).slice(1);
    const byId = new Map<unknown, ExcelJS.Row>();
    data.eachRow((r, n) => n > 1 && byId.set(r.getCell(header.indexOf('id') + 1).value, r));
    const cell = (id: string, key: string) => byId.get(id)!.getCell(header.indexOf(key) + 1).value;
    expect(cell('ppf-two', 'financedSites')).toBe('Site ppf-big, Site ppf-small');
    expect(cell('ppf-two', 'financed')).toBe('Site ppf-big');
    expect(cell('ppf-one', 'financedSites')).toBe('Site ppf-solo');
    expect(cell('ppf-none', 'financedSites') ?? '').toBe('');
  });
});
