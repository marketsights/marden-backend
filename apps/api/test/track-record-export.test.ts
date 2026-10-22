// Track record (spec 12) Excel exports: the backtest's cases and the models table from the track_record read
// model, and the Sources table from the collector heartbeats, each with the rows shown on screen.
import ExcelJS from 'exceljs';
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { clearReadModels, makeApp, putReadModel, testDb } from './helpers.js';

const AS_OF = '2026-10-21';
const DOC = {
  asOf: AS_OF, published: 1, total: 2,
  cases: [
    { name: 'TR test loan', instrumentId: 'TR-TEST', flagged: '2026-07-09', moved: '2026-09-18', leadDays: 71, backtestRunning: false, flagBasis: 'reconstructed' },
    { name: 'TR running case', instrumentId: null, flagged: null, moved: null, leadDays: null, backtestRunning: true, flagBasis: null },
  ],
  rules: [], material: [], statusMethod: { version: 'status v0.1', url: 'docs/methods/status-rules-v0.1.md' }, backtest: null,
  models: [{ model: 'price', output: 'Marden price', version: 'price v0.1', builtFrom: 'Tier curves', cadence: 'Daily', methodUrl: null, lastRun: AS_OF }],
};

describe('track record exports', () => {
  let db: Db;
  let app: FastifyInstance;

  beforeAll(async () => {
    db = testDb();
    app = await makeApp({ mode: 'dev', db });
    await clearReadModels(db, ['track_record']);
    await putReadModel(db, { view: 'track_record', key: 'all', asOf: AS_OF }, DOC);
    await db.query(`DELETE FROM source WHERE id = 'tr_test_source'`);
    await db.query(
      `INSERT INTO source (id, name, kind, cadence_minutes, last_ok_at) VALUES ('tr_test_source', 'TR test collector', 'api', 1440, now())`,
    );
  });
  afterAll(async () => {
    await clearReadModels(db, ['track_record']);
    await db.query(`DELETE FROM source WHERE id = 'tr_test_source'`);
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
    return { header, rows };
  }

  it('exports the backtest cases', async () => {
    const { rows } = await sheet('/api/export/track-record.xlsx');
    expect(rows).toHaveLength(2);
    expect(rows[0]).toMatchObject({ name: 'TR test loan', flagged: '2026-07-09', moved: '2026-09-18', leadDays: 71, flagBasis: 'reconstructed', status: 'published' });
    // A case still being backtested is a row of its own, as on the chart, with "—" for what it does not have yet.
    expect(rows[1]).toMatchObject({ name: 'TR running case', flagged: '—', moved: '—', leadDays: '—', status: 'backtest running' });
  });

  it('exports the models table', async () => {
    const { header, rows } = await sheet('/api/export/models.xlsx');
    expect(header.slice(0, 5)).toEqual(['output', 'version', 'builtFrom', 'cadence', 'lastRun']); // the screen's columns first
    expect(rows).toEqual([expect.objectContaining({ model: 'price', output: 'Marden price', version: 'price v0.1', cadence: 'Daily' })]);
  });

  it('exports the sources table from the collector heartbeats', async () => {
    const { header, rows } = await sheet('/api/export/sources.xlsx');
    expect(header.slice(0, 5)).toEqual(['source', 'id', 'kind', 'cadenceMinutes', 'status']);
    expect(rows.find((r) => r.id === 'tr_test_source')).toMatchObject({ source: 'TR test collector', cadenceMinutes: 1440, status: 'ok' });
  });

  it('answers 503 not_ready while the track record is not built', async () => {
    await clearReadModels(db, ['track_record']);
    const res = await app.inject({ method: 'GET', url: '/api/export/models.xlsx' });
    expect(res.json()).toEqual({ error: 'not_ready', view: 'track_record', key: 'all' });
    await putReadModel(db, { view: 'track_record', key: 'all', asOf: AS_OF }, DOC);
  });
});
