// Last active on the Team table (spec 13): the API stamps membership.last_active_at on a member's
// requests, at most once per interval, and GET /api/account serves it as team[].lastActiveAt (null
// for a member who has not used Marden since, shown as n/d).
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { ACTIVITY_INTERVAL_MS, createActivityRecorder } from '../src/auth/activity.js';
import type { Db, Queryable } from '../src/db.js';
import { bearer, dropOrgs, makeApp, makeKeys, seedOrg, signToken, testDb, uid, type Keys } from './helpers.js';

describe('activity recorder throttle', () => {
  it('writes once per member per interval in this process', async () => {
    const sql: unknown[][] = [];
    const db = { query: (_text: string, params?: unknown[]) => { sql.push(params ?? []); return Promise.resolve({ rows: [], rowCount: 0 }); } };
    let clock = 1_000_000;
    const recorder = createActivityRecorder(db as unknown as Queryable, { now: () => clock });
    await recorder.touch('org_1', 'user_1');
    await recorder.touch('org_1', 'user_1');
    await recorder.touch('org_1', 'user_2');
    await recorder.touch('org_2', 'user_1');
    clock += ACTIVITY_INTERVAL_MS - 1;
    await recorder.touch('org_1', 'user_1');
    clock += 1;
    await recorder.touch('org_1', 'user_1');
    expect(sql).toEqual([
      ['org_1', 'user_1', ACTIVITY_INTERVAL_MS],
      ['org_1', 'user_2', ACTIVITY_INTERVAL_MS],
      ['org_2', 'user_1', ACTIVITY_INTERVAL_MS],
      ['org_1', 'user_1', ACTIVITY_INTERVAL_MS],
    ]);
  });

  it('retries on the next request when the write failed', async () => {
    let calls = 0;
    const db = { query: () => (++calls === 1 ? Promise.reject(new Error('down')) : Promise.resolve({ rows: [], rowCount: 1 })) };
    const recorder = createActivityRecorder(db as unknown as Queryable, { now: () => 5 });
    await expect(recorder.touch('org_1', 'user_1')).rejects.toThrow('down');
    await recorder.touch('org_1', 'user_1');
    await recorder.touch('org_1', 'user_1');
    expect(calls).toBe(2);
  });
});

describe('last active (Clerk mode, real tables)', () => {
  let db: Db;
  let app: FastifyInstance;
  let keys: Keys;
  const org = uid('org_act');
  const u = { owner: uid('user_act_owner'), analyst: uid('user_act_analyst') };
  const lastActive = async (userId: string) =>
    (await db.query<{ at: Date | null }>(`SELECT last_active_at AS at FROM membership WHERE org_id = $1 AND user_id = $2`, [org, userId])).rows[0]?.at ?? null;

  beforeAll(async () => {
    db = testDb();
    keys = await makeKeys();
    app = await makeApp({ mode: 'clerk', db, keys });
    await seedOrg(db, { id: org, members: [{ id: u.owner, role: 'owner' }, { id: u.analyst, role: 'member' }] });
  });

  afterAll(async () => {
    await app.close();
    await dropOrgs(db, [org], Object.values(u));
    await db.close();
  });

  it("stamps the caller's membership and serves it on /api/account; others stay null", async () => {
    expect(await lastActive(u.owner)).toBeNull();
    const t = await signToken(keys, { userId: u.owner, orgId: org });
    const res = await app.inject({ method: 'GET', url: '/api/account', headers: bearer(t) });
    expect(res.statusCode).toBe(200);
    const stamped = await lastActive(u.owner);
    expect(stamped).not.toBeNull();
    expect(Math.abs(Date.now() - stamped!.getTime())).toBeLessThan(60_000);
    const team = res.json().team as { userId: string; lastActiveAt: string | null }[];
    expect(team.find((m) => m.userId === u.owner)?.lastActiveAt).toBe(stamped!.toISOString());
    expect(team.find((m) => m.userId === u.analyst)?.lastActiveAt).toBeNull();
    expect(await lastActive(u.analyst)).toBeNull();
  });

  it('a refused request still counts as activity', async () => {
    // A member may not change roles (403), but they did use Marden.
    const t = await signToken(keys, { userId: u.analyst, orgId: org });
    const res = await app.inject({ method: 'PATCH', url: `/api/account/members/${u.owner}`, headers: bearer(t), payload: { role: 'viewer' } });
    expect(res.statusCode).toBe(403);
    expect(await lastActive(u.analyst)).not.toBeNull();
  });

  it('moves a stamp only when it is older than the interval (several processes write once per interval)', async () => {
    await db.query(`UPDATE membership SET last_active_at = now() - interval '1 minute' WHERE org_id = $1 AND user_id = $2`, [org, u.analyst]);
    const recent = await lastActive(u.analyst);
    await createActivityRecorder(db).touch(org, u.analyst);
    expect(await lastActive(u.analyst)).toEqual(recent);
    await db.query(`UPDATE membership SET last_active_at = now() - interval '2 hours' WHERE org_id = $1 AND user_id = $2`, [org, u.analyst]);
    await createActivityRecorder(db).touch(org, u.analyst);
    expect(Math.abs(Date.now() - (await lastActive(u.analyst))!.getTime())).toBeLessThan(60_000);
  });
});
