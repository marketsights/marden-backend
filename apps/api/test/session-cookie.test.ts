// Spec 23 with the web app calling the API same-origin: the Clerk session arrives as the
// Authorization bearer (fetch) or as Clerk's `__session` cookie (EventSource, Excel links), and both
// go through the same verification and the same role / plan / org checks.
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { bearer, dropOrgs, makeApp, makeKeys, seedOrg, signToken, testDb, uid, type Keys } from './helpers.js';

describe('Clerk session from the bearer or the __session cookie', () => {
  let db: Db;
  let app: FastifyInstance;
  let keys: Keys;
  let port: number;
  const org = uid('org_cookie');
  const u = { member: uid('user_cookie_member'), viewer: uid('user_cookie_viewer') };
  const token = (userId: string, extra: Partial<Parameters<typeof signToken>[1]> = {}) => signToken(keys, { userId, orgId: org, ...extra });
  const cookie = (t: string) => ({ cookie: `theme=light; __session=${t}; other=1` });
  const get = (url: string, headers: Record<string, string>) => app.inject({ method: 'GET', url, headers });

  beforeAll(async () => {
    db = testDb();
    keys = await makeKeys();
    app = await makeApp({ mode: 'clerk', db, keys, env: { STREAM_HEARTBEAT_MS: '200' } });
    await seedOrg(db, { id: org, members: [{ id: u.member, role: 'member' }, { id: u.viewer, role: 'viewer' }] });
    await db.query(`INSERT INTO site (slug, name) VALUES ('cookie-test-site', 'Cookie test site') ON CONFLICT DO NOTHING`);
    await app.listen({ host: '127.0.0.1', port: 0 });
    port = (app.server.address() as AddressInfo).port;
  });

  afterAll(async () => {
    await app.close();
    await dropOrgs(db, [org], Object.values(u));
    await db.query(`DELETE FROM site WHERE slug = 'cookie-test-site'`);
    await db.close();
  });

  it('accepts a valid cookie session on a read', async () => {
    const res = await get('/api/me', cookie(await token(u.member)));
    expect(res.statusCode).toBe(200);
    expect(res.json()).toMatchObject({ user: { id: u.member }, role: 'member' });
  });

  it('verifies the cookie exactly like the bearer: tampered, expired, not yet valid, foreign, wrong party, wrong issuer', async () => {
    const good = await token(u.member);
    const [h, p, s] = good.split('.');
    const bad = [
      `${h}.${p}.${(s ?? '').slice(0, -4)}AAAA`,
      await token(u.member, { expiresIn: -60 }),
      await token(u.member, { notBefore: 120 }),
      await signToken(await makeKeys(), { userId: u.member, orgId: org }),
      await token(u.member, { azp: 'https://evil.example' }),
      await token(u.member, { iss: 'https://other.clerk.accounts.dev' }),
    ];
    for (const t of bad) {
      const viaCookie = await get('/api/me', cookie(t));
      const viaBearer = await get('/api/me', bearer(t));
      expect(viaCookie.statusCode).toBe(401);
      expect(viaCookie.json()).toEqual(viaBearer.json());
    }
  });

  it('applies the same role and org checks to a cookie session (Excel link)', async () => {
    const viewer = await get('/api/export/sites.xlsx', cookie(await token(u.viewer)));
    expect(viewer.statusCode).toBe(403);
    expect(viewer.json()).toMatchObject({ reason: 'role_lacks_permission' });
    const member = await get('/api/export/sites.xlsx', cookie(await token(u.member)));
    expect([401, 403]).not.toContain(member.statusCode);
    const noOrg = await get('/api/me', cookie(await token(u.member, { orgId: null })));
    expect(noOrg.json()).toMatchObject({ reason: 'no_active_org' });
  });

  it('lets a bearer decide when both are present: no fallback from a bad bearer to the cookie', async () => {
    const both = await get('/api/me', { ...bearer(await token(u.viewer)), ...cookie(await token(u.member)) });
    expect(both.json()).toMatchObject({ role: 'viewer' });
    const badBearer = await get('/api/me', { ...bearer('not-a-token'), ...cookie(await token(u.member)) });
    expect(badBearer.statusCode).toBe(401);
    const emptyBearer = await get('/api/me', { authorization: 'Bearer ', ...cookie(await token(u.member)) });
    expect(emptyBearer.statusCode).toBe(401);
    // A non-bearer Authorization (e.g. a staging proxy's basic auth) leaves the cookie in charge.
    const basic = await get('/api/me', { authorization: 'Basic dXNlcjpwYXNz', ...cookie(await token(u.member)) });
    expect(basic.statusCode).toBe(200);
  });

  it('never accepts the cookie alone on a write (CSRF): writes need the bearer', async () => {
    const t = await token(u.member);
    const payload = { kind: 'site', refId: 'cookie-test-site' };
    const viaCookie = await app.inject({ method: 'POST', url: '/api/watchlist/items', headers: cookie(t), payload });
    expect(viaCookie.statusCode).toBe(401);
    expect(viaCookie.json()).toMatchObject({ reason: 'missing_token' });
    const viaBearer = await app.inject({ method: 'POST', url: '/api/watchlist/items', headers: bearer(t), payload });
    expect(viaBearer.statusCode).toBe(201);
    const id = viaBearer.json().items.find((i: { refId: string }) => i.refId === 'cookie-test-site').id as string;
    expect((await app.inject({ method: 'DELETE', url: `/api/watchlist/items/${id}`, headers: cookie(t) })).statusCode).toBe(401);
    expect((await app.inject({ method: 'DELETE', url: `/api/watchlist/items/${id}`, headers: bearer(t) })).statusCode).toBe(204);
  });

  it('opens the SSE stream with the cookie (EventSource cannot send a header), and refuses it without', async () => {
    const open = (headers: Record<string, string>) =>
      new Promise<{ status: number; type: string | undefined; close: () => void }>((resolve, reject) => {
        const req = http.get({ host: '127.0.0.1', port, path: '/api/stream', headers }, (res) => {
          res.resume();
          resolve({ status: res.statusCode ?? 0, type: res.headers['content-type'], close: () => req.destroy() });
        });
        req.on('error', reject);
      });
    const live = await open(cookie(await token(u.viewer)));
    expect(live.status).toBe(200);
    expect(live.type).toContain('text/event-stream');
    live.close();
    const anonymous = await open({});
    expect(anonymous.status).toBe(401);
    anonymous.close();
    const expired = await open(cookie(await token(u.viewer, { expiresIn: -60 })));
    expect(expired.status).toBe(401);
    expired.close();
  });
});
