// The US Eastern business day on the API's own dated reads (lib/business-day.ts). ?asOf=D means "as Marden
// stood when D ended in New York", midnight Eastern after D; the session is UTC, so `D + 1` alone is 8 pm EDT
// and anything recorded that evening showed a day late. Every row is real, copied from the local `marden`
// database (public schema): FERC eLibrary's researcher run, which started at 22:08 New York time on
// 21 October, an alert the engine wrote, the Stingray site, HIFLD's SPP boundary; only the alert's, the site's
// and the boundary's instant is set, to 22:00 New York time on 21 October (and the alert is unread again; the
// boundary loses its parent link and, in its own test, its HIFLD as-of date). The saved scenario is one this
// test saves through the API. A fact's end is a New York day too: Abilene's SEC risk statement, copied with a
// valid_to of 22 October (no real fact has one yet), is current at 22:00 New York time on the 21st, when
// CURRENT_DATE in the UTC session is already the 22nd, and not after midnight.
import type { FastifyInstance } from 'fastify';
import { afterAll, afterEach, beforeAll, describe, expect, inject, it, vi } from 'vitest';
import { createDb, type Db } from '../src/db.js';
import { businessDay, businessToday, dayEnd, dayEndAt } from '../src/lib/business-day.js';
import { factSource } from '../src/lib/licensing.js';
import { asOfDay } from '../src/lib/params.js';
import { unreadAlerts } from '../src/queries/alerts.js';
import { resetIsoBoundaryCache } from '../src/queries/grid.js';
import { dbUrl, makeApp } from './helpers.js';

const DAY = '2026-10-21';
const BEFORE = '2026-10-20';
const EVENING = '2026-10-22T02:00:00.000Z'; // 22:00 EDT on 21 October
const RUN_SOURCE = 'ferc_fts';
const RUN_ID = 311; // FERC eLibrary's first researcher run: started 2026-10-22 02:08:48 UTC = 22:08 EDT on 21 October
const ALERT_KEY = 'model:cushion:site:jupiter:2026-10-21:a366c184a67a7a9b';
const SITE = 'stingray';
const BOUNDARY = 'iso:SPP';
const FACT_ID = 1080685; // site abilene, risk_evidence, SEC EDGAR, valid_from 2026-09-11
const ENDS = '2026-10-22'; // the valid_to set on its copy: current through 21 October in New York
const AFTER_MIDNIGHT = '2026-10-22T04:01:00.000Z'; // 00:01 EDT on 22 October

describe('US Eastern business-day cutoff', () => {
  let db: Db;
  let app: FastifyInstance;
  const get = async (url: string) => (await app.inject({ method: 'GET', url })).json();

  /** Copy real rows of `table` from public into the test schema: the shared columns, `skip` left to their
   *  defaults, `set` (column -> [value, SQL type]) overriding the copied value. */
  async function copy(table: string, where: string, params: unknown[],
    { set = {}, skip = [] }: { set?: Record<string, [unknown, string]>; skip?: string[] } = {}) {
    const cols = (await db.query<{ c: string }>(
      `SELECT t.column_name AS c FROM information_schema.columns t JOIN information_schema.columns p
          ON p.table_schema = 'public' AND p.table_name = t.table_name AND p.column_name = t.column_name
        WHERE t.table_schema = $1 AND t.table_name = $2 AND t.is_generated = 'NEVER' ORDER BY t.ordinal_position`,
      [inject('testSchema'), table])).rows.map((r) => r.c).filter((c) => !(c in set) && !skip.includes(c));
    const extra = Object.entries(set);
    const values = extra.map(([, [, type]], i) => `$${params.length + i + 1}::${type}`);
    const res = await db.query(
      `INSERT INTO ${table} (${[...cols, ...extra.map(([c]) => c)].join(', ')})
       SELECT ${[...cols, ...values].join(', ')} FROM public.${table} WHERE ${where} RETURNING *`,
      [...params, ...extra.map(([, [v]]) => v)]);
    expect(res.rowCount, `${table} ${where} in the local database`).toBeGreaterThan(0);
  }

  beforeAll(async () => {
    db = createDb(dbUrl(), 2);
    db.pool.on('connect', (client) => void client.query(`SET TIME ZONE 'UTC'`)); // as production's sessions
    app = await makeApp({ mode: 'dev', db });
    await copy('source', 'id = $1', [RUN_SOURCE]);
    await copy('source_run', 'id = $1', [RUN_ID], { skip: ['id'] });
    await copy('alert', 'dedupe_key = $1', [ALERT_KEY], {
      set: { at: [EVENING, 'timestamptz'], read_at: [null, 'timestamptz'] }, skip: ['id', 'event_id'],
    });
    await copy('site', 'slug = $1', [SITE], { set: { created_at: [EVENING, 'timestamptz'] } });
    await copy('grid_region', 'id = $1', [BOUNDARY], { set: { updated_at: [EVENING, 'timestamptz'], parent_id: [null, 'text'] } });
    await copy('fact', 'id = $1', [FACT_ID], { set: { valid_to: [ENDS, 'date'], document_id: [null, 'bigint'] }, skip: ['id'] });
  });
  afterEach(() => { vi.useRealTimers(); });
  afterAll(async () => {
    await db.query(`DELETE FROM stress_saved WHERE org_id = 'dev'`);
    await db.query(`DELETE FROM alert WHERE dedupe_key = $1`, [ALERT_KEY]);
    await db.query(`DELETE FROM site WHERE slug = $1`, [SITE]);
    await db.query(`DELETE FROM grid_region WHERE id = $1`, [BOUNDARY]);
    await db.query(`DELETE FROM ${inject('testSchema')}.fact WHERE dedupe_key = (SELECT dedupe_key FROM public.fact WHERE id = $1)`, [FACT_ID]);
    resetIsoBoundaryCache();
    await db.query(`DELETE FROM source_run WHERE source_id = $1`, [RUN_SOURCE]);
    await db.query(`DELETE FROM source WHERE id = $1`, [RUN_SOURCE]);
    await app.close();
    await db.close();
  });

  it('ends a day at midnight New York time, EDT or EST, in SQL and in JS', async () => {
    const ends: Record<string, string> = {
      '2026-10-21': '2026-10-22T04:00:00.000Z', '2026-12-01': '2026-12-02T05:00:00.000Z',
      '2026-03-07': '2026-03-08T05:00:00.000Z', '2026-03-08': '2026-03-09T04:00:00.000Z', // DST starts 8 March
      '2026-10-31': '2026-11-01T04:00:00.000Z', '2026-11-01': '2026-11-02T05:00:00.000Z', // and ends 1 November
    };
    for (const [day, end] of Object.entries(ends)) {
      expect(dayEndAt(day).toISOString()).toBe(end);
      const sql = await db.query<{ t: Date }>(`SELECT ${dayEnd('$1')} AS t`, [day]);
      expect(sql.rows[0]!.t.toISOString()).toBe(end);
    }
    expect(businessDay(new Date(EVENING))).toBe(DAY);
    expect(businessDay(new Date('2026-10-22T03:59:59Z'))).toBe(DAY); // 23:59:59 EDT
    expect(businessDay(new Date('2026-10-22T04:01:00Z'))).toBe('2026-10-22');
    expect(businessDay(new Date('2026-12-02T04:30:00Z'))).toBe('2026-12-01'); // 23:30 EST: still 1 December
    expect(businessDay(new Date('2026-12-02T05:00:00Z'))).toBe('2026-12-02'); // midnight EST
  });

  it('answers "today" in New York time from 8 pm on, not tomorrow', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(EVENING));
    expect(new Date().toISOString().slice(0, 10)).toBe('2026-10-22');
    expect(businessToday()).toBe(DAY);
    expect(asOfDay(null)).toBe(DAY);
  });

  it('shows a source run that started at 22:08 New York time on that day, not before', async () => {
    const on = (await get(`/api/sources/health?asOf=${DAY}`)) as { asOf: string; sources: { id: string; lastRun: { startedAt: string } | null; lastOkAt: string | null; status: string }[] };
    expect(on.asOf).toBe('2026-10-22T04:00:00.000Z');
    const ferc = on.sources.find((s) => s.id === RUN_SOURCE);
    expect(ferc?.lastRun?.startedAt).toBe('2026-10-22T02:08:48.141Z');
    expect(ferc?.lastOkAt).toBe('2026-10-22T02:10:18.276Z');
    expect(ferc?.status).toBe('ok');
    const before = (await get(`/api/sources/health?asOf=${BEFORE}`)) as typeof on;
    expect(before.sources.find((s) => s.id === RUN_SOURCE)).toMatchObject({ lastRun: null, lastOkAt: null, status: 'down' });
  });

  it('lists an alert raised at 22:00 New York time on that day, not before', async () => {
    const times = async (asOf: string) => ((await get(`/api/alerts?asOf=${asOf}`)) as { at: string }[]).map((a) => a.at);
    expect(await times(DAY)).toContain(EVENING);
    expect(await times(BEFORE)).not.toContain(EVENING);
    expect((await unreadAlerts(db, 'dev', 'dev-user', DAY)).latestAt).toBe(EVENING);
    expect((await unreadAlerts(db, 'dev', 'dev-user', BEFORE)).latestAt).not.toBe(EVENING);
  });

  it('lists a scenario saved at 22:00 New York time on that day, not before', async () => {
    const request = { scenario: 'tenant_downgrade', params: { rating: 'BB+' }, severity: 'base' };
    const saved = (await app.inject({ method: 'POST', url: '/api/stress/saved', payload: { name: 'Oracle to BB+', request } })).json();
    await db.query(`UPDATE stress_saved SET created_at = $1 WHERE id::text = $2`, [EVENING, saved.id]);
    const ids = async (asOf: string) => ((await get(`/api/stress/saved?asOf=${asOf}`)) as { id: string }[]).map((s) => s.id);
    expect(await ids(DAY)).toContain(saved.id);
    expect(await ids(BEFORE)).not.toContain(saved.id);
  });

  it('finds a site added at 22:00 New York time on that day, not before', async () => {
    const found = async (asOf: string) => ((await get(`/api/search?q=Stingray&asOf=${asOf}`)) as { results: { kind: string; id: string }[] })
      .results.some((r) => r.kind === 'site' && r.id === SITE);
    expect(await found(DAY)).toBe(true);
    expect(await found(BEFORE)).toBe(false);
  });

  it('dates a boundary with no HIFLD as-of date by the New York day it was updated', async () => {
    await db.query(`UPDATE grid_region SET meta = meta #- '{hifld,as_of}' WHERE id = $1`, [BOUNDARY]);
    resetIsoBoundaryCache();
    const fc = (await get('/api/grid/iso-rto')) as { features: { id: string; properties: { source: { asOf: string } } }[] };
    expect(fc.features.find((f) => f.id === BOUNDARY)?.properties.source.asOf).toBe(DAY); // not 2026-10-22 (UTC)
  });

  it('keeps a fact ending on the 22nd current at 22:00 New York time on the 21st, not after midnight', async () => {
    // The licence switch's own query (factSource with sources hidden): its "today" is businessToday().
    const current = async () => (await db.query(
      `SELECT id FROM ${factSource(null, '$1')} f WHERE subject_type = 'site' AND subject_id = 'abilene' AND field = 'risk_evidence'`,
      [['a source the licence switch hides']])).rowCount;
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date(EVENING));
    expect(await current()).toBe(1);
    vi.setSystemTime(new Date(AFTER_MIDNIGHT));
    expect(await current()).toBe(0);
  });

  it('ends a fact in fact_current on the New York date of now(), whatever the session zone', async () => {
    // CURRENT_DATE is the session zone's date: UTC+14 is a day ahead of New York from 6 am EDT on and UTC-11 a
    // day behind before 7 am, so at any hour one of these zones catches a view that compares with it.
    const fact = `${inject('testSchema')}.fact`;
    for (const zone of ['UTC', 'Pacific/Kiritimati', 'Pacific/Pago_Pago']) {
      await db.tx(async (c) => {
        await c.query(`SET LOCAL TIME ZONE '${zone}'`);
        const today = businessDay((await c.query<{ at: Date }>('SELECT now() AS at')).rows[0]!.at);
        const current = async () => (await c.query(
          `SELECT id FROM fact_current WHERE subject_type = 'site' AND subject_id = 'abilene' AND field = 'risk_evidence'`)).rowCount;
        const end = `UPDATE ${fact} SET valid_to = $1::date + $2::int
                      WHERE dedupe_key = (SELECT dedupe_key FROM public.fact WHERE id = $3)`;
        await c.query(end, [today, 1, FACT_ID]);
        expect(await current(), zone).toBe(1);
        await c.query(end, [today, 0, FACT_ID]);
        expect(await current(), zone).toBe(0);
        await c.query(`UPDATE ${fact} SET valid_to = $1::date WHERE dedupe_key = (SELECT dedupe_key FROM public.fact WHERE id = $2)`, [ENDS, FACT_ID]);
      });
    }
  });
});
