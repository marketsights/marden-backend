// db/migrations/20261021234100_marden_org_roles.sql on existing data, in its own throwaway schema:
// every membership keeps its role (marked as mirrored from Clerk), invitations sent before the change
// are carried over from audit_log with their Marden role, and migrate:down removes it all again.
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

describe('Marden-owned org roles migration', () => {
  const schema = `api_roles_mig_${process.pid}_${Date.now().toString(36)}`;
  const client = new pg.Client({ connectionString: BASE_URL });
  const [up, down] = sql('20261021234100_marden_org_roles.sql');
  const rows = async (q: string) => (await client.query(q)).rows;

  beforeAll(async () => {
    await client.connect();
    await client.query(`CREATE SCHEMA ${schema}`);
    await client.query(`SET search_path TO ${schema}, public`);
    await client.query(`SET TIME ZONE 'UTC'`);
    await client.query(sql('20261021100000_core.sql')[0]);
    await client.query(sql('20261021180000_product_access.sql')[0]);
    await client.query(`
      INSERT INTO org (id, name, plan) VALUES ('o1', 'Fund One', 'paid'), ('o2', 'Fund Two', 'trial');
      INSERT INTO app_user (id, email) VALUES ('u1', 'u1@x.test'), ('u2', 'Joined@X.test'), ('u3', 'u3@x.test');
      INSERT INTO membership (org_id, user_id, role) VALUES
        ('o1', 'u1', 'owner'), ('o1', 'u2', 'viewer'), ('o2', 'u3', 'admin');
      INSERT INTO audit_log (at, org_id, user_id, action, target, meta) VALUES
        ('2026-10-20T10:00:00Z', 'o1', 'u1', 'member.invite', 'invitation:orginv_joined', '{"email": "joined@x.test", "role": "member"}'),
        ('2026-10-21T09:00:00Z', 'o1', 'u1', 'member.invite', 'invitation:orginv_open', '{"email": "New@Fund.test", "role": "admin"}'),
        ('2026-10-21T09:30:00Z', 'o2', 'u_gone', 'member.invite', 'invitation:orginv_o2', '{"email": "pm@fund.test", "role": "viewer"}'),
        ('2026-10-21T10:00:00Z', 'o_deleted', 'u1', 'member.invite', 'invitation:orginv_dead', '{"email": "a@fund.test", "role": "member"}'),
        ('2026-10-21T10:00:00Z', 'o1', 'u1', 'member.invite', 'invitation:orginv_bad', '{"email": "b@fund.test", "role": "superuser"}'),
        ('2026-10-21T10:00:00Z', 'o1', 'u1', 'member.role', 'user:u2', '{"from": "member", "to": "viewer"}');`);
    await client.query(up);
  });
  afterAll(async () => {
    await client.query(`DROP SCHEMA IF EXISTS ${schema} CASCADE`);
    await client.end();
  });

  it('keeps every current role and records that it came from Clerk', async () => {
    expect(await rows(`SELECT org_id, user_id, role, role_source FROM membership ORDER BY org_id, user_id`)).toEqual([
      { org_id: 'o1', user_id: 'u1', role: 'owner', role_source: 'clerk' },
      { org_id: 'o1', user_id: 'u2', role: 'viewer', role_source: 'clerk' },
      { org_id: 'o2', user_id: 'u3', role: 'admin', role_source: 'clerk' },
    ]);
    await client.query(`INSERT INTO app_user (id, email) VALUES ('u4', 'u4@x.test'); INSERT INTO membership (org_id, user_id, role) VALUES ('o2', 'u4', 'member')`);
    expect(await rows(`SELECT role_source FROM membership WHERE user_id = 'u4'`)).toEqual([{ role_source: 'system' }]);
    await expect(client.query(`UPDATE membership SET role_source = 'clerk_role' WHERE user_id = 'u4'`)).rejects.toThrow();
    expect((await rows(`SELECT col_description('membership'::regclass, 3) AS c`))[0]?.c).toMatch(/^Marden org role/);
  });

  it('carries invitations sent before the change over with their Marden role', async () => {
    expect(await rows(`SELECT id, org_id, email, role, invited_by, status, created_at::text, expires_at::text,
                              accepted_at IS NOT NULL AS accepted FROM org_invitation ORDER BY id`)).toEqual([
      { id: 'orginv_joined', org_id: 'o1', email: 'joined@x.test', role: 'member', invited_by: 'u1', status: 'accepted',
        created_at: '2026-10-20 10:00:00+00', expires_at: '2026-11-19 10:00:00+00', accepted: true },
      { id: 'orginv_o2', org_id: 'o2', email: 'pm@fund.test', role: 'viewer', invited_by: null, status: 'pending',
        created_at: '2026-10-21 09:30:00+00', expires_at: '2026-11-20 09:30:00+00', accepted: false },
      { id: 'orginv_open', org_id: 'o1', email: 'new@fund.test', role: 'admin', invited_by: 'u1', status: 'pending',
        created_at: '2026-10-21 09:00:00+00', expires_at: '2026-11-20 09:00:00+00', accepted: false },
    ]);
  });

  it('enforces the invitation constraints', async () => {
    const insert = (values: string) => client.query(`INSERT INTO org_invitation (id, org_id, email, role, status, accepted_at) VALUES ${values}`);
    await expect(insert(`('i1', 'o1', 'Upper@x.test', 'member', 'pending', NULL)`)).rejects.toThrow();
    await expect(insert(`('i2', 'o1', 'a@x.test', 'staff', 'pending', NULL)`)).rejects.toThrow();
    await expect(insert(`('i3', 'o_none', 'a@x.test', 'member', 'pending', NULL)`)).rejects.toThrow();
    await expect(insert(`('i4', 'o1', 'a@x.test', 'member', 'accepted', NULL)`)).rejects.toThrow();
    await client.query(`DELETE FROM org WHERE id = 'o2'`);
    expect(await rows(`SELECT id FROM org_invitation ORDER BY id`)).toEqual([{ id: 'orginv_joined' }, { id: 'orginv_open' }]);
  });

  it('is reversible and leaves the roles alone', async () => {
    await client.query(down);
    expect(await rows(`SELECT to_regclass('${schema}.org_invitation') AS t`)).toEqual([{ t: null }]);
    expect(await rows(`SELECT count(*)::int AS n FROM information_schema.columns WHERE table_schema = '${schema}' AND table_name = 'membership' AND column_name = 'role_source'`))
      .toEqual([{ n: 0 }]);
    expect(await rows(`SELECT user_id, role FROM membership ORDER BY user_id`)).toEqual([{ user_id: 'u1', role: 'owner' }, { user_id: 'u2', role: 'viewer' }]);
    await client.query(up); // and applies again cleanly
  });
});
