// Spec 23 behaviour with Clerk-style session tokens signed by locally generated keys:
// 401 signed out or bad token, 403 wrong role / plan / org, 404 for another org's data.
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { ClerkAdmin } from '../src/auth/clerk-admin.js';
import type { Db } from '../src/db.js';
import { bearer, dropOrgs, makeApp, makeKeys, seedOrg, signToken, testDb, uid, type Keys } from './helpers.js';

describe('auth (Clerk mode)', () => {
  let db: Db;
  let app: FastifyInstance;
  let keys: Keys;
  const orgA = uid('org_a');
  const orgB = uid('org_b');
  const trialOrg = uid('org_trial');
  const u = { owner: uid('user_owner'), admin: uid('user_admin'), member: uid('user_member'), viewer: uid('user_viewer'), other: uid('user_b'), trial: uid('user_trial'), outsider: uid('user_out') };
  const clerkCalls: string[] = [];
  const clerkAdmin: ClerkAdmin = {
    invite: (org, email) => { clerkCalls.push(`invite ${org} ${email}`); return Promise.resolve({ id: uid('orginv'), expiresAt: null }); },
    removeMember: (org, user) => { clerkCalls.push(`remove ${org} ${user}`); return Promise.resolve(); },
    mfaEnabled: () => Promise.resolve(true),
  };
  const token = (userId: string, orgId: string | null = orgA, extra: Parameters<typeof signToken>[1] = { userId }) =>
    signToken(keys, { ...extra, userId, orgId });

  beforeAll(async () => {
    db = testDb();
    keys = await makeKeys();
    app = await makeApp({ mode: 'clerk', db, keys, clerkAdmin });
    await seedOrg(db, { id: orgA, members: [
      { id: u.owner, role: 'owner' }, { id: u.admin, role: 'admin' }, { id: u.member, role: 'member' }, { id: u.viewer, role: 'viewer' },
    ] });
    await seedOrg(db, { id: orgB, members: [{ id: u.other, role: 'owner' }] });
    await seedOrg(db, { id: trialOrg, plan: 'trial', members: [{ id: u.trial, role: 'owner' }] });
    await db.query(`INSERT INTO app_user (id, email) VALUES ($1, 'outsider@example.test')`, [u.outsider]);
    await db.query(`INSERT INTO site (slug, name) VALUES ('auth-test-site', 'Auth test site') ON CONFLICT DO NOTHING`);
  });

  afterAll(async () => {
    await app.close();
    await dropOrgs(db, [orgA, orgB, trialOrg], Object.values(u));
    await db.query(`DELETE FROM site WHERE slug = 'auth-test-site'`);
    await db.close();
  });

  const get = async (url: string, t?: string) => app.inject({ method: 'GET', url, headers: t ? bearer(t) : {} });

  it('answers 401 without a token, with a tampered, expired, not-yet-valid or foreign token', async () => {
    expect((await get('/api/sources/health')).statusCode).toBe(401);
    const good = await token(u.member);
    expect((await get('/api/sources/health', good)).statusCode).toBe(200);
    const [h, p, s] = good.split('.');
    const tampered = `${h}.${p}.${(s ?? '').slice(0, -4)}AAAA`;
    expect((await get('/api/sources/health', tampered)).statusCode).toBe(401);
    expect((await get('/api/sources/health', await token(u.member, orgA, { userId: u.member, expiresIn: -60 }))).statusCode).toBe(401);
    expect((await get('/api/sources/health', await token(u.member, orgA, { userId: u.member, notBefore: 120 }))).statusCode).toBe(401);
    const foreign = await signToken(await makeKeys(), { userId: u.member, orgId: orgA });
    expect((await get('/api/sources/health', foreign)).statusCode).toBe(401);
  });

  it('answers 401 for a wrong issuer or authorized party', async () => {
    const wrongIss = await token(u.member, orgA, { userId: u.member, iss: 'https://other.clerk.accounts.dev' });
    expect((await get('/api/sources/health', wrongIss)).statusCode).toBe(401);
    const wrongAzp = await token(u.member, orgA, { userId: u.member, azp: 'https://evil.example' });
    const res = await get('/api/sources/health', wrongAzp);
    expect(res.statusCode).toBe(401);
    expect(res.json()).toMatchObject({ error: 'unauthorized' });
  });

  it('keeps CORS and security headers on a 401 so the web app can read it', async () => {
    const res = await app.inject({ method: 'GET', url: '/api/me', headers: { origin: 'http://localhost:3000' } });
    expect(res.statusCode).toBe(401);
    expect(res.headers['access-control-allow-origin']).toBe('http://localhost:3000');
    expect(res.headers['x-content-type-options']).toBe('nosniff');
    const preflight = await app.inject({
      method: 'OPTIONS', url: '/api/watchlist/items',
      headers: { origin: 'http://localhost:3000', 'access-control-request-method': 'POST' },
    });
    expect(preflight.statusCode).toBe(204);
  });

  it('accepts the __session cookie', async () => {
    const res = await app.inject({ method: 'GET', url: '/api/me', headers: { cookie: `__session=${await token(u.viewer)}` } });
    expect(res.statusCode).toBe(200);
  });

  it('answers 403 with no active org, an unknown org or no membership', async () => {
    expect((await get('/api/me', await token(u.member, null))).json()).toMatchObject({ reason: 'no_active_org' });
    expect((await get('/api/me', await token(u.member, 'org_unknown'))).statusCode).toBe(403);
    expect((await get('/api/me', await token(u.outsider))).statusCode).toBe(403);
  });

  it('answers 403 when the role or the plan lacks the permission', async () => {
    const viewer = await token(u.viewer);
    const add = await app.inject({ method: 'POST', url: '/api/watchlist/items', headers: bearer(viewer), payload: { kind: 'site', refId: 'auth-test-site' } });
    expect(add.statusCode).toBe(403);
    expect(add.json()).toMatchObject({ reason: 'role_lacks_permission' });
    expect((await get('/api/export/sites.xlsx', viewer)).statusCode).toBe(403);
    const run = await app.inject({ method: 'POST', url: '/api/stress/run', headers: bearer(viewer), payload: {} });
    expect(run.statusCode).toBe(403);
    const trial = await get('/api/export/sites.xlsx', await token(u.trial, trialOrg));
    expect(trial.statusCode).toBe(403);
    expect(trial.json()).toMatchObject({ reason: 'plan_lacks_permission' });
  });

  it('returns the caller from /api/me with org role, products and permissions', async () => {
    const res = await get('/api/me', await token(u.member));
    expect(res.statusCode).toBe(200);
    const me = res.json();
    expect(me).toEqual({
      user: { id: u.member, email: `${u.member}@example.test`, name: null },
      org: { id: orgA, name: orgA },
      orgRole: 'member',
      products: [{ id: 'marden', name: 'Marden', plan: 'paid', status: 'active', role: 'member', seats: null, seatsUsed: 4 }],
      permissions: expect.arrayContaining(['watchlist:write', 'account:read', 'me:read']),
      staff: false,
    });
    expect(me.permissions).not.toContain('members:manage');
  });

  it("answers 404 for another org's watchlist items, saved scenarios and members", async () => {
    const other = await token(u.other, orgB);
    const add = await app.inject({ method: 'POST', url: '/api/watchlist/items', headers: bearer(other), payload: { kind: 'site', refId: 'auth-test-site' } });
    expect(add.statusCode).toBe(201);
    const itemId = add.json().items[0].id as string;
    const saved = await app.inject({
      method: 'POST', url: '/api/stress/saved', headers: bearer(other),
      payload: { name: 'B scenario', request: { scenario: 'power_delay', params: { months: 6 }, severity: 'base' } },
    });
    expect(saved.statusCode).toBe(201);
    const owner = await token(u.owner);
    expect((await app.inject({ method: 'DELETE', url: `/api/watchlist/items/${itemId}`, headers: bearer(owner) })).statusCode).toBe(404);
    expect((await get(`/api/stress/saved/${saved.json().id}`, owner)).statusCode).toBe(404);
    const patch = await app.inject({ method: 'PATCH', url: `/api/account/members/${u.other}`, headers: bearer(owner), payload: { role: 'viewer' } });
    expect(patch.statusCode).toBe(404);
    expect((await app.inject({ method: 'DELETE', url: `/api/watchlist/items/${itemId}`, headers: bearer(other) })).statusCode).toBe(204);
  });

  it('applies team rules: admins cannot manage owners, the last owner stays', async () => {
    const admin = await token(u.admin);
    const owner = await token(u.owner);
    const patch = (t: string, id: string, role: string) =>
      app.inject({ method: 'PATCH', url: `/api/account/members/${id}`, headers: bearer(t), payload: { role } });
    expect((await patch(admin, u.owner, 'viewer')).statusCode).toBe(403);
    expect((await patch(admin, u.member, 'owner')).statusCode).toBe(403);
    expect((await patch(owner, u.owner, 'admin')).json()).toMatchObject({ error: 'conflict', reason: 'last_owner' });
    const ok = await patch(admin, u.member, 'viewer');
    expect(ok.statusCode).toBe(200);
    expect(ok.json()).toMatchObject({ userId: u.member, role: 'viewer' });
    expect(clerkCalls).toEqual([]); // org roles are Marden's: no Clerk call
    expect((await patch(admin, u.member, 'member')).statusCode).toBe(200);
    const del = await app.inject({ method: 'DELETE', url: `/api/account/members/${u.owner}`, headers: bearer(owner) });
    expect(del.statusCode).toBe(409);
    const memberPatch = await patch(await token(u.member), u.viewer, 'member');
    expect(memberPatch.statusCode).toBe(403);
    const audit = await db.query(`SELECT count(*)::int AS n FROM audit_log WHERE org_id = $1 AND action = 'member.role'`, [orgA]);
    expect(audit.rows[0]?.n).toBe(2);
  });

  it('sends invites through Clerk, keeps the Marden role in org_invitation and audit-logs them', async () => {
    const res = await app.inject({
      method: 'POST', url: '/api/account/invites', headers: bearer(await token(u.admin)),
      payload: { email: 'new.analyst@example.test', role: 'member' },
    });
    expect(res.statusCode).toBe(201);
    expect(clerkCalls).toContain(`invite ${orgA} new.analyst@example.test`);
    const inv = await db.query(`SELECT id, email, role, status, invited_by FROM org_invitation WHERE org_id = $1`, [orgA]);
    expect(inv.rows).toEqual([{ id: res.json().invitationId, email: 'new.analyst@example.test', role: 'member', status: 'pending', invited_by: u.admin }]);
    const asOwner = await app.inject({
      method: 'POST', url: '/api/account/invites', headers: bearer(await token(u.admin)), payload: { email: 'x@example.test', role: 'owner' },
    });
    expect(asOwner.statusCode).toBe(403);
  });

  it('lists the team on /api/account', async () => {
    const res = await get('/api/account', await token(u.viewer));
    expect(res.statusCode).toBe(200);
    expect(res.json().team.map((m: { role: string }) => m.role)).toEqual(['owner', 'admin', 'member', 'viewer']);
    expect(res.json().team[0].products).toEqual([{ productId: 'marden', role: 'owner' }]);
    expect(res.json().products).toEqual([{
      id: 'marden', name: 'Marden', plan: 'paid', status: 'active', seats: null, seatsUsed: 4,
      startsOn: '2026-01-01', endsOn: null, live: true,
    }]);
    expect(res.json().org).not.toHaveProperty('plan');
    expect(res.json().seats).toEqual({ used: 4, total: null });
  });
});

describe('rate limit per user', () => {
  it('limits each signed-in user separately', async () => {
    const db = testDb();
    const keys = await makeKeys();
    const org = uid('org_rl');
    const [a, b] = [uid('user_rl_a'), uid('user_rl_b')];
    await seedOrg(db, { id: org, members: [{ id: a, role: 'member' }, { id: b, role: 'member' }] });
    const app = await makeApp({ mode: 'clerk', db, keys, env: { RATE_LIMIT_PER_MINUTE: '3' } });
    try {
      const hit = async (user: string) =>
        (await app.inject({ method: 'GET', url: '/api/me', headers: bearer(await signToken(keys, { userId: user, orgId: org })) })).statusCode;
      expect([await hit(a), await hit(a), await hit(a), await hit(a)]).toEqual([200, 200, 200, 429]);
      expect(await hit(b)).toBe(200);
    } finally {
      await app.close();
      await dropOrgs(db, [org], [a, b]);
      await db.close();
    }
  });
});
