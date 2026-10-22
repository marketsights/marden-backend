// MARDEN_SHOW_UNLICENSED_PRICES on the API's own fact reads (docs/methods/licensing.md §3), on real rows: the
// Oracle 5.2% 2035 note's facts (finra.org TRACE security-master facts beside its SEC EDGAR terms), the Abilene
// on-site plant's TCEQ air-permit fact and that plant's latest plant_row document are copied from the local
// `marden` database (public schema) into the test schema. With the switch true every row is served; with it
// false no row from an 'unlicensed-commercial' source reaches the fact source the permit search and the plant
// search box use, nor the staff review queue, and licensed rows are untouched.
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { loadConfig } from '../src/config.js';
import type { Db } from '../src/db.js';
import { factSource, hiddenSources, UNLICENSED_SOURCE_NAMES } from '../src/lib/licensing.js';
import { dbUrl, makeApp, testDb } from './helpers.js';

const ORACLE = 'EX-ORCL-239';
const PLANT = 'n-abilene-onsite';
const TRACE = 'FINRA TRACE (finra.org)';
const COLS = `subject_type, subject_id, field, value, unit, tag, source_name, source_url, quote, quote_loc, valid_from,
  valid_to, learned_at, status, method_version, producer, confidence, verifier, material, dedupe_key`;

interface FactRow { id: string; field: string; source_name: string; subject_type: string; subject_id: string; value: unknown }

describe('unlicensed-price switch in the API', () => {
  let db: Db;
  let shown: FastifyInstance;
  let hidden: FastifyInstance;
  let copied: FactRow[] = [];
  let permit: FactRow | undefined;
  /** A permit number the plant's facts list but its plant_row document does not show (found only via the facts). */
  let factOnly: string | undefined;
  let reviewId: string | null = null;

  beforeAll(async () => {
    db = testDb();
    shown = await makeApp({ mode: 'dev', db, env: { MARDEN_SHOW_UNLICENSED_PRICES: 'true' } });
    hidden = await makeApp({ mode: 'dev', db, env: { MARDEN_SHOW_UNLICENSED_PRICES: 'false' } });
    // Real rows: every published fact of the Oracle note, the plant's air-permit fact and its plant_row document.
    copied = (await db.query<FactRow>(
      `INSERT INTO fact (${COLS})
       SELECT ${COLS} FROM public.fact
        WHERE status = 'published' AND ((subject_type = 'instrument' AND subject_id = $1)
           OR (subject_type = 'plant' AND subject_id = $2 AND field = 'air_permit_number'))
       ON CONFLICT (dedupe_key) DO NOTHING
       RETURNING id::text, field, source_name, subject_type, subject_id, value`, [ORACLE, PLANT])).rows;
    permit = copied.find((r) => r.field === 'air_permit_number');
    const doc = (await db.query<{ doc: unknown }>(
      `INSERT INTO read_model (view, key, as_of, doc)
       SELECT view, key, as_of, doc FROM public.read_model WHERE view = 'plant_row' AND key = $1
        ORDER BY as_of DESC LIMIT 1
       ON CONFLICT (view, key, as_of) DO NOTHING RETURNING doc`, [PLANT])).rows[0]?.doc;
    if (permit && doc) {
      const text = JSON.stringify(doc).toLowerCase();
      factOnly = String(permit.value).split(',').map((t) => t.trim()).find((t) => t.length > 4 && !text.includes(t.toLowerCase()));
    }
    const trace = copied.find((r) => r.source_name === TRACE);
    if (trace) {
      reviewId = (await db.query<{ id: string }>(
        `INSERT INTO review_item (kind, ref_type, ref_id, title, reason, payload)
         VALUES ('fact', 'fact', $1, 'licensing-test TRACE fact', 'licence switch test', '{"area": "licensing-test"}')
         RETURNING id::text`, [trace.id])).rows[0]!.id;
    }
  });
  afterAll(async () => {
    if (reviewId) await db.query(`DELETE FROM review_item WHERE id = $1`, [reviewId]);
    await db.query(`DELETE FROM read_model WHERE view = 'plant_row' AND key = $1`, [PLANT]);
    await db.query(`DELETE FROM fact WHERE id = ANY($1::bigint[])`, [copied.map((r) => r.id)]);
    await shown.close();
    await hidden.close();
    await db.close();
  });

  const traceRows = () => copied.filter((r) => r.source_name === TRACE);

  it('reads the switch like the Python services: default true, false hides every unlicensed name', () => {
    const base = { NODE_ENV: 'test', DATABASE_URL: dbUrl(), MARDEN_AUTH_MODE: 'dev' };
    expect(loadConfig(base).showUnlicensedPrices).toBe(true);
    expect(loadConfig({ ...base, MARDEN_SHOW_UNLICENSED_PRICES: ' ' }).showUnlicensedPrices).toBe(true);
    expect(loadConfig({ ...base, MARDEN_SHOW_UNLICENSED_PRICES: 'FALSE' }).showUnlicensedPrices).toBe(false);
    expect(loadConfig({ ...base, MARDEN_SHOW_UNLICENSED_PRICES: 'no' }).showUnlicensedPrices).toBe(false);
    expect(loadConfig({ ...base, MARDEN_SHOW_UNLICENSED_PRICES: '1' }).showUnlicensedPrices).toBe(true);
    expect(() => loadConfig({ ...base, MARDEN_SHOW_UNLICENSED_PRICES: 'maybe' })).toThrow(/MARDEN_SHOW_UNLICENSED_PRICES/);
    expect(hiddenSources(true)).toEqual([]);
    expect(hiddenSources(false)).toEqual(UNLICENSED_SOURCE_NAMES);
    expect(shown.hiddenSources).toEqual([]);
    expect(hidden.hiddenSources).toContain(TRACE);
  });

  it('fact source: real TRACE rows served while true, left out while false, licensed rows kept', async (ctx) => {
    if (traceRows().length === 0) ctx.skip();
    const read = async (src: string, params: unknown[]) => (await db.query<{ id: string; field: string; source_name: string }>(
      `SELECT id::text, field, source_name FROM ${src} f WHERE subject_type = 'instrument' AND subject_id = $1 ORDER BY field`,
      [ORACLE, ...params])).rows;
    const current = await read(factSource(null, null), []);
    expect(current.map((r) => r.source_name)).toContain(TRACE);
    // Nothing hidden: the filtered precedence picks exactly the fact_current row of every field.
    expect(await read(factSource(null, '$2'), [['(no such source)']])).toEqual(current);
    const filtered = await read(factSource(null, '$2'), [UNLICENSED_SOURCE_NAMES]);
    expect(filtered.filter((r) => UNLICENSED_SOURCE_NAMES.includes(r.source_name))).toEqual([]);
    expect(filtered).toEqual(current.filter((r) => r.source_name !== TRACE)); // the note's other fields stay
    const day = new Date().toISOString().slice(0, 10);
    const asOf = await read(factSource('$2', null), [day]);
    expect(asOf.map((r) => r.source_name)).toContain(TRACE);
    expect((await read(factSource('$2', '$3'), [day, UNLICENSED_SOURCE_NAMES])).map((r) => r.source_name)).not.toContain(TRACE);
  });

  it('search: a real licensed permit number is found with the switch either way', async (ctx) => {
    if (!permit) ctx.skip();
    const number = factOnly ?? String(permit!.value).split(',')[0]!.trim();
    for (const app of [shown, hidden]) {
      const res = await app.inject({ method: 'GET', url: `/api/search?q=${encodeURIComponent(number)}` });
      expect(res.statusCode).toBe(200);
      const hits = (res.json().results as { kind: string; id: string }[]).filter((r) => r.kind === 'permit');
      expect(hits.map((h) => h.id)).toContain(permit!.subject_id);
      const past = await app.inject({ method: 'GET', url: `/api/search?q=${encodeURIComponent(number)}&asOf=2026-01-01` });
      expect(past.statusCode).toBe(200); // not known then
      expect((past.json().results as { kind: string }[]).filter((r) => r.kind === 'permit')).toEqual([]);
    }
  });

  it('plant search box: a permit number only the licensed fact holds finds the plant with the switch either way', async (ctx) => {
    if (!factOnly) ctx.skip();
    for (const app of [shown, hidden]) {
      const res = await app.inject({ method: 'GET', url: `/api/power-plants?q=${encodeURIComponent(factOnly!)}` });
      expect(res.statusCode).toBe(200);
      expect((res.json().rows as { id: string }[]).map((r) => r.id)).toEqual([PLANT]);
      const past = await app.inject({ method: 'GET', url: `/api/power-plants?q=${encodeURIComponent(factOnly!)}&asOf=2026-01-01` });
      expect([200, 503]).toContain(past.statusCode); // no plant_row snapshot that early: not served either way
    }
  });

  it('review queue: an item about a real TRACE fact is served while true and absent while false', async (ctx) => {
    if (!reviewId) ctx.skip();
    const list = async (app: FastifyInstance) => {
      const res = await app.inject({ method: 'GET', url: '/api/review?area=licensing-test' });
      expect(res.statusCode).toBe(200);
      return res.json() as { items: { id: string; sourceName: string }[]; total: number };
    };
    const open = await list(shown);
    expect(open.items.map((i) => [i.id, i.sourceName])).toEqual([[reviewId, TRACE]]);
    expect(open.total).toBe(1);
    expect((await shown.inject({ method: 'GET', url: `/api/review/${reviewId}` })).json()).toMatchObject({ sourceName: TRACE });
    const closed = await list(hidden);
    expect(closed).toEqual({ items: [], total: 0 });
    expect((await hidden.inject({ method: 'GET', url: `/api/review/${reviewId}` })).statusCode).toBe(404);
    expect((await hidden.inject({ method: 'POST', url: `/api/review/${reviewId}/approve`, payload: {} })).statusCode).toBe(404);
  });
});
