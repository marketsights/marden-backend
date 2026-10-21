// GET /api/risk-list (spec 10): the published weeks for the week picker, ?week= on any day of its week,
// frozen past lists served as stored, and the snapshot date limiting which weeks exist.
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

  it('finds the Monday of any day', () => {
    expect(mondayOf('2026-10-19')).toBe('2026-10-19');
    expect(mondayOf('2026-10-25')).toBe('2026-10-19');
    expect(mondayOf('2026-10-21')).toBe('2026-10-19');
    expect(mondayOf('2027-01-01')).toBe('2026-12-28');
  });
});
