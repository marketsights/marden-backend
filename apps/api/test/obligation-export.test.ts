// Obligation map Excel export (spec 18 table view; spec 00 "Exports"): the table's columns in screen
// order with ends by name, n/d (no annual schedule) and "—" (year outside the schedule) as the screen
// shows them, no JSON cells, the screen's filters, and every Sourced value and source passage on
// the sources sheet.
import ExcelJS from 'exceljs';
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { obligationExportRows } from '../src/lib/obligation-export.js';
import { clearReadModels, makeApp, putReadModel, sourced, testDb } from './helpers.js';

const AS_OF = '2026-10-15';
const m = (value: number) => ({ ...sourced(value, 'M'), methodVersion: 'obligations v0.1' });
const evidence = (text: string) => ({ date: '2026-10-13', text, tag: 'F', sourceName: 'SEC EDGAR', sourceUrl: 'https://example.test/10q' });
interface EdgeSpec { id: string; from: string; to: string; type: string; totalB: number | null; annual: { year: number; amountB: unknown }[] }
const edge = ({ id, from, to, type, totalB, annual }: EdgeSpec) => ({
  id, from, to, type, status: 'Signed', totalB: sourced(totalB, totalB === null ? 'R' : 'F'), annual,
  start: sourced('2026-H2 (target)', 'R'), termYears: sourced(totalB === null ? null : 15, 'F'), conditions: [sourced('Outside date 2027', 'F')],
  linkedSites: ['abilene'], linkedInstruments: ['EX-1'], evidence: [evidence(`${from} owes ${to}`)],
});
const GRAPH = {
  asOf: AS_OF,
  kpis: { totalB: 15, filedPct: 100, changedThisMonth: 2 },
  nodes: [
    { id: 'oracle', name: 'Oracle', type: 'Cloud', status: 'Broken', owedB: 0, owesB: 5 },
    { id: 'applied-digital', name: 'Applied Digital Corp.', type: 'Developer', status: 'Holding', owedB: 5, owesB: 0 },
    { id: 'openai', name: 'OpenAI', type: 'AI lab', status: 'Strained', owedB: 0, owesB: 0 },
  ],
  edges: [
    edge({ id: 'oracle--applied-digital--lease', from: 'oracle', to: 'applied-digital', type: 'Lease', totalB: 5, annual: [2026, 2027].map((year) => ({ year, amountB: m(0.333) })) }),
    edge({ id: 'openai--oracle--compute-purchase', from: 'openai', to: 'oracle', type: 'Compute purchase', totalB: null, annual: [] }),
    edge({ id: 'oracle--openai--warrant', from: 'oracle', to: 'openai', type: 'Warrant', totalB: 10, annual: [{ year: 2029, amountB: m(2) }] }),
  ],
};

describe('obligations export', () => {
  let db: Db;
  let app: FastifyInstance;

  beforeAll(async () => {
    db = testDb();
    app = await makeApp({ mode: 'dev', db });
    await putReadModel(db, { view: 'obligations', key: 'all', asOf: AS_OF }, GRAPH);
  });
  afterAll(async () => {
    await clearReadModels(db, ['obligations']);
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

  it('exports the map table in screen order: names, n/d and — as shown, no JSON cells', async () => {
    const { header, rows, sources } = await sheet('/api/export/obligations.xlsx');
    expect(header).toEqual(['id', 'payer', 'payee', 'type', 'status', 'totalB', 'annual2026B', 'start', 'termYears', 'sources']);
    expect(rows[0]).toMatchObject({
      payer: 'Oracle', payee: 'Applied Digital Corp.', type: 'Lease', status: 'Signed', totalB: 5, annual2026B: 0.333,
      start: '2026-H2 (target)', termYears: 15, sources: '2026-10-13 · oracle owes applied-digital',
    });
    expect(rows[1]).toMatchObject({ payer: 'OpenAI', payee: 'Oracle', totalB: 'n/d', annual2026B: 'n/d', termYears: 'n/d' });
    expect(rows[2]).toMatchObject({ payer: 'Oracle', payee: 'OpenAI', annual2026B: '—' });
    expect(rows.every((r) => Object.values(r).every((v) => !String(v).startsWith('[') && !String(v).startsWith('{')))).toBe(true);
    const line = (row: string, field: string) => sources.find((l) => l[1] === row && l[2] === field);
    expect(line('oracle--applied-digital--lease', 'annual2026B')?.[4]).toBe('M');
    expect(line('oracle--applied-digital--lease', 'annual2026B')?.[8]).toBe('obligations v0.1');
    expect(line('oracle--applied-digital--lease', 'totalB')?.[7]).toBe(AS_OF);
    expect(line('oracle--applied-digital--lease', 'sources[0]')?.[6]).toBe('https://example.test/10q');
  });

  it('keeps the screen’s filters (the company page exports both sides of one company)', async () => {
    expect((await sheet('/api/export/obligations.xlsx?type=Lease')).rows.map((r) => r.id)).toEqual(['oracle--applied-digital--lease']);
    expect((await sheet('/api/export/obligations.xlsx?company=openai')).rows.map((r) => r.id)).toEqual([
      'openai--oracle--compute-purchase', 'oracle--openai--warrant',
    ]);
  });

  it('keeps each Sourced value as sent for the sources sheet', () => {
    const [row] = obligationExportRows(GRAPH as never);
    expect(row?.totalB).toBe(GRAPH.edges[0]?.totalB);
    expect(row?.annual2026B).toBe(GRAPH.edges[0]?.annual[0]?.amountB);
  });
});
