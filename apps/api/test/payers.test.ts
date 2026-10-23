// Payers index (spec 24) and Payer page (spec 08): ?kind=&risk= filters on GET /api/payers, the
// index export with the same filters and the index's columns, and the payer's sites export sourced
// per site from the sites document (n/d kept as n/d, not 0).
import ExcelJS from 'exceljs';
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { clearReadModels, makeApp, putReadModel, sourced, testDb } from './helpers.js';

const AS_OF = '2026-10-15';
const m = (value: number | string | null) => ({ ...sourced(value, 'M'), methodVersion: 'payer capacity v0' });

type Exposed = [id: string, amountB: number | null, holders: number];
function capacity([slug, name, kind]: [string, string, string], risk: string | null, exposed: Exposed[]) {
  return {
    slug, name, kind, spreadBp: m(null), leaseAdjLeverageToday: m(risk ? 3.1 : null), leaseAdjLeveragePeak: m(risk ? 4.6 : null),
    triggerHeadroomTurns: m(null), breachYear: m(null), fundingGap3yB: m(null), aiRevenueCoverage: m(null),
    topCounterparty: risk ? { name: 'OpenAI', share: m(0.5) } : null,
    downgradeRisk: risk === null ? { value: null, tag: 'M', asOf: AS_OF } : m(risk),
    ratings: risk ? [{ agency: 'S&P', rating: sourced('BBB-', 'F'), outlook: sourced('Negative', 'F'), lastAction: sourced('Cut', 'F'), triggers: [] }] : [],
    years: [],
    exposed: exposed.map(([id, amount, holders]) => ({
      instrumentId: id, name: `Instrument ${id}`, link: 'Lease', amountB: sourced(amount, 'F'),
      ratingTrigger: sourced(null, 'F'), onTrip: sourced(null, 'F'), holdersCount: holders,
    })),
  };
}

const PAYERS = {
  asOf: AS_OF,
  rows: [
    capacity(['pay-oracle', 'Oracle', 'Cloud'], 'High', [['I1', 18, 10], ['I2', null, 5], ['I3', 9.4, 0]]),
    {
      ...capacity(['pay-openai', 'OpenAI', 'AI lab'], null, [['I1', 18, 10], ['I4', 2.2, 3]]),
      listed: false,
      scale: [{ key: 'revenue', basis: 'annualized, end of Sep 2026', amountB: sourced(50, 'R') }],
      exposedElsewhere: [{ slug: 'pay-oracle', name: 'Oracle', instruments: 2, amountB: sourced(18, 'F') }],
    },
    capacity(['pay-nebius', 'Nebius', 'Neocloud'], 'Watch', []),
  ],
};

const site = (slug: string, debt: number | null, gw: string | null, cushion: number | null) => ({
  slug, name: `Site ${slug}`, county: 'Test', state: 'TX', endUser: 'OpenAI', tenant: 'Oracle', developer: 'Dev',
  gw: sourced(gw, 'R'), chain: [], status: 'Strained', cushionMonths: cushion === null ? null : m(cushion),
  rentStartP50: '2027-06', debtB: sourced(debt, 'F'), leadLenders: 'Bank',
});

const PAYER = {
  slug: 'pay-oracle', name: 'Oracle', role: 'tenant',
  kpis: {
    siteDebtB: sourced(18, 'M'), gwLeased: sourced('2.6 GW', 'R'), rating: sourced('BBB-', 'R'),
    cds5y: m(null), notesB: sourced(119.5, 'F'), noteHolders: sourced(361, 'F'),
  },
  sites: [
    { slug: 'pay-a', name: 'Site pay-a', gw: '1.2 GW', rentStartP50: '2027-06', outsideDate: sourced('2027-12', 'R'), cushionMonths: 6, debtB: 18, status: 'Strained' },
    // The payer document writes 0 / 'n/d' where nothing is disclosed; the site's own row keeps n/d.
    { slug: 'pay-b', name: 'Site pay-b', gw: 'n/d', rentStartP50: null, outsideDate: sourced(null, 'R'), cushionMonths: null, debtB: 0, status: 'Strained' },
  ],
  paidBy: [], credit: {},
};

describe('payers', () => {
  let db: Db;
  let app: FastifyInstance;

  beforeAll(async () => {
    db = testDb();
    app = await makeApp({ mode: 'dev', db });
    await putReadModel(db, { view: 'payers', key: 'all', asOf: AS_OF }, PAYERS);
    await putReadModel(db, { view: 'payer', key: 'pay-oracle', asOf: AS_OF }, PAYER);
  });
  afterAll(async () => {
    await clearReadModels(db, ['payers', 'payer', 'payer_capacity', 'sites']);
    await app.close();
    await db.close();
  });

  const get = (url: string) => app.inject({ method: 'GET', url });
  const slugs = async (qs: string) => ((await get(`/api/payers${qs}`)).json().rows as { slug: string }[]).map((r) => r.slug);

  it('serves every payer without filters and filters by kind and risk band', async () => {
    expect(await slugs('')).toEqual(['pay-oracle', 'pay-openai', 'pay-nebius']);
    expect((await get('/api/payers')).json().asOf).toBe(AS_OF);
    expect(await slugs('?kind=cloud,Neocloud')).toEqual(['pay-oracle', 'pay-nebius']);
    expect(await slugs('?risk=High')).toEqual(['pay-oracle']);
    expect(await slugs('?risk=n/d')).toEqual(['pay-openai']);
    expect(await slugs('?kind=AI%20lab&risk=High')).toEqual([]);
    const bad = await get('/api/payers?risk=Severe');
    expect(bad.statusCode).toBe(400);
  });

  async function sheet(url: string) {
    const res = await get(url);
    expect(res.statusCode).toBe(200);
    const wb = new ExcelJS.Workbook();
    await wb.xlsx.load(res.rawPayload as unknown as ArrayBuffer);
    const data = wb.getWorksheet('Data')!;
    const header = (data.getRow(1).values as unknown[]).slice(1) as string[];
    const rows = Array.from({ length: data.rowCount - 1 }, (_, i) => {
      const values = (data.getRow(i + 2).values as unknown[]).slice(1);
      return Object.fromEntries(header.map((h, j) => [h, values[j]]));
    });
    return { rows, sources: wb.getWorksheet('Sources')!.getSheetValues().slice(2) as unknown[][] };
  }

  it('exports the filtered index with its columns and the exposed totals', async () => {
    const { rows, sources } = await sheet('/api/export/payers.xlsx?kind=Cloud,Neocloud');
    expect(rows.map((r) => r.slug)).toEqual(['pay-oracle', 'pay-nebius']);
    expect(rows[0]).toMatchObject({
      name: 'Oracle', kind: 'Cloud', spRating: 'BBB-', spOutlook: 'Negative', downgradeRisk: 'High', leaseAdjLeverageToday: 3.1,
      topCounterparty: 'OpenAI', topCounterpartyShare: 0.5, instrumentsExposed: 3, exposedB: 27.4, holderPositions: 15,
    });
    expect(rows[1]).toMatchObject({ instrumentsExposed: 0, holderPositions: 0, fitchRating: '' }); // no Fitch rating: empty cell
    expect(sources.some((l) => l[1] === 'pay-oracle' && l[2] === 'downgradeRisk' && l[4] === 'M' && l[8] === 'payer capacity v0')).toBe(true);
  });

  it('exports backstops apart from the payer’s own exposed debt, and its reported scale', async () => {
    const page = { ...PAYERS, rows: PAYERS.rows.map((r) => (r.slug !== 'pay-openai' ? r : { ...r, exposed: r.exposed.map((e) => (e.instrumentId === 'I1' ? { ...e, link: 'Guarantee' } : e)) })) };
    await putReadModel(db, { view: 'payers', key: 'all', asOf: AS_OF }, page);
    const { rows, sources } = await sheet('/api/export/payers.xlsx?kind=AI%20lab');
    expect(rows[0]).toMatchObject({
      slug: 'pay-openai', listed: false, instrumentsExposed: 1, exposedB: 2.2, holderPositions: 3, instrumentsBackstopped: 1, backstoppedB: 18,
      revenueB: 50, revenueBasis: 'annualized, end of Sep 2026', countedElsewhere: 'Oracle: 2',
    });
    expect(sources.some((l) => l[1] === 'pay-openai' && l[2] === 'revenueB' && l[4] === 'R')).toBe(true);
    await putReadModel(db, { view: 'payers', key: 'all', asOf: AS_OF }, PAYERS);
  });

  it('exports one payer’s sites, sourced from the sites document when it is built', async () => {
    const before = await sheet('/api/export/payer.xlsx?slug=pay-oracle');
    expect(before.rows.map((r) => [r.slug, r.gw, r.debtB])).toEqual([['pay-a', '1.2 GW', 18], ['pay-b', 'n/d', 'n/d']]);
    await putReadModel(db, { view: 'sites', key: 'all', asOf: AS_OF }, [site('pay-a', 18, '1.2 GW', 6), site('pay-b', null, null, null)]);
    const after = await sheet('/api/export/payer.xlsx?slug=pay-oracle');
    expect(after.rows.map((r) => [r.slug, r.tenant, r.gw, r.debtB, r.cushionMonths])).toEqual([
      ['pay-a', 'Oracle', '1.2 GW', 18, 6],
      ['pay-b', 'Oracle', 'n/d', 'n/d', ''], // no cushion (no lease date): empty cell
    ]);
    expect(after.sources.some((l) => l[1] === 'pay-a' && l[2] === 'debtB' && l[4] === 'F')).toBe(true);
  });

  it('exports the instruments that depend on one payer', async () => {
    await putReadModel(db, { view: 'payer_capacity', key: 'pay-oracle', asOf: AS_OF }, PAYERS.rows[0]);
    const { rows } = await sheet('/api/export/payer-exposed.xlsx?slug=pay-oracle');
    expect(rows.map((r) => [r.instrumentId, r.amountB, r.holdersCount])).toEqual([['I1', 18, 10], ['I2', 'n/d', 5], ['I3', 9.4, 0]]);
    expect((await get('/api/export/payer-exposed.xlsx')).statusCode).toBe(400);
  });

  it('asks for the slug and answers 503 for a payer not built yet', async () => {
    expect((await get('/api/export/payer.xlsx')).statusCode).toBe(400);
    expect((await get('/api/export/payer.xlsx?slug=pay-missing')).json()).toEqual({ error: 'not_ready', view: 'payer', key: 'pay-missing' });
  });
});
