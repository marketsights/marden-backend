// Read-model GETs: the stored doc is served as is, 503 not_ready when it has not been built, ?asOf
// picks the snapshot of that day, unknown subjects are 404, and filters act on the stored document.
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { clearReadModels, makeApp, putReadModel, sourced, testDb } from './helpers.js';

const VIEWS = ['completion', 'site_detail', 'site_tab:evidence', 'instrument_detail', 'gpu_collateral', 'risk_list', 'leases', 'obligations'];
const SLUG = 'rm-test-site';
const INSTRUMENT = 'RM-TEST-LOAN';

const row = (slug: string, state: string, cushion: number | null, endUser: string) => ({
  slug, name: slug, state, driver: 'Power', leadLenders: 'SMBC +3', debtB: sourced(1.5, 'R'),
  chain: [
    { role: 'end_user', party: endUser, status: 'green', reason: '' },
    { role: 'tenant', party: 'Tenant Co', status: 'amber', reason: '' },
    { role: 'developer', party: 'Dev Co', status: 'green', reason: '' },
    { role: 'lenders', party: 'Bank A, Bank B', status: 'green', reason: '' },
  ],
  projection: cushion === null ? null : { cushionMonths: sourced(cushion, 'M') },
  lastEvidence: { date: '2026-10-01', text: 'Filing', tag: 'F', sourceName: 'Test', sourceUrl: 'https://example.test' },
});

describe('read-model routes', () => {
  let db: Db;
  let app: FastifyInstance;

  beforeAll(async () => {
    db = testDb();
    app = await makeApp({ mode: 'dev', db });
    await db.query(`INSERT INTO site (slug, name, state) VALUES ($1, 'RM test site', 'TX')`, [SLUG]);
    await db.query(`INSERT INTO instrument (id, name, kind, issuer_name) VALUES ($1, 'RM loan', 'term_loan', 'RM Issuer')`, [INSTRUMENT]);
  });
  afterAll(async () => {
    await clearReadModels(db, VIEWS);
    await db.query(`DELETE FROM watchlist_item WHERE ref_id = $1`, [SLUG]);
    await db.query(`DELETE FROM instrument WHERE id = $1`, [INSTRUMENT]);
    await db.query(`DELETE FROM site WHERE slug = $1`, [SLUG]);
    await app.close();
    await db.close();
  });

  const get = (url: string) => app.inject({ method: 'GET', url });

  it('answers 503 not_ready while a read model has not been built', async () => {
    const res = await get('/api/completion');
    expect(res.statusCode).toBe(503);
    expect(res.json()).toEqual({ error: 'not_ready', view: 'completion', key: 'all' });
  });

  it('serves the stored document and picks the snapshot for ?asOf', async () => {
    const early = { asOf: '2026-10-01', summary: { marker: 'early' }, rows: [row(SLUG, 'TX', 4, 'OpenAI')] };
    const late = { asOf: '2026-10-15', summary: { marker: 'late' }, rows: [row(SLUG, 'TX', 1, 'OpenAI')] };
    await putReadModel(db, { view: 'completion', key: 'all', asOf: '2026-10-01' }, early);
    await putReadModel(db, { view: 'completion', key: 'all', asOf: '2026-10-15' }, late);
    expect((await get('/api/completion')).json()).toEqual(late);
    expect((await get('/api/completion?asOf=2026-10-14')).json()).toEqual(early);
    expect((await get('/api/completion?asOf=2026-10-15')).json()).toEqual(late);
    expect((await get('/api/completion?asOf=2026-09-30')).statusCode).toBe(503);
    expect((await get('/api/completion?asOf=2026-02-30')).statusCode).toBe(400);
    expect((await get('/api/alert-settings?asOf=yesterday')).statusCode).toBe(400);
  });

  it('filters completion rows server-side on the stored document', async () => {
    await putReadModel(db, { view: 'completion', key: 'all', asOf: '2026-10-16' }, {
      asOf: '2026-10-16', summary: {},
      rows: [row('a-site', 'TX', -2, 'OpenAI'), row('b-site', 'NM', 2, 'OpenAI'), row('c-site', 'TX', null, 'Microsoft'), row(SLUG, 'TX', 8, 'OpenAI')],
    });
    const slugs = async (qs: string) => ((await get(`/api/completion?${qs}`)).json().rows as { slug: string }[]).map((r) => r.slug);
    expect(await slugs('state=TX')).toEqual(['a-site', 'c-site', SLUG]);
    expect(await slugs('cushion=<0,0-3')).toEqual(['a-site', 'b-site']);
    expect(await slugs('cushion=none')).toEqual(['c-site']);
    expect(await slugs('endUser=microsoft')).toEqual(['c-site']);
    expect(await slugs('holder=smbc&state=NM')).toEqual(['b-site']);
    expect(await slugs('watchlist=true')).toEqual([]);
    await app.inject({ method: 'POST', url: '/api/watchlist/items', payload: { kind: 'site', refId: SLUG } });
    expect(await slugs('watchlist=true')).toEqual([SLUG]);
    expect((await get('/api/completion?cushion=soon')).statusCode).toBe(400);
  });

  it('answers 404 for an unknown site and 503 for a known site not built yet', async () => {
    expect((await get('/api/sites/no-such-site')).statusCode).toBe(404);
    expect((await get(`/api/sites/${SLUG}`)).json()).toEqual({ error: 'not_ready', view: 'site_detail', key: SLUG });
    const doc = { slug: SLUG, name: 'RM test site', status: 'Strained', cushionMonths: sourced(2, 'M') };
    await putReadModel(db, { view: 'site_detail', key: SLUG, asOf: '2026-10-15' }, doc);
    expect((await get(`/api/sites/${SLUG}`)).json()).toEqual(doc);
    expect((await get(`/api/sites/${SLUG}/evidence`)).json()).toEqual({ error: 'not_ready', view: 'site_tab:evidence', key: SLUG });
    await putReadModel(db, { view: 'site_tab:evidence', key: SLUG, asOf: '2026-10-15' }, { records: [] });
    expect((await get(`/api/sites/${SLUG}/evidence`)).json()).toEqual({ records: [] });
  });

  it('slices price history from the instrument detail document', async () => {
    const priceHistory = [
      { date: '2026-01-15', price: 99 }, { date: '2026-06-01', price: 97 }, { date: '2026-09-01', price: 93 }, { date: '2026-10-15', price: 89 },
    ];
    await putReadModel(db, { view: 'instrument_detail', key: INSTRUMENT, asOf: '2026-10-15' }, { id: INSTRUMENT, priceHistory, fundMarks: { low: 98, high: 99, filing: 'N-PORT', date: '2026-06-30' }, holders: { count: 0, parMm: 0, rows: [] } });
    const res = await get(`/api/instruments/${INSTRUMENT}/price-history?range=3m`);
    expect(res.json().points).toEqual(priceHistory.slice(2));
    expect((await get(`/api/instruments/${INSTRUMENT}/price-history`)).json().points).toHaveLength(4);
    expect((await get(`/api/instruments/${INSTRUMENT}/holders`)).json()).toEqual({ count: 0, parMm: 0, rows: [] });
    expect((await get(`/api/instruments/${INSTRUMENT}/price-history?range=2w`)).statusCode).toBe(400);
  });

  it('answers 404 for collateral on an instrument whose detail is built without GPU collateral', async () => {
    await clearReadModels(db, ['instrument_detail', 'gpu_collateral']);
    const url = `/api/instruments/${INSTRUMENT}/collateral`;
    expect((await get(url)).json()).toEqual({ error: 'not_ready', view: 'gpu_collateral', key: INSTRUMENT });
    await putReadModel(db, { view: 'instrument_detail', key: INSTRUMENT, asOf: '2026-10-15' }, { id: INSTRUMENT, priceHistory: [], holders: { count: 0, parMm: 0, rows: [] } });
    const res = await get(url);
    expect(res.statusCode).toBe(404);
    expect(res.json()).toEqual({ error: 'not_found', what: 'gpu_collateral' });
    // The detail says so, so the page does not ask for collateral it does not have.
    expect((await get(`/api/instruments/${INSTRUMENT}`)).json()).toMatchObject({ id: INSTRUMENT, hasCollateral: false });
    expect((await get(`${url}?asOf=2026-10-01`)).statusCode).toBe(503);
    const collateral = { instrumentId: INSTRUMENT, model: 'H100', units: sourced(1000, 'M'), ltvByQuarter: [] };
    await putReadModel(db, { view: 'gpu_collateral', key: INSTRUMENT, asOf: '2026-10-15' }, collateral);
    expect((await get(url)).json()).toEqual(collateral);
    expect((await get(`/api/instruments/${INSTRUMENT}`)).json()).toMatchObject({ id: INSTRUMENT, hasCollateral: true });
    expect((await get(`/api/instruments/${INSTRUMENT}?asOf=2026-10-14`)).statusCode).toBe(503);
    expect((await get('/api/instruments/NO-SUCH-INSTRUMENT/collateral')).statusCode).toBe(404);
  });

  it('serves the requested or latest risk list week', async () => {
    await putReadModel(db, { view: 'risk_list', key: '2026-10-05', asOf: '2026-10-05' }, { week: { start: '2026-10-05', end: '2026-10-11' }, rows: [] });
    await putReadModel(db, { view: 'risk_list', key: '2026-10-12', asOf: '2026-10-12' }, { week: { start: '2026-10-12', end: '2026-10-18' }, rows: [] });
    expect((await get('/api/risk-list')).json().week.start).toBe('2026-10-12');
    expect((await get('/api/risk-list?week=2026-10-05')).json().week.start).toBe('2026-10-05');
    expect((await get('/api/risk-list?asOf=2026-10-08')).json().week.start).toBe('2026-10-05');
    expect((await get('/api/risk-list?week=2026-09-28')).statusCode).toBe(503);
  });

  it('filters obligation edges and serves one lease from the lease map', async () => {
    const edge = (id: string, [from, to]: [string, string], type: string, totalB: number) => ({ id, from, to, type, status: 'Signed', totalB: sourced(totalB, 'F') });
    await putReadModel(db, { view: 'obligations', key: 'all', asOf: '2026-10-15' }, {
      asOf: '2026-10-15', kpis: { totalB: 0, filedPct: 0, changedThisMonth: 0 },
      nodes: [{ id: 'openai' }, { id: 'oracle' }, { id: 'coreweave' }],
      edges: [edge('e1', ['openai', 'oracle'], 'Compute purchase', 300), edge('e2', ['openai', 'coreweave'], 'Compute purchase', 22)],
    });
    const graph = (await get('/api/obligations?minB=100')).json();
    expect(graph.edges.map((e: { id: string }) => e.id)).toEqual(['e1']);
    expect(graph.nodes.map((n: { id: string }) => n.id)).toEqual(['openai', 'oracle']);
    expect((await get('/api/obligations?company=coreweave&format=table')).json().edges).toHaveLength(1);
    await putReadModel(db, { view: 'leases', key: 'all', asOf: '2026-10-15' }, { asOf: '2026-10-15', totals: {}, reconciliation: [], leases: [{ id: 'L1', lessee: 'Oracle' }] });
    expect((await get('/api/leases/L1')).json()).toEqual({ id: 'L1', lessee: 'Oracle' });
    expect((await get('/api/leases/L2')).statusCode).toBe(404);
  });

  it('adds request ids and security headers', async () => {
    const res = await app.inject({ method: 'GET', url: '/api/leases', headers: { 'x-request-id': 'req-abc.123', origin: 'http://localhost:3000' } });
    expect(res.headers['x-request-id']).toBe('req-abc.123');
    expect(res.headers['x-content-type-options']).toBe('nosniff');
    expect(res.headers['access-control-allow-origin']).toBe('http://localhost:3000');
    const other = await app.inject({ method: 'GET', url: '/api/leases', headers: { origin: 'https://evil.example' } });
    expect(other.headers['access-control-allow-origin']).toBeUndefined();
    expect(String(other.headers['x-request-id'])).toMatch(/^[0-9a-f-]{36}$/);
  });
});
