// Data centers: a campus that finances several sites (Galaxy Helios I and II; Polaris Forge 1 and its building 4)
// carries them all in `financedSites`. The KPI strip counts the campus once and sums every site's debt, not only
// the primary `financed` link's (a site listed on two campuses once), and the watchlist filter finds the campus through any of its sites.
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { clearReadModels, makeApp, putReadModel, sourced, testDb } from './helpers.js';

const evidence = { date: '2026-10-01', text: 'Record', tag: 'F', sourceName: 'County', sourceUrl: 'https://example.test/r' };
const link = (slug: string, chainStatus: string, debt: number | null) =>
  ({ siteSlug: slug, siteName: slug, chainStatus, debtB: sourced(debt, debt === null ? 'R' : 'F') });

function dcRow(id: string, state: string, sites: ReturnType<typeof link>[]) {
  return {
    id, name: `Campus ${id}`, aliases: [], county: 'Test', state, lat: 32.4, lon: -99.7,
    developer: sourced('Dev', 'F'), tenant: sourced(null, 'R'), status: sourced('Under construction', 'F'),
    facilityMw: sourced(800, 'F'), itMw: sourced(null, 'F'), buildings: { built: 0, total: 1 },
    powerSource: sourced('Grid', 'F'), utility: sourced(null, 'F'), iso: sourced('ERCOT', 'F'), plantId: null,
    onlineYear: sourced(null, 'M'), lastChange: evidence, firstSeen: '2025-02-01',
    financed: sites[0] ?? null, financedSites: sites,
  };
}

const ROWS = [
  // two financed sites with debt and one without a disclosed amount yet: most debt first
  dcRow('dcf-two', 'TX', [link('dcf-big', 'Broken', 1.59), link('dcf-small', 'Strained', 1.27), link('dcf-nd', 'Holding', null)]),
  dcRow('dcf-one', 'NM', [link('dcf-solo', 'Holding', 9.4)]),
  dcRow('dcf-none', 'TX', []),
  // a site whose debt secures two campuses (Core Scientific's notes on Denton and Dalton): its debt counts once
  dcRow('dcf-shared', 'NM', [link('dcf-solo', 'Holding', 9.4)]),
];

describe('data centers with several financed sites', () => {
  let db: Db;
  let app: FastifyInstance;

  beforeAll(async () => {
    db = testDb();
    app = await makeApp({ mode: 'dev', db });
    await clearReadModels(db, ['dc_row']);
    for (const r of ROWS) {
      await db.query(`INSERT INTO campus (id, name, state) VALUES ($1, $2, $3)`, [r.id, r.name, r.state]);
      await putReadModel(db, { view: 'dc_row', key: r.id, asOf: '2026-10-15' }, r);
    }
  });
  afterAll(async () => {
    await db.query(`DELETE FROM watchlist_item WHERE org_id = 'dev' AND ref_id LIKE 'dcf-%'`);
    await clearReadModels(db, ['dc_row']);
    await db.query(`DELETE FROM campus WHERE id = ANY($1)`, [ROWS.map((r) => r.id)]);
    await app.close();
    await db.close();
  });

  const json = async (url: string) => (await app.inject({ method: 'GET', url })).json();

  it('counts each financed campus once and sums the debt of all its sites', async () => {
    const all = await json('/api/data-centers/summary');
    expect(all.financed.count).toBe(3);
    expect(all.financed.debtB).toBeCloseTo(1.59 + 1.27 + 9.4, 6);  // dcf-solo once, though two campuses list it
    const nm = await json('/api/data-centers/summary?state=NM');
    expect(nm.financed).toEqual({ count: 2, debtB: 9.4 });
    const tx = await json('/api/data-centers/summary?state=TX');
    expect(tx.financed.count).toBe(1);
    expect(tx.financed.debtB).toBeCloseTo(2.86, 6);  // the primary link alone would show 1.59
  });

  it('serves the list on the row as built', async () => {
    const row = await json('/api/data-centers/dcf-two/row');
    expect(row.financedSites.map((s: { siteSlug: string }) => s.siteSlug)).toEqual(['dcf-big', 'dcf-small', 'dcf-nd']);
    expect(row.financed).toEqual(row.financedSites[0]);
  });

  it('finds a campus on the watchlist through any of its financed sites', async () => {
    const ids = async () => ((await json('/api/data-centers?watchlist=true')).rows as { id: string }[]).map((r) => r.id);
    expect(await ids()).toEqual([]);
    await db.query(`INSERT INTO watchlist_item (org_id, kind, ref_id) VALUES ('dev', 'site', 'dcf-small')`);
    expect(await ids()).toEqual(['dcf-two']);
    await db.query(`INSERT INTO watchlist_item (org_id, kind, ref_id) VALUES ('dev', 'site', 'dcf-solo')`);
    expect((await ids()).sort()).toEqual(['dcf-one', 'dcf-shared', 'dcf-two']);
  });
});
