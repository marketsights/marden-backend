// User-data endpoints on the app tables (dev org): watchlist CRUD joined to read models, alerts,
// alert settings defaults, Home layout, saved stress, plus source heartbeats and search.
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { clearReadModels, makeApp, putReadModel, sourced, testDb } from './helpers.js';

const SITE = 'ud-test-site';
const LOAN = 'UD-TEST-LOAN';

describe('user data', () => {
  let db: Db;
  let app: FastifyInstance;

  beforeAll(async () => {
    db = testDb();
    app = await makeApp({ mode: 'dev', db });
    await db.query(`INSERT INTO site (slug, name, state) VALUES ($1, 'Frontier test', 'TX')`, [SITE]);
    await db.query(`INSERT INTO instrument (id, name, kind, issuer_name, site_slug, cusip) VALUES ($1, 'Frontier construction loan', 'construction_loan', 'Frontier SPV', $2, '12345ABC9')`, [LOAN, SITE]);
  });
  afterAll(async () => {
    await clearReadModels(db, ['site_detail', 'instrument_detail']);
    await db.query(`DELETE FROM watchlist_item WHERE org_id = 'dev'`);
    await db.query(`DELETE FROM alert WHERE org_id = 'dev'`);
    await db.query(`DELETE FROM alert_settings WHERE org_id = 'dev'`);
    await db.query(`DELETE FROM home_layout WHERE org_id = 'dev'`);
    await db.query(`DELETE FROM stress_saved WHERE org_id = 'dev'`);
    await db.query(`DELETE FROM instrument WHERE id = $1`, [LOAN]);
    await db.query(`DELETE FROM site WHERE slug = $1`, [SITE]);
    await db.query(`DELETE FROM source_run WHERE source_id LIKE 'ud_%'`);
    await db.query(`DELETE FROM source WHERE id LIKE 'ud_%'`);
    await db.query(`DELETE FROM fact WHERE dedupe_key LIKE 'ud-permit%'`);
    await app.close();
    await db.close();
  });

  const call = (method: 'GET' | 'POST' | 'PUT' | 'DELETE', url: string, payload?: unknown) =>
    app.inject({ method, url, ...(payload === undefined ? {} : { payload: payload as Record<string, unknown> }) });

  it('adds, lists (joined to read models) and removes watchlist items', async () => {
    await putReadModel(db, { view: 'site_detail', key: SITE, asOf: '2026-10-15' }, { slug: SITE, name: 'Frontier', endUser: 'OpenAI', tenant: 'Oracle', status: 'Strained', cushionMonths: sourced(2, 'M') });
    await putReadModel(db, { view: 'instrument_detail', key: LOAN, asOf: '2026-10-15' }, { id: LOAN, name: 'Frontier loan', sizeB: sourced(23, 'R'), mardenPrice: sourced(94.2, 'M'), change1w: sourced(-0.4, 'M'), markGap: 'no fund', chainStatus: 'Strained' });
    const add = await call('POST', '/api/watchlist/items', { items: [{ kind: 'site', refId: SITE }, { kind: 'instrument', refId: LOAN }] });
    expect(add.statusCode).toBe(201);
    const again = await call('POST', '/api/watchlist/items', { kind: 'site', refId: SITE });
    expect(again.json().items[0]).toMatchObject({ id: add.json().items[0].id, created: false });
    expect((await call('POST', '/api/watchlist/items', { kind: 'site', refId: 'missing-site' })).statusCode).toBe(404);
    expect((await call('POST', '/api/watchlist/items', { kind: 'issuer', refId: SITE })).statusCode).toBe(400);

    const items = (await call('GET', '/api/watchlist')).json();
    expect(items).toEqual([
      { id: add.json().items[0].id, kind: 'site', refId: SITE, name: 'Frontier', typeLabel: 'Site', sub: 'OpenAI → Oracle', status: 'Strained', cushionMonths: 2, price: null, change1w: null, markGap: null, nextCatalyst: null },
      { id: add.json().items[1].id, kind: 'instrument', refId: LOAN, name: 'Frontier loan', typeLabel: 'Loan', sub: '$23B', status: 'Strained', cushionMonths: 2, price: sourced(94.2, 'M'), change1w: sourced(-0.4, 'M'), markGap: 'no fund', nextCatalyst: null },
    ]);
    const id = add.json().items[0].id as string;
    expect((await call('DELETE', `/api/watchlist/items/${id}`)).statusCode).toBe(204);
    expect((await call('DELETE', `/api/watchlist/items/${id}`)).statusCode).toBe(404);
    expect((await call('GET', '/api/watchlist')).json()).toHaveLength(1);
    const audit = await db.query<{ action: string }>(
      `SELECT action FROM audit_log WHERE org_id = 'dev' AND action LIKE 'watchlist.%' AND target = ANY($1) ORDER BY id`,
      [[`site:${SITE}`, `instrument:${LOAN}`]],
    );
    expect(audit.rows.map((r) => r.action)).toEqual(['watchlist.add', 'watchlist.add', 'watchlist.remove']);
  });

  it('returns default alert settings until the user saves their own', async () => {
    const defaults = (await call('GET', '/api/alert-settings')).json();
    expect(defaults).toEqual({
      types: { cushion: true, milestone: true, power: true, opposition: true, tenant: true, price_or_mark: true, construction_stall: true },
      instantRed: true, dailyDigest: true, weeklyNote: true, priceThresholdPts: 2,
    });
    const next = { ...defaults, dailyDigest: false, priceThresholdPts: 3, types: { ...defaults.types, opposition: false } };
    expect((await call('PUT', '/api/alert-settings', next)).json()).toEqual(next);
    expect((await call('GET', '/api/alert-settings')).json()).toEqual(next);
    expect((await call('PUT', '/api/alert-settings', { ...next, priceThresholdPts: 5 })).statusCode).toBe(400);
  });

  it('returns the six default Home widgets and saves a layout', async () => {
    const layout = (await call('GET', '/api/home/layout')).json();
    expect(layout.widgets.map((w: { key: string }) => w.key)).toEqual(['headline', 'watchlist_changes', 'alerts', 'tightest_cushions', 'risk_top5', 'coming_up']);
    const mine = { widgets: [{ key: 'alerts' }, { key: 'site_schedule', params: { slug: SITE } }] };
    expect((await call('PUT', '/api/home/layout', mine)).json()).toEqual(mine);
    expect((await call('GET', '/api/home/layout')).json()).toEqual(mine);
    expect((await call('PUT', '/api/home/layout', { widgets: [{ key: 'pie_chart' }] })).statusCode).toBe(400);
  });

  it('lists alerts and marks them read', async () => {
    await db.query(
      `INSERT INTO alert (org_id, kind, headline, tag, rule_label, ref_url, subject_type, subject_id)
       VALUES ('dev', 'power', 'Permit hearing moved', 'F', 'Power event', '/sites/${SITE}', 'site', $1),
              ('dev', 'cushion', 'Cushion under 3 months', 'M', 'Cushion < 3', '/sites/${SITE}', 'site', $1)`,
      [SITE],
    );
    const alerts = (await call('GET', '/api/alerts')).json();
    expect(alerts).toHaveLength(2);
    expect(alerts[0]).toMatchObject({ read: false, emailed: false, tag: expect.stringMatching(/^[FM]$/) as unknown });
    expect((await call('POST', '/api/alerts/read', { ids: [alerts[0].id] })).statusCode).toBe(204);
    expect((await call('GET', '/api/alerts?unread=true')).json()).toHaveLength(1);
    expect((await call('POST', '/api/alerts/read', {})).statusCode).toBe(204);
    expect((await call('GET', '/api/alerts?unread=true')).json()).toHaveLength(0);
  });

  it('validates stress runs, answers 503 until the stress model exists, and saves scenarios', async () => {
    expect((await call('POST', '/api/stress/run', { scenario: 'meteor', params: {}, severity: 'base' })).statusCode).toBe(400);
    const run = await call('POST', '/api/stress/run', { scenario: 'tenant_downgrade', params: { rating: 'BB+' }, severity: 'severe' });
    expect(run.statusCode).toBe(503);
    expect(run.json()).toEqual({ error: 'not_ready', view: 'model:stress', key: 'tenant_downgrade' });
    const saved = await call('POST', '/api/stress/saved', { name: 'Oracle to BB+', request: { scenario: 'tenant_downgrade', params: { rating: 'BB+' }, severity: 'base' } });
    expect(saved.statusCode).toBe(201);
    expect(saved.json()).toMatchObject({ name: 'Oracle to BB+', result: null, ranAt: null, createdBy: 'dev-user' });
    expect((await call('GET', `/api/stress/saved/${saved.json().id}`)).json()).toEqual(saved.json());
    expect((await call('GET', '/api/stress/saved')).json()).toHaveLength(1);
    expect((await call('GET', '/api/stress/saved/00000000-0000-0000-0000-000000000000')).statusCode).toBe(404);
  });

  it('reports source heartbeats as ok, delayed or down', async () => {
    await db.query(
      `INSERT INTO source (id, name, kind, cadence_minutes, last_ok_at, blocked) VALUES
         ('ud_fresh', 'Fresh feed', 'api', 60, now() - interval '30 minutes', false),
         ('ud_late', 'Late feed', 'api', 60, now() - interval '150 minutes', false),
         ('ud_dead', 'Dead feed', 'portal', 60, now() - interval '2 days', false),
         ('ud_never', 'Never ran', 'portal', 1440, NULL, false)`,
    );
    await db.query(`INSERT INTO source_run (source_id, started_at, finished_at, ok, items_seen, items_new) VALUES ('ud_fresh', now() - interval '31 minutes', now() - interval '30 minutes', true, 12, 3)`);
    const health = (await call('GET', '/api/sources/health')).json();
    const status = Object.fromEntries((health.sources as { id: string; status: string }[]).map((s) => [s.id, s.status]));
    expect(status).toMatchObject({ ud_fresh: 'ok', ud_late: 'delayed', ud_dead: 'down', ud_never: 'down' });
    const fresh = (health.sources as { id: string; lastRun: unknown }[]).find((s) => s.id === 'ud_fresh');
    expect(fresh?.lastRun).toMatchObject({ ok: true, itemsSeen: 12, itemsNew: 3 });
  });

  it('searches sites, instruments by CUSIP and permit numbers', async () => {
    await db.query(
      `INSERT INTO fact (subject_type, subject_id, field, value, tag, source_name, valid_from, status, producer, dedupe_key)
       VALUES ('site', $1, 'air_permit_number', '"PSDTX1692"', 'F', 'TCEQ', '2025-12-12', 'published', 'test', 'ud-permit')`,
      [SITE],
    );
    const kinds = async (q: string) => ((await call('GET', `/api/search?q=${encodeURIComponent(q)}`)).json().results as { kind: string; id: string }[]);
    expect((await kinds('Frontier')).map((r) => r.kind)).toEqual(expect.arrayContaining(['site', 'instrument']));
    expect(await kinds('12345ABC')).toEqual([expect.objectContaining({ kind: 'instrument', id: LOAN })]);
    expect(await kinds('PSDTX1692')).toEqual([expect.objectContaining({ kind: 'permit', id: SITE, label: 'PSDTX1692' })]);
    expect((await call('GET', '/api/search?q=x')).statusCode).toBe(400);
  });

  it('finds permit numbers through fact_current, and through fact_asof for ?asOf', async () => {
    await db.query(`UPDATE fact SET learned_at = '2025-12-13T00:00:00Z' WHERE dedupe_key = 'ud-permit'`);
    await db.query(
      `INSERT INTO fact (subject_type, subject_id, field, value, tag, source_name, valid_from, status, producer, dedupe_key, learned_at)
       VALUES ('site', $1, 'air_permit_number', '"PSDTX1692M1"', 'F', 'TCEQ', '2026-06-01', 'published', 'test', 'ud-permit-2', '2026-06-02T00:00:00Z'),
              ('site', $1, 'air_permit_number', '"PSDTX1692X"', 'F', 'TCEQ', '2026-07-01', 'candidate', 'test', 'ud-permit-3', '2026-07-02T00:00:00Z')`,
      [SITE],
    );
    const labels = async (qs: string) =>
      ((await call('GET', `/api/search?q=PSDTX1692${qs}`)).json().results as { kind: string; label: string }[])
        .filter((r) => r.kind === 'permit').map((r) => r.label);
    expect(await labels('')).toEqual(['PSDTX1692M1']); // the current value; the candidate is not published
    expect(await labels('&asOf=2026-01-15')).toEqual(['PSDTX1692']); // what Marden knew then
    expect(await labels('&asOf=2026-06-01')).toEqual(['PSDTX1692']); // amendment not learned until Jun 2
    expect(await labels('&asOf=2025-12-01')).toEqual([]);
  });
});
