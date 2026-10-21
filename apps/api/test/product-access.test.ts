// Per-product access (docs/access-control.md) in Clerk mode: two products with different matrices, a
// member with a role only on product B gets only B's permissions, plan gates follow the granting
// product, a suspended / ended / out-of-dates / inactive product grants nothing, an org without a live
// product gets 403 no_product, and a lapsed product closes the stream. Dev mode keeps working.
import type { AddressInfo } from 'node:net';
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { ORG_ROLE_PERMISSIONS } from '../src/auth/permissions.js';
import { PRODUCTS, type ProductRegistry } from '../src/auth/products/index.js';
import type { Db } from '../src/db.js';
import { bearer, dropOrgs, makeApp, makeKeys, seedOrg, signToken, testDb, uid, type Keys } from './helpers.js';
import { connect, until } from './sse-client.js';

const B = uid('pricing');
/** Product B: reads instruments; its members may also export. Nothing else of the V1 app. */
const TWO_PRODUCTS: ProductRegistry = {
  ...PRODUCTS,
  [B]: {
    viewer: ['instruments:read'],
    member: ['instruments:read', 'export:xlsx'],
    admin: ['instruments:read', 'export:xlsx'],
    owner: ['instruments:read', 'export:xlsx'],
  },
};
const day = (offset: number): string => new Date(Date.now() + offset * 86_400_000).toISOString().slice(0, 10);

describe('per-product access (Clerk mode)', () => {
  let db: Db;
  let app: FastifyInstance;
  let keys: Keys;
  let port: number;
  const orgTwo = uid('org_two');
  const orgLapse = uid('org_lapse');
  const orgNone = uid('org_none');
  const u = {
    onlyA: uid('user_a'), onlyB: uid('user_b'), both: uid('user_ab'), noRole: uid('user_norole'),
    lapse: uid('user_lapse'), none: uid('user_none'),
  };
  const token = (userId: string, orgId: string) => signToken(keys, { userId, orgId });
  const get = async (url: string, userId: string, orgId: string) =>
    app.inject({ method: 'GET', url, headers: bearer(await token(userId, orgId)) });
  const status = async (url: string, userId: string, orgId: string) => (await get(url, userId, orgId)).statusCode;
  const setLapse = (sql: string) => db.query(`UPDATE org_product SET ${sql} WHERE org_id = $1 AND product_id = 'marden'`, [orgLapse]);

  beforeAll(async () => {
    db = testDb();
    keys = await makeKeys();
    await db.query(`INSERT INTO product (id, name) VALUES ($1, 'Pricing (test)')`, [B]);
    // V1 app on trial, product B paid: the export gate depends on which product grants it.
    await seedOrg(db, {
      id: orgTwo,
      products: [{ productId: 'marden', plan: 'trial' }, { productId: B, plan: 'paid' }],
      members: [
        { id: u.onlyA, role: 'member', products: { marden: 'member' } },
        { id: u.onlyB, role: 'member', products: { [B]: 'member' } },
        { id: u.both, role: 'owner', products: { marden: 'member', [B]: 'viewer' } },
        { id: u.noRole, role: 'admin', products: {} },
      ],
    });
    await seedOrg(db, { id: orgLapse, members: [{ id: u.lapse, role: 'member' }] });
    await seedOrg(db, { id: orgNone, products: [], members: [{ id: u.none, role: 'owner' }] });
    app = await makeApp({ mode: 'clerk', db, keys, products: TWO_PRODUCTS, env: { STREAM_RECHECK_MS: '100' } });
    await app.listen({ host: '127.0.0.1', port: 0 });
    port = (app.server.address() as AddressInfo).port;
  });

  afterAll(async () => {
    await app.close();
    await dropOrgs(db, [orgTwo, orgLapse, orgNone], Object.values(u));
    await db.query(`DELETE FROM product WHERE id = $1`, [B]);
    await db.close();
  });

  it("a member with a role only on product B gets only B's permissions", async () => {
    const me = (await get('/api/me', u.onlyB, orgTwo)).json();
    expect(me.permissions).toEqual(['account:read', 'export:xlsx', 'instruments:read', 'me:read']);
    expect(me.products).toEqual([
      { id: 'marden', name: 'Marden', plan: 'trial', status: 'active', role: null, seats: null, seatsUsed: 2 },
      { id: B, name: 'Pricing (test)', plan: 'paid', status: 'active', role: 'member', seats: null, seatsUsed: 2 },
    ]);
    expect([401, 403]).not.toContain(await status('/api/instruments', u.onlyB, orgTwo));
    const sites = await get('/api/sites', u.onlyB, orgTwo);
    expect(sites.statusCode).toBe(403);
    expect(sites.json()).toEqual({ error: 'forbidden', reason: 'role_lacks_permission' });
    const add = await app.inject({
      method: 'POST', url: '/api/watchlist/items', headers: bearer(await token(u.onlyB, orgTwo)), payload: { kind: 'site', refId: 'x' },
    });
    expect(add.statusCode).toBe(403);
  });

  it('applies the plan gate of the product that grants the permission', async () => {
    // Export via the V1 app on trial: refused by plan. Via product B (paid): allowed.
    const trial = await get('/api/export/sites.xlsx', u.onlyA, orgTwo);
    expect(trial.json()).toMatchObject({ reason: 'plan_lacks_permission' });
    expect((await get('/api/me', u.onlyA, orgTwo)).json().permissions).not.toContain('export:xlsx');
    expect([401, 403]).not.toContain(await status('/api/export/sites.xlsx', u.onlyB, orgTwo));
    // u.both is a viewer on B (no export) and a member of the V1 app on trial: still refused by plan.
    expect((await get('/api/export/sites.xlsx', u.both, orgTwo)).json()).toMatchObject({ reason: 'plan_lacks_permission' });
    expect([401, 403]).not.toContain(await status('/api/sites', u.both, orgTwo));
  });

  it('a member without a product role keeps org administration but no data', async () => {
    const res = await get('/api/sites', u.noRole, orgTwo);
    expect(res.statusCode).toBe(403);
    expect(res.json()).toEqual({ error: 'no_product' });
    const me = (await get('/api/me', u.noRole, orgTwo)).json();
    expect(me.orgRole).toBe('admin');
    expect(me.products.map((p: { role: string | null }) => p.role)).toEqual([null, null]);
    expect(me.permissions).toEqual([...ORG_ROLE_PERMISSIONS.admin].sort());
    expect(await status('/api/account', u.noRole, orgTwo)).toBe(200);
  });

  it('an org with no product: /api/me answers products [] and every data route 403 no_product', async () => {
    const me = await get('/api/me', u.none, orgNone);
    expect(me.statusCode).toBe(200);
    expect(me.json()).toMatchObject({ orgRole: 'owner', products: [], permissions: [...ORG_ROLE_PERMISSIONS.owner].sort() });
    for (const url of ['/api/sites', '/api/home', '/api/watchlist', '/api/stream', '/api/export/sites.xlsx', '/api/search?q=ab']) {
      const res = await get(url, u.none, orgNone);
      expect(res.statusCode, url).toBe(403);
      expect(res.json(), url).toEqual({ error: 'no_product' });
    }
    expect((await get('/api/account', u.none, orgNone)).json().products).toEqual([]);
  });

  it('a suspended, ended, not yet started or expired product grants nothing', async () => {
    expect([401, 403]).not.toContain(await status('/api/sites', u.lapse, orgLapse));
    const lapses = [`status = 'suspended'`, `status = 'ended'`, `ends_on = '${day(-1)}'`, `starts_on = '${day(1)}'`];
    for (const change of lapses) {
      await setLapse(change);
      const res = await get('/api/sites', u.lapse, orgLapse);
      expect(res.json(), change).toEqual({ error: 'no_product' });
      const me = (await get('/api/me', u.lapse, orgLapse)).json();
      expect(me.products, change).toEqual([]);
      expect(me.permissions, change).not.toContain('sites:read');
      await setLapse(`status = 'active', starts_on = '2026-01-01', ends_on = NULL`);
    }
    // Dates are inclusive: a product that starts and ends today grants access today.
    await setLapse(`starts_on = '${day(0)}', ends_on = '${day(0)}'`);
    expect([401, 403]).not.toContain(await status('/api/sites', u.lapse, orgLapse));
    await setLapse(`starts_on = '2026-01-01', ends_on = NULL`);
  });

  it('an inactive product grants nothing anywhere', async () => {
    await db.query(`UPDATE product SET active = false WHERE id = $1`, [B]);
    try {
      expect((await get('/api/instruments', u.onlyB, orgTwo)).json()).toEqual({ error: 'no_product' });
      expect((await get('/api/me', u.onlyB, orgTwo)).json().products.map((p: { id: string }) => p.id)).toEqual(['marden']);
    } finally {
      await db.query(`UPDATE product SET active = true WHERE id = $1`, [B]);
    }
  });

  it('closes the stream on the next re-check once the product lapses', async () => {
    const sse = await connect(port, undefined, bearer(await token(u.lapse, orgLapse)));
    expect(sse.status).toBe(200);
    await until(() => app.streams.size === 1);
    await setLapse(`status = 'suspended'`);
    try {
      await until(() => app.streams.size === 0);
    } finally {
      sse.close();
      await setLapse(`status = 'active'`);
    }
  });
});

describe('per-product access (dev mode)', () => {
  it('gives the dev org product marden (paid) with the dev user as owner', async () => {
    const db = testDb();
    const app = await makeApp({ mode: 'dev', db });
    try {
      const me = (await app.inject({ method: 'GET', url: '/api/me' })).json();
      expect(me).toMatchObject({ org: { id: 'dev' }, orgRole: 'owner', staff: true });
      expect(me.products).toEqual([{ id: 'marden', name: 'Marden', plan: 'paid', status: 'active', role: 'owner', seats: null, seatsUsed: 1 }]);
      expect(me.permissions).toEqual(expect.arrayContaining(['export:xlsx', 'watchlist:write', 'members:manage', 'products:manage']));
    } finally {
      await app.close();
      await db.close();
    }
  });
});
