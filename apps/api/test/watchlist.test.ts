// Watchlist screen additions (spec 11): the fields the screen reads beyond the contract (site slug,
// sourced cushion and debt, dated catalyst), adding an issuer, and the Excel export per tab.
import ExcelJS from 'exceljs';
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { clearReadModels, makeApp, putReadModel, sourced, testDb } from './helpers.js';

const ISSUER = 'wl-test-issuer';
const SITE = 'wl-test-site';
const LOAN = 'WL-TEST-LOAN';
const BOND = 'WL-TEST-BOND';

describe('watchlist screen API', () => {
  let db: Db;
  let app: FastifyInstance;

  beforeAll(async () => {
    db = testDb();
    app = await makeApp({ mode: 'dev', db });
    await db.query(`INSERT INTO entity (id, name, kind) VALUES ($1, 'Lighthouse Holdings', 'developer')`, [ISSUER]);
    await db.query(`INSERT INTO site (slug, name, state) VALUES ($1, 'Lighthouse test', 'MI')`, [SITE]);
    await db.query(
      `INSERT INTO instrument (id, name, kind, issuer_entity_id, issuer_name, site_slug, cusip) VALUES
       ($1, 'Lighthouse construction loan', 'construction_loan', $3, 'Lighthouse Holdings', $4, NULL),
       ($2, 'Lighthouse 7% 2031', 'bond', $3, 'Lighthouse Holdings', NULL, '99999XAB1')`,
      [LOAN, BOND, ISSUER, SITE],
    );
    await putReadModel(db, { view: 'site_detail', key: SITE, asOf: '2026-10-15' }, {
      slug: SITE, name: 'Lighthouse', endUser: 'OpenAI', tenant: 'Oracle', status: 'Strained',
      cushionMonths: { ...sourced(7, 'M'), methodVersion: 'cushion v0.1' }, debtB: sourced(14, 'R'),
    });
    await putReadModel(db, { view: 'instrument_detail', key: LOAN, asOf: '2026-10-15' }, {
      id: LOAN, name: 'Lighthouse loan', sizeB: sourced(14, 'R'), mardenPrice: sourced(97.5, 'M'), change1w: sourced(-0.2, 'M'),
      markGap: sourced(-6.1, 'M'), chainStatus: 'Strained', nextMarksPublic: '2026-11-29',
    });
  });
  afterAll(async () => {
    await clearReadModels(db, ['site_detail', 'instrument_detail', 'home']);
    await db.query(`DELETE FROM alert WHERE org_id = 'dev' AND dedupe_key LIKE 'wl-test-%'`);
    await db.query(`DELETE FROM watchlist_item WHERE org_id = 'dev'`);
    await db.query(`DELETE FROM instrument WHERE id = ANY($1)`, [[LOAN, BOND]]);
    await db.query(`DELETE FROM site WHERE slug = $1`, [SITE]);
    await db.query(`DELETE FROM entity WHERE id = $1`, [ISSUER]);
    await app.close();
    await db.close();
  });

  const call = (method: 'GET' | 'POST', url: string, payload?: unknown) =>
    app.inject({ method, url, ...(payload === undefined ? {} : { payload: payload as Record<string, unknown> }) });

  it('lists what adding an issuer adds: the sites its instruments depend on, then its instruments', async () => {
    const res = await call('GET', `/api/watchlist/issuers/${ISSUER}`);
    expect(res.statusCode).toBe(200);
    expect(res.json()).toEqual({
      issuer: { id: ISSUER, name: 'Lighthouse Holdings' },
      items: [
        { kind: 'site', refId: SITE, name: 'Lighthouse test', typeLabel: 'Site', cusip: null, watchId: null },
        { kind: 'instrument', refId: BOND, name: 'Lighthouse 7% 2031', typeLabel: 'Bond', cusip: '99999XAB1', watchId: null },
        { kind: 'instrument', refId: LOAN, name: 'Lighthouse construction loan', typeLabel: 'Loan', cusip: null, watchId: null },
      ],
    });
    expect((await call('GET', '/api/watchlist/issuers/no-such-issuer')).statusCode).toBe(404);
  });

  it('adds the checked names in one request and marks them watched for the issuer', async () => {
    const add = await call('POST', '/api/watchlist/items', { items: [{ kind: 'site', refId: SITE }, { kind: 'instrument', refId: LOAN }] });
    expect(add.statusCode).toBe(201);
    const [siteId, loanId] = (add.json().items as { id: string }[]).map((i) => i.id);
    const after = (await call('GET', `/api/watchlist/issuers/${ISSUER}`)).json();
    expect(after.items.map((i: { watchId: string | null }) => i.watchId)).toEqual([siteId, null, loanId]);
  });

  it('serves the site slug, the sourced cushion and debt, and the dated next catalyst', async () => {
    await clearReadModels(db, ['home']);
    const items = (await call('GET', '/api/watchlist?asOf=2026-10-16')).json();
    expect(items).toHaveLength(2);
    expect(items[0]).toMatchObject({
      kind: 'site', refId: SITE, siteSlug: SITE, status: 'Strained', cushionMonths: 7,
      cushion: { value: 7, tag: 'M', methodVersion: 'cushion v0.1' }, debtB: sourced(14, 'R'),
      nextCatalyst: null, nextCatalystAt: null,
    });
    expect(items[1]).toMatchObject({
      kind: 'instrument', refId: LOAN, siteSlug: SITE, cushionMonths: 7, cushion: { value: 7, tag: 'M' },
      debtB: sourced(14, 'R'), markGap: sourced(-6.1, 'M'),
      nextCatalyst: 'Fund marks due', nextCatalystAt: '2026-11-29',
    });
  });

  it('takes the next catalyst from the home read model: the soonest step not yet past, its short line, named for the row', async () => {
    await putReadModel(db, { view: 'home', key: 'all', asOf: '2026-10-15' }, {
      comingUp: [
        { date: '2026-10-10', what: 'Already past', when: 'Early Oct', watch: [`site:${SITE}`], refUrl: `/sites/${SITE}` },
        { date: '2026-11-02', what: 'County board vote on the rezoning of the campus', when: 'Early Nov', watch: [`instrument:${LOAN}`], refUrl: `/instruments/${LOAN}` },
        { date: '2026-11-02', what: 'County board vote', when: 'Early Nov', watch: [`site:${SITE}`, `instrument:${LOAN}`], refUrl: `/sites/${SITE}` },
        { date: '2026-11-20', what: 'Lighthouse loan: lender consent due', when: 'Late Nov', watch: [`instrument:${LOAN}`], refUrl: `/instruments/${LOAN}` },
        { date: '2026-10-20', what: 'Another site step', when: 'Late Oct', watch: ['site:somewhere-else'], refUrl: '/sites/somewhere-else' },
      ],
    });
    const items = (await call('GET', '/api/watchlist?asOf=2026-10-16')).json();
    expect(items[0]).toMatchObject({ kind: 'site', nextCatalyst: 'County board vote', nextCatalystAt: '2026-11-02' });
    expect(items[1]).toMatchObject({ kind: 'instrument', nextCatalyst: 'County board vote', nextCatalystAt: '2026-11-02' });
    // Once that day has passed, the loan's next step is its own (its name dropped), still before its fund marks.
    const later = (await call('GET', '/api/watchlist?asOf=2026-11-05')).json();
    expect(later[0]).toMatchObject({ nextCatalyst: null, nextCatalystAt: null });
    expect(later[1]).toMatchObject({ nextCatalyst: 'Lender consent due', nextCatalystAt: '2026-11-20' });
  });

  it('marks all alerts read with {} or { all: true }, and refuses ids with all', async () => {
    await db.query(
      `INSERT INTO alert (org_id, user_id, kind, headline, body, tag, rule_label, ref_url, subject_type, subject_id,
                          severity, occurred_on, trigger_key, dedupe_key)
       VALUES ('dev', 'dev-user', 'power', 'Permit hearing moved', 'b', 'F', 'Power event', '/sites/${SITE}', 'site', $1,
               'amber', '2026-10-20', 'event:wl-test-1', 'wl-test-1')`,
      [SITE],
    );
    expect((await call('GET', '/api/alerts?unread=true')).json().length).toBeGreaterThan(0);
    expect((await call('POST', '/api/alerts/read', { all: true, ids: ['1'] })).statusCode).toBe(400);
    expect((await call('POST', '/api/alerts/read', { all: false })).statusCode).toBe(400);
    expect((await call('POST', '/api/alerts/read', { all: true })).statusCode).toBe(204);
    expect((await call('GET', '/api/alerts?unread=true')).json()).toEqual([]);
  });

  it('exports the rows of one tab with ?kind and refuses an unknown kind', async () => {
    const res = await call('GET', '/api/export/watchlist.xlsx?kind=instrument');
    expect(res.statusCode).toBe(200);
    const wb = new ExcelJS.Workbook();
    await wb.xlsx.load(res.rawPayload as unknown as ArrayBuffer);
    const data = wb.getWorksheet('Data')!;
    expect(data.rowCount).toBe(2);
    const header = (data.getRow(1).values as unknown[]).slice(1);
    expect(data.getRow(2).getCell(header.indexOf('refId') + 1).value).toBe(LOAN);
    expect((await call('GET', '/api/export/watchlist.xlsx')).statusCode).toBe(200);
    expect((await call('GET', '/api/export/watchlist.xlsx?kind=issuer')).statusCode).toBe(400);
  });
});
