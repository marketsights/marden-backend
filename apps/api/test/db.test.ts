// readModel / readModelRows / latestKey pick the latest snapshot on or before ?asOf.
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { clearReadModels, putReadModel, testDb } from './helpers.js';

describe('db read models', () => {
  let db: Db;
  beforeAll(async () => {
    db = testDb();
    await putReadModel(db, { view: 'db_test_row', key: 'a', asOf: '2026-10-01' }, { id: 'a', v: 1 });
    await putReadModel(db, { view: 'db_test_row', key: 'a', asOf: '2026-10-10' }, { id: 'a', v: 2 });
    await putReadModel(db, { view: 'db_test_row', key: 'b', asOf: '2026-10-05' }, { id: 'b', v: 1 });
  });
  afterAll(async () => {
    await clearReadModels(db, ['db_test_row']);
    await db.close();
  });

  it('reads the latest doc, or the latest on or before asOf', async () => {
    expect(await db.readModel('db_test_row', 'a')).toEqual({ id: 'a', v: 2 });
    expect(await db.readModel('db_test_row', 'a', '2026-10-09')).toEqual({ id: 'a', v: 1 });
    expect(await db.readModel('db_test_row', 'a', '2026-09-30')).toBeNull();
    expect(await db.readModel('db_test_row', 'zzz')).toBeNull();
    expect(await db.readModelEntry('db_test_row', 'a', '2026-10-09')).toEqual({ key: 'a', asOf: '2026-10-01', doc: { id: 'a', v: 1 } });
  });

  it('reads one latest doc per key for row views', async () => {
    expect(await db.readModelRows('db_test_row')).toEqual([{ id: 'a', v: 2 }, { id: 'b', v: 1 }]);
    expect(await db.readModelRows('db_test_row', '2026-10-04')).toEqual([{ id: 'a', v: 1 }]);
    expect(await db.readModelRows('db_test_row', '2026-09-01')).toEqual([]);
  });

  it('finds the highest key as of a date', async () => {
    expect(await db.latestKey('db_test_row')).toBe('b');
    expect(await db.latestKey('db_test_row', '2026-10-04')).toBe('a');
  });

  it('sets app.org_id inside org transactions', async () => {
    const res = await db.withOrg('org_x', (c) => c.query<{ org: string }>(`SELECT current_setting('app.org_id') AS org`));
    expect(res.rows[0]?.org).toBe('org_x');
  });
});
