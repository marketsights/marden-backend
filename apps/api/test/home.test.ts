// GET /api/home: the `home` read model (every name's pieces) cut to the org's watchlist, the headline
// summed over the watched names only, the caller's price threshold applied, copies of one record shown
// once, unread alerts added; 503 not_ready until the read model is built. Search finds instruments by
// their issuer and site words. Docs here follow HomeDoc (contracts/app.ts) as the Python builder writes it.
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { HomeDoc, HomeResponse } from '../src/contracts/app.js';
import type { Db } from '../src/db.js';
import { clearReadModels, makeApp, putReadModel, sourced, testDb } from './helpers.js';

const SITE = 'hm-site';
const LOAN = 'HM-LOAN';
const NOTE = 'HM-NOTE';
const DAY = '2026-10-21';
const ev = (text: string, date = '2026-10-20') => ({ date, text, tag: 'F' as const, sourceName: 'SEC EDGAR', sourceUrl: 'https://example.test/8k' });
const m = (value: number) => ({ value, tag: 'M' as const, asOf: DAY, sourceName: 'Marden model', methodVersion: 'price v0.1' });

const doc: HomeDoc = {
  asOf: DAY,
  week: { start: '2026-10-15', end: DAY },
  under3: [
    { watch: [`site:${SITE}`], name: 'HM site', debtB: sourced(18, 'F'), cushionMonths: m(-1) },
    { watch: [`instrument:${LOAN}`], site: SITE, name: 'HM construction loan', debtB: sourced(12, 'R', '2026-10-18'), cushionMonths: m(-1) },
  ],
  cushionKnown: [`site:${SITE}`, `instrument:${LOAN}`],
  statusChanges: [{ watch: [`site:${SITE}`, `instrument:${LOAN}`], name: 'HM site', line: 'HM site to Broken', at: DAY }],
  fundMarksHigh: [{ watch: [`instrument:${NOTE}`], name: 'HM 9% notes', gapPts: m(-7.5) }],
  changes: [
    { severity: 3, watch: [`site:${SITE}`, `instrument:${LOAN}`], kind: 'site', refId: SITE, name: 'HM site', type: 'status', label: 'Tenant link', from: 'amber', to: { ...sourced('red', 'F'), value: 'red' }, status: 'Broken', record: ev('Force majeure notice') },
    { severity: 2, watch: [`instrument:${NOTE}`], kind: 'instrument', refId: NOTE, name: 'HM 9% notes', type: 'price', from: 90, to: m(87.5), status: null, record: ev('Marden price 90.0 to 87.5 over the week', DAY) },
    { severity: 1, watch: [`instrument:${LOAN}`], kind: 'instrument', refId: LOAN, name: 'HM construction loan', type: 'price', from: 87.5, to: m(89), status: 'Broken', record: ev('Marden price 87.5 to 89.0 over the week', DAY) },
    { severity: 1, watch: [`site:${SITE}`, `instrument:${LOAN}`], kind: 'site', refId: SITE, name: 'HM site', type: 'filing', label: '8-K', from: null, to: sourced('8-K: Other Events', 'F'), status: 'Broken', record: ev('8-K: Other Events') },
    { severity: 1, watch: [`instrument:${LOAN}`], kind: 'instrument', refId: LOAN, name: 'HM construction loan', type: 'filing', label: '8-K', from: null, to: sourced('8-K: Other Events', 'F'), status: 'Broken', record: ev('8-K: Other Events') },
    { severity: 3, watch: ['site:hm-other'], kind: 'site', refId: 'hm-other', name: 'Other site', type: 'cushion', from: 4, to: m(-2), status: 'Broken', record: ev('Slip') },
  ],
  comingUp: [
    { watch: [`site:${SITE}`, `instrument:${LOAN}`], date: '2026-10-26', when: 'Late Oct', what: 'Permit hearing', refUrl: `/sites/${SITE}` },
    { watch: [`instrument:${LOAN}`], date: '2026-10-26', when: 'Late Oct', what: 'Permit hearing', refUrl: `/instruments/${LOAN}` },
    { watch: [`instrument:${NOTE}`], date: '2026-10-30', when: 'Late Oct', what: 'HM 9% notes: next N-PORT marks public', refUrl: `/instruments/${NOTE}` },
    { watch: ['site:hm-other'], date: '2026-11-02', when: 'Early Nov', what: 'Other step', refUrl: '/sites/hm-other' },
  ],
};

describe('home', () => {
  let db: Db;
  let app: FastifyInstance;
  const home = async (): Promise<HomeResponse> => (await app.inject({ method: 'GET', url: '/api/home' })).json();
  const watch = (kind: 'site' | 'instrument', refId: string) =>
    app.inject({ method: 'POST', url: '/api/watchlist/items', payload: { kind, refId } });

  beforeAll(async () => {
    db = testDb();
    app = await makeApp({ mode: 'dev', db });
    await db.query(`DELETE FROM watchlist_item WHERE org_id = 'dev'`);
    await db.query(`INSERT INTO site (slug, name, state) VALUES ($1, 'HM site', 'TX')`, [SITE]);
    await db.query(
      `INSERT INTO instrument (id, name, kind, issuer_name, site_slug) VALUES ($1, 'Construction loan', 'construction_loan', 'HM Builder SPV', $3),
              ($2, '9.00% Sr Notes 2031', 'bond', 'Hypothetical Marden Cloud Inc.', NULL)`,
      [LOAN, NOTE, SITE],
    );
  });
  afterAll(async () => {
    await clearReadModels(db, ['home']);
    await db.query(`DELETE FROM watchlist_item WHERE org_id = 'dev'`);
    await db.query(`DELETE FROM alert_settings WHERE org_id = 'dev'`);
    await db.query(`DELETE FROM instrument WHERE id = ANY($1)`, [[LOAN, NOTE]]);
    await db.query(`DELETE FROM site WHERE slug = $1`, [SITE]);
    await app.close();
    await db.close();
  });

  it('answers 503 not_ready until the home read model is built', async () => {
    const res = await app.inject({ method: 'GET', url: '/api/home' });
    expect(res.statusCode).toBe(503);
    expect(res.json()).toEqual({ error: 'not_ready', view: 'home', key: 'all' });
  });

  it('shows nothing for an empty watchlist: n/d, not zeros', async () => {
    await putReadModel(db, { view: 'home', key: 'all', asOf: DAY }, doc);
    const body = await home();
    expect(body.watched).toEqual({ sites: [], instruments: [] });
    expect(body.headline.watchedDebtUnder3.debtB.value).toBeNull();
    expect(body.headline.statusChangesThisWeek).toEqual({ count: 0, names: [] });
    expect(body.headline.fundMarksHigh).toEqual({ count: 0, topName: null, gapPts: null });
    expect(body.watchlistChanges).toEqual([]);
    expect(body.comingUp).toEqual([]);
    expect(body.unreadAlerts).toEqual({ count: 0, latestAt: null });
  });

  it('keeps a watched loan its site pieces, its own size and each record once', async () => {
    await watch('instrument', LOAN);
    const body = await home();
    expect(body.headline.watchedDebtUnder3).toEqual({ debtB: doc.under3[1]?.debtB, names: ['HM construction loan'] });
    expect(body.headline.statusChangesThisWeek).toEqual({ count: 1, names: ['HM site to Broken'] });
    expect(body.watchlistChanges.map((c) => [c.type, c.refId])).toEqual([['status', SITE], ['filing', SITE]]);
    expect(body.watchlistChanges[0]).not.toHaveProperty('watch');
    expect(body.watchlistChanges[0]).not.toHaveProperty('severity');
    expect(body.comingUp).toEqual([{ when: 'Late Oct', what: 'Permit hearing', refUrl: `/sites/${SITE}` }]);
  });

  it('counts a watched site its whole debt once, not its loan again', async () => {
    await watch('site', SITE);
    const body = await home();
    expect(body.headline.watchedDebtUnder3).toEqual({ debtB: doc.under3[0]?.debtB, names: ['HM site'] });
    await watch('instrument', NOTE);
    const more = await home();
    expect(more.headline.fundMarksHigh).toEqual({ count: 1, topName: 'HM 9% notes', gapPts: m(-7.5) });
    expect(more.watched).toEqual({ sites: [SITE], instruments: [LOAN, NOTE] });
  });

  it("applies the caller's price threshold (default 2 points) to price moves", async () => {
    const prices = (b: HomeResponse) => b.watchlistChanges.filter((c) => c.type === 'price').map((c) => c.to.value);
    expect(prices(await home())).toEqual([87.5]);
    const settings = (await app.inject({ method: 'GET', url: '/api/alert-settings' })).json();
    await app.inject({ method: 'PUT', url: '/api/alert-settings', payload: { ...settings, priceThresholdPts: 1 } });
    expect(prices(await home())).toEqual([87.5, 89]);
  });

  it('finds instruments by their issuer and site words', async () => {
    const hits = async (q: string) =>
      ((await app.inject({ method: 'GET', url: `/api/search?q=${encodeURIComponent(q)}` })).json().results as { kind: string; id: string }[])
        .filter((r) => r.kind === 'instrument').map((r) => r.id);
    expect(await hits('Hypothetical Marden notes')).toEqual([NOTE]);
    expect(await hits('HM site loan')).toEqual([LOAN]);
    expect(await hits('HM nowhere')).toEqual([]);
  });
});
