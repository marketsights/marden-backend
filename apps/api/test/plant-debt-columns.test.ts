// On-site power table columns from spec 20 (Owner, Financed by, Plant debt, Power buyer): each plant row
// carries the roll-up of its plant_debt document, in the page, the one-row refresh and the Excel export.
// Plant debt is n/d (never zero) when no record states an amount.
import ExcelJS from 'exceljs';
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { PlantDebt } from '../src/contracts/types.js';
import type { Db } from '../src/db.js';
import { PLANT_DEBT_SUM_METHOD, plantDebtColumns } from '../src/lib/plant-debt-row.js';
import { clearReadModels, makeApp, putReadModel, sourced, testDb } from './helpers.js';

const evidence = { date: '2026-10-01', text: 'Record', tag: 'F' as const, sourceName: 'County', sourceUrl: 'https://example.test/r' };

function plantRow(id: string, name: string) {
  return {
    id, name, county: 'Taylor', state: 'TX', lat: 32.5, lon: -99.8, serves: null, technology: 'Simple-cycle turbine',
    units: { count: 10, byModel: [] }, permittedMw: sourced(360.5, 'F'), status: sourced('Permitted', 'F'),
    airPermit: null, operator: sourced('Crusoe', 'R'), fuelSupply: sourced(null, 'F'), gridMode: 'Unknown', financed: null,
    lastChange: evidence,
  };
}

const FILED: PlantDebt = {
  owner: sourced('Abilene DC 1, LLC', 'F'),
  liens: [{ securedParty: 'MUFG Bank', debtor: 'Abilene DC 1, LLC', state: 'DE', fileNumber: '2025-1', filed: '2025-02-01', collateral: 'Turbines', url: 'https://example.test/ucc' }],
  mortgages: [{ lender: 'JPMorgan', amount: sourced(250_000_000, 'F'), recorded: '2025-03-01', url: 'https://example.test/deed' }],
  financing: [
    { lender: 'mufg bank', type: 'Loan', amount: sourced(500_000_000, 'R'), evidence },
    { lender: 'GE Vernova', type: 'Vendor financing', amount: sourced(null, 'R'), evidence },
  ],
  powerBuyer: { name: 'Oracle', term: sourced('15 years', 'F'), edgeId: 'oracle--crusoe--power-purchase' },
  equipmentOrders: [],
};

describe('plant debt columns (spec 20)', () => {
  let db: Db;
  let app: FastifyInstance;

  beforeAll(async () => {
    db = testDb();
    app = await makeApp({ mode: 'dev', db });
    await db.query(`INSERT INTO power_plant (id, name, state) VALUES ('pd-1', 'Debt plant', 'TX'), ('pd-2', 'Bare plant', 'TX')`);
    await putReadModel(db, { view: 'plant_row', key: 'pd-1', asOf: '2026-10-15' }, plantRow('pd-1', 'Debt plant'));
    await putReadModel(db, { view: 'plant_row', key: 'pd-2', asOf: '2026-10-15' }, plantRow('pd-2', 'Bare plant'));
    await putReadModel(db, { view: 'plant_debt', key: 'pd-1', asOf: '2026-10-15' }, FILED);
    // pd-2: the debt document as built when nothing is filed: an empty list is n/d, not zero.
    await putReadModel(db, { view: 'plant_debt', key: 'pd-2', asOf: '2026-10-15' }, {
      owner: sourced(null, 'F'), liens: [], mortgages: [], financing: [], powerBuyer: null, equipmentOrders: [],
    });
  });
  afterAll(async () => {
    await clearReadModels(db, ['plant_row', 'plant_debt']);
    await db.query(`DELETE FROM power_plant WHERE id IN ('pd-1', 'pd-2')`);
    await app.close();
    await db.close();
  });

  const json = async (url: string) => (await app.inject({ method: 'GET', url })).json();

  it('rolls the debt document up into four columns', () => {
    const cols = plantDebtColumns(FILED, '2026-10-15');
    expect(cols.owner.value).toBe('Abilene DC 1, LLC');
    expect(cols.financedBy).toEqual(['MUFG Bank', 'GE Vernova', 'JPMorgan']); // each once, case-insensitive
    expect(cols.plantDebt).toMatchObject({ value: 750_000_000, tag: 'M', methodVersion: PLANT_DEBT_SUM_METHOD });
    expect(cols.plantDebt.sourceName).toBe('Sum of 2 stated amounts; 1 more not stated');
    expect(cols.powerBuyer).toBe('Oracle');
    const none = plantDebtColumns(null, '2026-10-15');
    expect(none).toMatchObject({ owner: { value: null }, financedBy: [], plantDebt: { value: null, tag: 'M' }, powerBuyer: null });
  });

  it('adds the columns to every page row and to the one-row refresh', async () => {
    const page = await json('/api/power-plants?sort=name');
    const [bare, filed] = page.rows;
    expect(filed).toMatchObject({ id: 'pd-1', owner: { value: 'Abilene DC 1, LLC' }, financedBy: ['MUFG Bank', 'GE Vernova', 'JPMorgan'] });
    expect(filed.plantDebt.value).toBe(750_000_000);
    expect(filed.powerBuyer).toBe('Oracle');
    expect(bare).toMatchObject({ id: 'pd-2', owner: { value: null }, financedBy: [], plantDebt: { value: null }, powerBuyer: null });
    expect(await json('/api/power-plants/pd-1/row')).toEqual(filed);
    expect(await json('/api/power-plants/pd-2/row')).toEqual(bare);
  });

  it('keeps the columns n/d when the debt document is not built yet', async () => {
    await clearReadModels(db, ['plant_debt']);
    try {
      const row = await json('/api/power-plants/pd-1/row');
      expect(row).toMatchObject({ owner: { value: null }, financedBy: [], plantDebt: { value: null }, powerBuyer: null });
    } finally {
      await putReadModel(db, { view: 'plant_debt', key: 'pd-1', asOf: '2026-10-15' }, FILED);
    }
  });

  it('exports the columns with the rows', async () => {
    const res = await app.inject({ method: 'GET', url: '/api/export/power-plants.xlsx?sort=name' });
    expect(res.statusCode).toBe(200);
    const wb = new ExcelJS.Workbook();
    await wb.xlsx.load(res.rawPayload as unknown as ArrayBuffer);
    const data = wb.getWorksheet('Data')!;
    const header = (data.getRow(1).values as unknown[]).slice(1);
    expect(header).toEqual(expect.arrayContaining(['owner', 'financedBy', 'plantDebt', 'powerBuyer']));
    const cell = (row: number, key: string) => data.getRow(row).getCell(header.indexOf(key) + 1).value;
    expect([cell(2, 'plantDebt'), cell(3, 'plantDebt')]).toEqual(['n/d', 750_000_000]);
    expect(cell(3, 'financedBy')).toBe('MUFG Bank, GE Vernova, JPMorgan');
    expect([cell(3, 'owner'), cell(3, 'powerBuyer')]).toEqual(['Abilene DC 1, LLC', 'Oracle']);
  });
});
