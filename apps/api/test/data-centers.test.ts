// Data centers and On-site power: spec 14/15 filters in SQL on the row read models, paging, GeoJSON,
// KPI strips equal to the filtered rows, and the Excel export (data + sources sheets).
import ExcelJS from 'exceljs';
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { resetIsoBoundaryCache } from '../src/queries/grid.js';
import { clearReadModels, makeApp, putReadModel, sourced, testDb } from './helpers.js';

const today = new Date().toISOString().slice(0, 10);
const evidence = (date: string) => ({ date, text: `Record ${date}`, tag: 'F', sourceName: 'County', sourceUrl: 'https://example.test/record' });

function dcRow(id: string, o: { state: string; status: string; mw: number | null; dev: string; financed?: string; firstSeen: string; last: string; lat?: number }) {
  return {
    id, name: `Campus ${id}`, aliases: [`${id} alias`], county: 'Test', state: o.state, lat: o.lat ?? 32.4, lon: -99.7,
    developer: sourced(o.dev, 'F'), tenant: sourced(null, 'R'), status: sourced(o.status, 'F'),
    facilityMw: sourced(o.mw, 'F'), itMw: sourced(null, 'F'), buildings: { built: 1, total: 2 },
    powerSource: sourced('Grid', 'F'), utility: sourced('Oncor', 'F'), iso: sourced('ERCOT', 'F'), plantId: null,
    onlineYear: sourced(2027, 'M'), lastChange: evidence(o.last), firstSeen: o.firstSeen,
    financed: o.financed ? { siteSlug: o.financed, siteName: o.financed, chainStatus: 'Strained', debtB: sourced(9.4, 'R') } : null,
  };
}

const ROWS = [
  dcRow('dc-a', { state: 'TX', status: 'Operating', mw: 1200, dev: 'Crusoe', financed: 'dc-test-site', firstSeen: '2025-01-10', last: '2026-10-10' }),
  dcRow('dc-b', { state: 'TX', status: 'Planned', mw: 300, dev: 'Vantage', firstSeen: today, last: '2026-10-12' }),
  dcRow('dc-c', { state: 'NM', status: 'Planned', mw: null, dev: 'Yucca', firstSeen: '2026-03-01', last: '2026-09-01' }),
];

describe('data centers and power plants', () => {
  let db: Db;
  let app: FastifyInstance;

  beforeAll(async () => {
    db = testDb();
    app = await makeApp({ mode: 'dev', db });
    for (const r of ROWS) {
      await db.query(`INSERT INTO campus (id, name, state, parcel_ids) VALUES ($1, $2, $3, $4)`, [r.id, r.name, r.state, r.id === 'dc-c' ? ['R-0042-17'] : []]);
      await putReadModel(db, { view: 'dc_row', key: r.id, asOf: '2026-10-15' }, r);
    }
    await putReadModel(db, { view: 'plant_row', key: 'pp-1', asOf: '2026-10-15' }, {
      id: 'pp-1', name: 'Abilene generation', state: 'TX', lat: 32.5, lon: -99.8, technology: 'Simple-cycle turbine',
      units: { count: 10, byModel: [{ oem: 'Solar Turbines', model: 'Titan 350', count: 5 }, { oem: 'GE Vernova', model: 'LM2500', count: 5 }] },
      permittedMw: sourced(360.5, 'F'), status: sourced('Permitted', 'F'), airPermit: { agency: 'TCEQ', number: '177263', type: 'Standard', status: 'Permitted', decided: today, url: 'https://example.test/p' },
      operator: sourced('Crusoe', 'F'), gridMode: 'Islanded', financed: null, lastChange: evidence('2026-10-01'), serves: null,
    });
    await putReadModel(db, { view: 'plant_detail', key: 'pp-1', asOf: '2026-10-15' }, {
      id: 'pp-1', permit: { filed: '2024-08-30', decided: today },
      unitRows: [{ oem: 'Solar Turbines', status: 'Permitted', totalMw: 190 }, { oem: 'GE Vernova', status: 'Permitted', totalMw: 170.5 }],
    });
  });
  afterAll(async () => {
    await clearReadModels(db, ['dc_row', 'plant_row', 'plant_detail']);
    await db.query(`DELETE FROM campus WHERE id = ANY($1)`, [ROWS.map((r) => r.id)]);
    await app.close();
    await db.close();
  });

  const json = async (url: string) => (await app.inject({ method: 'GET', url })).json();
  const ids = async (qs: string) => ((await json(`/api/data-centers?${qs}`)).rows as { id: string }[]).map((r) => r.id);

  it('pages rows with the default sort (last change, newest first)', async () => {
    const page = await json('/api/data-centers');
    expect(page).toMatchObject({ asOf: '2026-10-15', total: 3, page: 1, pageSize: 50 });
    expect(page.rows.map((r: { id: string }) => r.id)).toEqual(['dc-b', 'dc-a', 'dc-c']);
    expect(page.rows[1]).toEqual(ROWS[0]);
    const p2 = await json('/api/data-centers?page=2');
    expect(p2).toMatchObject({ total: 3, page: 2, rows: [] });
  });

  it('applies the spec 14 filters in SQL', async () => {
    expect(await ids('state=tx')).toEqual(['dc-b', 'dc-a']);
    expect(await ids('status=Planned&state=NM')).toEqual(['dc-c']);
    expect(await ids('mwMin=500')).toEqual(['dc-a']);
    expect(await ids('mwMax=500')).toEqual(['dc-b']);
    expect(await ids('financed=true')).toEqual(['dc-a']);
    expect(await ids(`firstSeenSince=2026-01-01`)).toEqual(['dc-b', 'dc-c']);
    expect(await ids('developer=crusoe,yucca&sort=name')).toEqual(['dc-a', 'dc-c']);
    expect(await ids('q=vantage')).toEqual(['dc-b']);
    expect(await ids('q=R-0042')).toEqual(['dc-c']);
    expect(await ids('q=dc-a%20alias')).toEqual(['dc-a']);
    expect(await ids('sort=-facilityMw')).toEqual(['dc-a', 'dc-b', 'dc-c']);
    expect(await ids('watchlist=true')).toEqual([]);
    expect((await app.inject({ method: 'GET', url: '/api/data-centers?sort=evil' })).statusCode).toBe(400);
  });

  it('searches permit numbers and opposition as known on the as-of day', async () => {
    await db.query(
      `INSERT INTO fact (subject_type, subject_id, field, value, tag, source_name, valid_from, status, producer, dedupe_key, learned_at)
       VALUES ('campus', 'dc-b', 'air_permit_number', '"O-4410"', 'F', 'TCEQ', '2026-09-01', 'published', 'test', 'dc-permit', '2026-10-20T12:00:00Z')`,
    );
    await db.query(
      `INSERT INTO event (subject_type, subject_id, event_date, kind, title, tag, source_name, status, dedupe_key, learned_at)
       VALUES ('campus', 'dc-c', '2026-09-15', 'opposition', 'Residents petition', 'F', 'County', 'published', 'dc-opp', '2026-10-20T12:00:00Z')`,
    );
    try {
      expect(await ids('q=O-4410')).toEqual(['dc-b']);
      expect(await ids('q=O-4410&asOf=2026-10-16')).toEqual([]); // learned Oct 20: not known on Oct 16
      expect(await ids('opposition=true')).toEqual(['dc-c']);
      expect(await ids('opposition=true&asOf=2026-10-16')).toEqual([]);
    } finally {
      await db.query(`DELETE FROM fact WHERE dedupe_key = 'dc-permit'`);
      await db.query(`DELETE FROM event WHERE dedupe_key = 'dc-opp'`);
    }
  });

  it('returns GeoJSON for the map with the same filters', async () => {
    const res = await app.inject({ method: 'GET', url: '/api/data-centers?format=geojson&state=TX' });
    expect(res.headers['content-type']).toContain('application/geo+json');
    const fc = res.json();
    expect(fc.type).toBe('FeatureCollection');
    expect(fc.total).toBe(2);
    expect(fc.features).toHaveLength(2);
    expect(fc.features[0]).toMatchObject({ type: 'Feature', geometry: { type: 'Point', coordinates: [-99.7, 32.4] } });
    expect(fc.features[0].properties.id).toBe('dc-b');
  });

  it('serves one row by id for live updates: the same doc as the list, 404 unknown, 503 not built', async () => {
    const res = await app.inject({ method: 'GET', url: '/api/data-centers/dc-a/row' });
    expect(res.statusCode).toBe(200);
    expect(res.json()).toEqual((await json('/api/data-centers?state=TX&sort=name')).rows.find((r: { id: string }) => r.id === 'dc-a'));
    expect((await app.inject({ method: 'GET', url: '/api/data-centers/no-such-campus/row' })).statusCode).toBe(404);
    await db.query(`INSERT INTO campus (id, name) VALUES ('dc-unbuilt', 'Unbuilt campus')`);
    try {
      expect((await app.inject({ method: 'GET', url: '/api/data-centers/dc-unbuilt/row' })).json())
        .toEqual({ error: 'not_ready', view: 'dc_row', key: 'dc-unbuilt' });
    } finally {
      await db.query(`DELETE FROM campus WHERE id = 'dc-unbuilt'`);
    }
    const plant = await app.inject({ method: 'GET', url: '/api/power-plants/pp-1/row' });
    expect(plant.statusCode).toBe(200);
    expect(plant.json()).toEqual((await json('/api/power-plants')).rows[0]);
  });

  it('computes the KPI strip from the filtered rows', async () => {
    const all = await json('/api/data-centers/summary');
    expect(all).toMatchObject({ campuses: 3, buildings: 6, financed: { count: 1, debtB: 9.4 } });
    expect(all.mwByStatus).toEqual([{ status: 'Operating', mw: 1200 }, { status: 'Planned', mw: 300 }]);
    expect(all.newThisWeek).toEqual({ count: 1, newest: { id: 'dc-b', name: 'Campus dc-b' } });
    expect(all.coverage.find((c: { field: string }) => c.field === 'facilityMw').pct).toBeCloseTo(66.7, 1);
    const tx = await json('/api/data-centers/summary?state=TX');
    expect(tx.campuses).toBe((await json('/api/data-centers?state=TX')).total);
  });

  it('counts built buildings and the campuses behind each coverage rate', async () => {
    const all = await json('/api/data-centers/summary');
    expect(all).toMatchObject({ buildings: 6, buildingsBuilt: 3 });
    const field = (f: string) => all.coverage.find((c: { field: string }) => c.field === f);
    expect(field('facilityMw')).toEqual({ field: 'facilityMw', pct: 66.7, known: 2 });
    expect(field('tenant')).toEqual({ field: 'tenant', pct: 0, known: 0 });
    expect(field('buildings')).toEqual({ field: 'buildings', pct: 100, known: 3 });
    const nm = await json('/api/data-centers/summary?state=NM');
    expect(nm).toMatchObject({ campuses: 1, buildings: 2, buildingsBuilt: 1 });
    expect(nm.coverage.find((c: { field: string }) => c.field === 'facilityMw')).toEqual({ field: 'facilityMw', pct: 0, known: 0 });
  });

  it('matches developer, tenant and utility on part of the recorded text', async () => {
    expect(await ids('developer=crus&sort=name')).toEqual(['dc-a']);
    expect(await ids('utility=ONC&sort=name')).toEqual(['dc-a', 'dc-b', 'dc-c']);
    expect(await ids('utility=AEP')).toEqual([]);
    expect(await ids('tenant=oracle')).toEqual([]); // tenant n/d on every test row
    expect((await json('/api/data-centers/summary?utility=oncor&state=TX')).campuses).toBe(2);
  });

  it('serves the ISO/RTO boundaries layer from grid_region, simplified, with its source', async () => {
    resetIsoBoundaryCache();
    await db.query(
      `INSERT INTO grid_region (id, kind, name, country, geom, meta) VALUES
         ('iso:TESTRTO', 'iso_rto', 'Test Regional Transmission Organization', 'US',
          ST_Multi(ST_GeomFromText('POLYGON((-100 30, -95.000001 30, -95 30, -95 35, -100 35, -100 30))', 4326)),
          '{"label": "TESTRTO", "hifld": {"as_of": "2022-12-15", "source_url": "https://example.test/hifld-iso.gpkg"}}'),
         ('iso:TESTCA', 'iso_rto', 'Test Canadian operator', 'CA',
          ST_Multi(ST_GeomFromText('POLYGON((-80 50, -79 50, -79 51, -80 50))', 4326)), '{}'),
         ('iso:TESTNOGEOM', 'iso_rto', 'Registered without polygon', 'US', NULL, '{}')`,
    );
    try {
      const res = await app.inject({ method: 'GET', url: '/api/grid/iso-rto' });
      expect(res.statusCode).toBe(200);
      expect(res.headers['content-type']).toContain('application/geo+json');
      const fc = res.json();
      expect(fc.type).toBe('FeatureCollection');
      const ids = fc.features.map((f: { id: string }) => f.id);
      expect(ids).toContain('iso:TESTRTO');
      expect(ids).not.toContain('iso:TESTCA');
      expect(ids).not.toContain('iso:TESTNOGEOM');
      const rto = fc.features.find((f: { id: string }) => f.id === 'iso:TESTRTO');
      expect(rto.properties).toEqual({
        id: 'iso:TESTRTO', label: 'TESTRTO', name: 'Test Regional Transmission Organization',
        labelPoint: expect.any(Array),
        source: {
          value: 'Test Regional Transmission Organization', tag: 'F', sourceName: 'HIFLD Independent System Operators',
          sourceUrl: 'https://example.test/hifld-iso.gpkg', asOf: '2022-12-15',
        },
      });
      const [lon, lat] = rto.properties.labelPoint;
      expect(lon).toBeGreaterThan(-100);
      expect(lon).toBeLessThan(-95);
      expect(lat).toBeGreaterThan(30);
      expect(lat).toBeLessThan(35);
      // The near-duplicate vertex at -95.000001 is simplified away: a closed ring of 5 points.
      const ring = rto.geometry.type === 'MultiPolygon' ? rto.geometry.coordinates[0][0] : rto.geometry.coordinates[0];
      expect(['Polygon', 'MultiPolygon']).toContain(rto.geometry.type);
      expect(ring).toHaveLength(5);
      // Unchanged rows: the cached body; a changed row is picked up.
      await db.query(`UPDATE grid_region SET name = 'Renamed operator', updated_at = now() + interval '1 second' WHERE id = 'iso:TESTRTO'`);
      const again = (await app.inject({ method: 'GET', url: '/api/grid/iso-rto' })).json();
      expect(again.features.find((f: { id: string }) => f.id === 'iso:TESTRTO').properties.name).toBe('Renamed operator');
    } finally {
      await db.query(`DELETE FROM grid_region WHERE id IN ('iso:TESTRTO', 'iso:TESTCA', 'iso:TESTNOGEOM')`);
      resetIsoBoundaryCache();
    }
  });

  it('filters plants and builds the plant KPI strip', async () => {
    expect((await json('/api/power-plants?oem=ge%20vernova')).total).toBe(1);
    expect((await json('/api/power-plants?oem=Caterpillar')).total).toBe(0);
    expect((await json('/api/power-plants?q=177263')).total).toBe(1);
    const summary = await json('/api/power-plants/summary');
    expect(summary).toMatchObject({ plants: 1, units: 10, permitsLast30d: 1 });
    expect(summary.mwByTech).toEqual([{ tech: 'Simple-cycle turbine', mw: 360.5 }]);
    expect(summary.mwByOem).toEqual([
      { oem: 'GE Vernova', byStatus: [{ status: 'Permitted', mw: 170.5 }] },
      { oem: 'Solar Turbines', byStatus: [{ status: 'Permitted', mw: 190 }] },
    ]);
  });

  it('exports the filtered rows as xlsx with a data and a sources sheet', async () => {
    const res = await app.inject({ method: 'GET', url: '/api/export/data-centers.xlsx?state=TX' });
    expect(res.statusCode).toBe(200);
    expect(res.headers['content-type']).toContain('spreadsheetml.sheet');
    expect(res.headers['content-disposition']).toContain('marden-data-centers-2026-10-15.xlsx');
    const wb = new ExcelJS.Workbook();
    await wb.xlsx.load(res.rawPayload as unknown as ArrayBuffer);
    expect(wb.worksheets.map((s) => s.name)).toEqual(['Data', 'Buildings', 'Sources']);
    const data = wb.getWorksheet('Data')!;
    expect(data.rowCount).toBe(3);
    const header = (data.getRow(1).values as unknown[]).slice(1);
    expect(header).toContain('facilityMw');
    const mwCol = header.indexOf('facilityMw') + 1;
    expect(data.getRow(2).getCell(mwCol).value).toBe(300);
    const sources = wb.getWorksheet('Sources')!;
    expect((sources.getRow(1).values as unknown[]).slice(1)).toEqual(['Row', 'Field', 'Value', 'Tag', 'Source', 'URL', 'As of', 'Method']);
    const lines = sources.getSheetValues().slice(2) as unknown[][];
    const mw = lines.find((l) => l[1] === 'dc-a' && l[2] === 'facilityMw')!;
    expect(mw.slice(3)).toEqual([1200, 'F', 'Test record', 'https://example.test/doc', '2026-10-15', '']);
    expect(lines.some((l) => l[2] === 'financed.debtB' && l[4] === 'R')).toBe(true);
  });

  it('adds a building sheet for the exported campuses, in the table order, with their sources', async () => {
    const building = (id: string, label: string, inService: string | null) => ({
      id, label, footprint: { type: 'Polygon', coordinates: [[[-99.7, 32.4], [-99.6, 32.4], [-99.6, 32.5], [-99.7, 32.4]]] },
      sqft: sourced(null, 'F'), facilityMw: sourced(null, 'F'), itMw: sourced(105, 'R'), status: sourced('Operating', 'M'),
      buildingPermit: sourced(null, 'F'), coDate: sourced(null, 'F'), firstVisible: '2024-07-12', inService: sourced(inService, 'R'),
    });
    await putReadModel(db, { view: 'dc_tab:buildings', key: 'dc-a', asOf: '2026-10-15' }, [building('dc-a-b1', 'Building 1', '2025-06-28'), building('dc-a-b2', 'Building 2', null)]);
    await putReadModel(db, { view: 'dc_tab:buildings', key: 'dc-c', asOf: '2026-10-15' }, [building('dc-c-b1', 'Hall 1', null)]);
    try {
      const load = async (qs: string) => {
        const wb = new ExcelJS.Workbook();
        await wb.xlsx.load((await app.inject({ method: 'GET', url: `/api/export/data-centers.xlsx?${qs}` })).rawPayload as unknown as ArrayBuffer);
        return wb;
      };
      const tx = (await load('state=TX')).getWorksheet('Buildings')!;
      const header = (tx.getRow(1).values as unknown[]).slice(1);
      expect(header).toEqual(['id', 'campusId', 'campus', 'label', 'sqft', 'facilityMw', 'itMw', 'status', 'buildingPermit', 'coDate', 'firstVisible', 'inService']);
      const rows = (tx.getSheetValues().slice(2) as unknown[][]).map((r) => r.slice(1));
      expect(rows).toEqual([
        ['dc-a-b1', 'dc-a', 'Campus dc-a', 'Building 1', 'n/d', 'n/d', 105, 'Operating', 'n/d', 'n/d', '2024-07-12', '2025-06-28'],
        ['dc-a-b2', 'dc-a', 'Campus dc-a', 'Building 2', 'n/d', 'n/d', 105, 'Operating', 'n/d', 'n/d', '2024-07-12', 'n/d'],
      ]); // dc-c is in NM: filtered out with its campus
      const all = await load('sort=name');
      expect((all.getWorksheet('Buildings')!.getSheetValues().slice(2) as unknown[][]).map((r) => r[1])).toEqual(['dc-a-b1', 'dc-a-b2', 'dc-c-b1']);
      const sources = all.getWorksheet('Sources')!.getSheetValues().slice(2) as unknown[][];
      expect(sources.find((l) => l[1] === 'dc-a-b1' && l[2] === 'inService')?.slice(3, 5)).toEqual(['2025-06-28', 'R']);
    } finally {
      await clearReadModels(db, ['dc_tab:buildings']);
    }
  });

  it('answers 404 for an unknown export table and 503 when its read model is missing', async () => {
    expect((await app.inject({ method: 'GET', url: '/api/export/nope.xlsx' })).statusCode).toBe(404);
    expect((await app.inject({ method: 'GET', url: '/api/export/leases.xlsx' })).json()).toEqual({ error: 'not_ready', view: 'leases', key: 'all' });
  });
});
