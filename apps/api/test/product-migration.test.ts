// db/migrations/20261021180000_product_access.sql on existing data, in its own throwaway schema: the
// catalog row 'marden', orgs whose plan Marden set (design_partner, paid) keep the V1 app with each
// member's role, trial orgs get nothing (fail closed), and migrate:down removes it all again.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import pg from 'pg';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';

const BASE_URL = process.env.DATABASE_URL ?? 'postgres://marden:marden@localhost:5432/marden?sslmode=disable';
const sql = (name: string): [string, string] => {
  const text = readFileSync(fileURLToPath(new URL(`../../../db/migrations/${name}`, import.meta.url)), 'utf8');
  const [up = '', down = ''] = text.split('-- migrate:down');
  return [up, down];
};

describe('product access migration', () => {
  const schema = `api_mig_${process.pid}_${Date.now().toString(36)}`;
  const client = new pg.Client({ connectionString: BASE_URL });
  const [coreUp] = sql('20261021100000_core.sql');
  const [up, down] = sql('20261021180000_product_access.sql');
  const rows = async (q: string) => (await client.query(q)).rows;

  beforeAll(async () => {
    await client.connect();
    await client.query(`CREATE SCHEMA ${schema}`);
    await client.query(`SET search_path TO ${schema}, public`);
    await client.query(coreUp);
    await client.query(`
      INSERT INTO org (id, name, plan, created_at) VALUES
        ('o_trial', 'Trial', 'trial', '2026-05-01T12:00:00Z'), ('o_dp', 'DP', 'design_partner', '2026-06-15T23:30:00Z'),
        ('o_paid', 'Paid', 'paid', '2026-07-01T12:00:00Z');
      INSERT INTO app_user (id, email) VALUES ('u1', 'u1@x.test'), ('u2', 'u2@x.test'), ('u3', 'u3@x.test');
      INSERT INTO membership (org_id, user_id, role) VALUES
        ('o_trial', 'u1', 'owner'), ('o_dp', 'u1', 'admin'), ('o_dp', 'u2', 'viewer'), ('o_paid', 'u3', 'owner');`);
    await client.query(up);
  });
  afterAll(async () => {
    await client.query(`DROP SCHEMA IF EXISTS ${schema} CASCADE`);
    await client.end();
  });

  it("seeds the catalog with product 'marden'", async () => {
    expect(await rows(`SELECT id, name, active FROM product`)).toEqual([{ id: 'marden', name: 'Marden', active: true }]);
  });

  it('moves explicit plans to org_product and keeps each member their role; trial orgs get nothing', async () => {
    expect(await rows(`SELECT org_id, product_id, plan, seats, status, starts_on::text, ends_on FROM org_product ORDER BY org_id`)).toEqual([
      { org_id: 'o_dp', product_id: 'marden', plan: 'design_partner', seats: null, status: 'active', starts_on: '2026-06-15', ends_on: null },
      { org_id: 'o_paid', product_id: 'marden', plan: 'paid', seats: null, status: 'active', starts_on: '2026-07-01', ends_on: null },
    ]);
    expect(await rows(`SELECT org_id, user_id, role, granted_by FROM member_product_role ORDER BY org_id, user_id`)).toEqual([
      { org_id: 'o_dp', user_id: 'u1', role: 'admin', granted_by: null },
      { org_id: 'o_dp', user_id: 'u2', role: 'viewer', granted_by: null },
      { org_id: 'o_paid', user_id: 'u3', role: 'owner', granted_by: null },
    ]);
    expect((await rows(`SELECT col_description('org'::regclass, 3) AS c`))[0]?.c).toMatch(/^Deprecated/);
  });

  it('enforces the constraints: dates in order, known product, membership required', async () => {
    await expect(client.query(`INSERT INTO org_product (org_id, product_id, plan, starts_on, ends_on) VALUES ('o_trial', 'marden', 'paid', '2026-02-01', '2026-01-01')`)).rejects.toThrow();
    await expect(client.query(`INSERT INTO org_product (org_id, product_id, plan, starts_on) VALUES ('o_trial', 'nope', 'paid', '2026-02-01')`)).rejects.toThrow();
    await expect(client.query(`INSERT INTO member_product_role (org_id, user_id, product_id, role) VALUES ('o_paid', 'u2', 'marden', 'member')`)).rejects.toThrow();
  });

  it('is reversible', async () => {
    await client.query(down);
    expect(await rows(`SELECT to_regclass('${schema}.product') AS a, to_regclass('${schema}.org_product') AS b, to_regclass('${schema}.member_product_role') AS c`))
      .toEqual([{ a: null, b: null, c: null }]);
    expect((await rows(`SELECT col_description('org'::regclass, 3) AS c`))[0]?.c).toBeNull();
    expect((await rows(`SELECT count(*)::int AS n FROM membership`))[0]?.n).toBe(4);
  });
});
