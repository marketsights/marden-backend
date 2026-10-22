// GET /api/risk-list (spec 10): the published weeks for the week picker, ?week= on any day of its week,
// frozen past lists served as stored, and the snapshot date limiting which weeks exist.
import ExcelJS from 'exceljs';
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { mondayOf } from '../src/queries/risk-list.js';
import { clearReadModels, makeApp, putReadModel, testDb } from './helpers.js';

const list = (start: string, end: string, name: string) => ({
  week: { start, end },
  rows: [{ rank: 1, instrumentId: 'RL-TEST', name, size: '$1.0B', why: [], price: null, change1w: null, fundMark: null, gap: null,
    odds: { value: 0.5, tag: 'M', asOf: start, methodVersion: 'odds v0' }, move: { kind: 'new' } }],
  note: { title: `Risk list, week of ${start}`, paragraphs: [{ lead: name, text: 'Ranked 1.', refs: [] }] },
  earlierCalls: [],
});

describe('GET /api/risk-list', () => {
  let db: Db;
  let app: FastifyInstance;
  const get = (url: string) => app.inject({ method: 'GET', url });

  beforeAll(async () => {
    db = testDb();
    app = await makeApp({ mode: 'dev', db });
    await clearReadModels(db, ['risk_list']);
    await putReadModel(db, { view: 'risk_list', key: '2026-10-05', asOf: '2026-10-05' }, list('2026-10-05', '2026-10-11', 'First week'));
    await putReadModel(db, { view: 'risk_list', key: '2026-10-12', asOf: '2026-10-12' }, list('2026-10-12', '2026-10-18', 'Second week'));
    await putReadModel(db, { view: 'risk_list', key: '2026-10-19', asOf: '2026-10-19' }, list('2026-10-19', '2026-10-25', 'Third week'));
  });
  afterAll(async () => {
    await clearReadModels(db, ['risk_list']);
    await app.close();
    await db.close();
  });

  it('serves the latest list with every published week, oldest first', async () => {
    const res = await get('/api/risk-list');
    expect(res.statusCode).toBe(200);
    const body = res.json();
    expect(body.week).toEqual({ start: '2026-10-19', end: '2026-10-25' });
    expect(body.published).toEqual(['2026-10-05', '2026-10-12', '2026-10-19']);
    expect(body.rows[0].name).toBe('Third week');
    expect(body.note.paragraphs[0].lead).toBe('Third week');
  });

  it('takes any day of a week and serves that week as published', async () => {
    for (const day of ['2026-10-12', '2026-10-14', '2026-10-18']) {
      const body = (await get(`/api/risk-list?week=${day}`)).json();
      expect(body.week.start).toBe('2026-10-12');
      expect(body.rows[0].name).toBe('Second week');
      expect(body.published).toHaveLength(3);
    }
  });

  it('limits the weeks to the snapshot date', async () => {
    const body = (await get('/api/risk-list?asOf=2026-10-13')).json();
    expect(body.week.start).toBe('2026-10-12');
    expect(body.published).toEqual(['2026-10-05', '2026-10-12']);
  });

  it('answers 503 not_ready for a week with no list and 400 for a bad date', async () => {
    const res = await get('/api/risk-list?week=2026-09-30');
    expect(res.statusCode).toBe(503);
    expect(res.json()).toEqual({ error: 'not_ready', view: 'risk_list', key: '2026-09-28' });
    expect((await get('/api/risk-list?week=2026-13-01')).statusCode).toBe(400);
  });

  it('answers 503 not_ready when no list is published yet', async () => {
    expect((await get('/api/risk-list?asOf=2026-10-01')).json()).toEqual({ error: 'not_ready', view: 'risk_list', key: 'latest' });
  });

  it('exports a week in the screen’s columns, with vs last wk as shown and each record in the sources sheet', async () => {
    const why = [{ date: '2026-10-13', text: 'Pipeline now Feb 1 2027', tag: 'F', sourceName: 'FERC', sourceUrl: 'https://elibrary.ferc.gov/x' }];
    const odds = { value: 0.4, tag: 'M', asOf: '2026-10-12', methodVersion: 'odds v0' };
    const price = { value: 93.1, tag: 'M', asOf: '2026-10-12', methodVersion: 'price v0.1' };
    const doc = {
      ...list('2026-10-12', '2026-10-18', 'Second week'),
      rows: [
        { rank: 1, instrumentId: 'RL-A', name: 'Traded notes', size: '$2.0B', why, price, change1w: { ...price, value: -1.2 },
          fundMark: 'traded', gap: null, odds, move: { kind: 'up', by: 2 } },
        { rank: 2, instrumentId: 'RL-B', name: 'Marked notes', size: 'n/d', why: [], price: null, change1w: null,
          fundMark: 99.3, gap: -6.2, odds, move: { kind: 'same' } },
      ],
    };
    await putReadModel(db, { view: 'risk_list', key: '2026-10-12', asOf: '2026-10-12' }, doc);
    try {
      const res = await get('/api/export/risk-list.xlsx?week=2026-10-14');
      expect(res.statusCode).toBe(200);
      const wb = new ExcelJS.Workbook();
      await wb.xlsx.load(res.rawPayload as unknown as ArrayBuffer);
      const data = wb.getWorksheet('Data')!;
      const header = (data.getRow(1).values as unknown[]).slice(1);
      expect(header).toEqual(['rank', 'instrumentId', 'name', 'size', 'why', 'price', 'change1w', 'fundMark', 'gap', 'odds', 'vsLastWeek']);
      const row = (n: number) => Object.fromEntries(header.map((h, j) => [h, (data.getRow(n).values as unknown[]).slice(1)[j]]));
      expect(row(2)).toMatchObject({ name: 'Traded notes', fundMark: 'traded', gap: '—', vsLastWeek: '▲ 2', price: 93.1, odds: 0.4 });
      expect(String(row(2).why)).toContain('Pipeline now Feb 1 2027');
      expect(row(3)).toMatchObject({ name: 'Marked notes', price: '—', fundMark: 99.3, gap: -6.2, vsLastWeek: '—' });
      const sources = wb.getWorksheet('Sources')!;
      const urls = Array.from({ length: sources.rowCount }, (_, i) => (sources.getRow(i + 1).values as unknown[]).join(' '));
      expect(urls.some((line) => line.includes('https://elibrary.ferc.gov/x'))).toBe(true);
    } finally {
      await putReadModel(db, { view: 'risk_list', key: '2026-10-12', asOf: '2026-10-12' }, list('2026-10-12', '2026-10-18', 'Second week'));
    }
  });

  it('finds the Monday of any day', () => {
    expect(mondayOf('2026-10-19')).toBe('2026-10-19');
    expect(mondayOf('2026-10-25')).toBe('2026-10-19');
    expect(mondayOf('2026-10-21')).toBe('2026-10-19');
    expect(mondayOf('2027-01-01')).toBe('2026-12-28');
  });
});
