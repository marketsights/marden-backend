// Last active on the Team table (spec 13): the API stamps membership.last_active_at on a member's
// requests, at most once per interval, and GET /api/account serves it as team[].lastActiveAt (null
// for a member who has not used Marden since, shown as n/d).
import type { FastifyInstance } from 'fastify';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { ACTIVITY_INTERVAL_MS, createActivityRecorder, SESSION_IDLE_MS } from '../src/auth/activity.js';
import type { Db, Queryable } from '../src/db.js';
import { bearer, clearReadModels, dropOrgs, makeApp, makeKeys, putReadModel, seedOrg, signToken, testDb, uid, type Keys } from './helpers.js';

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
      ['org_1', 'user_1', ACTIVITY_INTERVAL_MS, SESSION_IDLE_MS],
      ['org_1', 'user_2', ACTIVITY_INTERVAL_MS, SESSION_IDLE_MS],
      ['org_2', 'user_1', ACTIVITY_INTERVAL_MS, SESSION_IDLE_MS],
      ['org_1', 'user_1', ACTIVITY_INTERVAL_MS, SESSION_IDLE_MS],
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

describe('last visit (Clerk mode, real tables)', () => {
  let db: Db;
  let app: FastifyInstance;
  let keys: Keys;
  const org = uid('org_visit');
  const user = uid('user_visit');
  const DAY = '2026-10-21';
  const EMPTY_HOME = { asOf: DAY, week: { start: '2026-10-15', end: DAY }, under3: [], cushionKnown: [], statusChanges: [], fundMarksHigh: [], changes: [], comingUp: [] };
  const sessions = async () =>
    (await db.query<{ started: Date | null; last: Date | null; active: Date | null }>(
      `SELECT session_started_at AS started, last_visit_at AS last, last_active_at AS active FROM membership WHERE org_id = $1 AND user_id = $2`,
      [org, user],
    )).rows[0]!;
  const set = (sql: string, ...params: unknown[]) => db.query(`UPDATE membership SET ${sql} WHERE org_id = $1 AND user_id = $2`, [org, user, ...params]);
  const home = async () => {
    const t = await signToken(keys, { userId: user, orgId: org });
    const res = await app.inject({ method: 'GET', url: '/api/home', headers: bearer(t) });
    expect(res.statusCode).toBe(200);
    return res.json() as { lastVisit: string | null };
  };
  const recent = (d: Date | null) => d !== null && Math.abs(Date.now() - d.getTime()) < 60_000;

  beforeAll(async () => {
    db = testDb();
    keys = await makeKeys();
    await seedOrg(db, { id: org, members: [{ id: user, role: 'owner' }] });
    await putReadModel(db, { view: 'home', key: 'all', asOf: DAY }, EMPTY_HOME);
  });
  // A fresh API process per test: each keeps its own in-process throttle, as a member returning after
  // 30 minutes finds (the throttle remembers 5 minutes), so only the database stamps decide.
  beforeEach(async () => {
    app = await makeApp({ mode: 'clerk', db, keys });
  });
  afterEach(async () => {
    await app.close();
  });

  afterAll(async () => {
    await clearReadModels(db, ['home']);
    await dropOrgs(db, [org], [user]);
    await db.close();
  });

  it('starts a session on the first request; with no earlier session seen, lastVisit is null (never guessed)', async () => {
    expect(await sessions()).toEqual({ started: null, last: null, active: null });
    expect((await home()).lastVisit).toBeNull();
    const s = await sessions();
    expect(recent(s.started) && recent(s.active)).toBe(true);
    expect(s.last).toBeNull();
  });

  it('keeps the session while the member is active within 30 minutes', async () => {
    const start = new Date('2026-10-21T13:05:00Z');
    await set(`session_started_at = $3, last_visit_at = NULL, last_active_at = now() - interval '10 minutes'`, start);
    expect((await home()).lastVisit).toBeNull();
    const s = await sessions();
    expect(s.started).toEqual(start); // same session: its start stays
    expect(recent(s.active)).toBe(true);
  });

  it('after 30 minutes idle the next request starts a new session and lastVisit is the previous session start', async () => {
    const previous = new Date('2026-10-20T14:02:00Z');
    await set(`session_started_at = $3, last_visit_at = '2026-10-19T09:00:00Z', last_active_at = now() - interval '45 minutes'`, previous);
    expect((await home()).lastVisit).toBe(previous.toISOString());
    const s = await sessions();
    expect(s.last).toEqual(previous);
    expect(recent(s.started)).toBe(true);
    // Later requests in the new session keep the same last visit.
    await set(`last_active_at = now() - interval '6 minutes'`);
    expect((await home()).lastVisit).toBe(previous.toISOString());
  });

  it('measures the idle time against the stamp: the session boundary is SESSION_IDLE_MS', async () => {
    expect(SESSION_IDLE_MS).toBe(30 * 60_000);
    const start = new Date('2026-10-21T15:00:00Z');
    await set(`session_started_at = $3, last_visit_at = NULL, last_active_at = now() - interval '29 minutes'`, start);
    await createActivityRecorder(db).touch(org, user);
    expect((await sessions()).started).toEqual(start);
    await set(`last_active_at = now() - interval '31 minutes'`);
    await createActivityRecorder(db).touch(org, user);
    const s = await sessions();
    expect(s.last).toEqual(start);
    expect(recent(s.started)).toBe(true);
  });
});
