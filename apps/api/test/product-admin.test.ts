// Product roles and the firm's products (docs/access-control.md): org owners and admins (not members)
// set a member's product role within the seat limit; staff create and change a firm's products; every
// change writes audit_log.
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { bearer, dropOrgs, makeApp, makeKeys, seedOrg, signToken, testDb, uid, type Keys } from './helpers.js';

describe('product roles and firm products', () => {
  let db: Db;
  let app: FastifyInstance;
  let keys: Keys;
  const orgS = uid('org_seats');
  const orgOther = uid('org_other');
  const orgNew = uid('org_new');
  const orgStaff = uid('org_staff');
  const noMatrix = uid('nomatrix');
  const u = {
    owner: uid('user_owner'), admin: uid('user_admin'), member: uid('user_member'), newbie: uid('user_newbie'),
    other: uid('user_other'), first: uid('user_first'), staff: uid('user_staff'),
  };
  const token = (userId: string, orgId: string, mfa = true) => signToken(keys, { userId, orgId, mfa });
  const call = async (method: 'GET' | 'POST' | 'PATCH', url: string, who: [string, string, boolean?], payload?: object) =>
    app.inject({ method, url, headers: bearer(await token(...who)), ...(payload ? { payload } : {}) });
  const assign = (by: string, target: string, body: object) =>
    call('PATCH', `/api/account/members/${target}/products`, [by, orgS], body);
  const auditRows = async (orgId: string, action: string) =>
    (await db.query<{ user_id: string | null; target: string; meta: Record<string, unknown> }>(
      `SELECT user_id, target, meta FROM audit_log WHERE org_id = $1 AND action = $2 ORDER BY id`, [orgId, action],
    )).rows;
  const asStaff = (method: 'POST' | 'PATCH', url: string, payload: object, mfa = true) =>
    call(method, url, [u.staff, orgStaff, mfa], payload);

  beforeAll(async () => {
    db = testDb();
    keys = await makeKeys();
    app = await makeApp({ mode: 'clerk', db, keys });
    await seedOrg(db, {
      id: orgS, products: [{ productId: 'marden', seats: 2 }],
      members: [
        { id: u.owner, role: 'owner' },
        { id: u.admin, role: 'admin', products: {} },
        { id: u.member, role: 'member' },
        { id: u.newbie, role: 'viewer', products: {} },
      ],
    });
    await seedOrg(db, { id: orgOther, members: [{ id: u.other, role: 'owner' }] });
    await seedOrg(db, { id: orgNew, products: [], members: [{ id: u.first, role: 'owner' }] });
    await seedOrg(db, { id: orgStaff, products: [], members: [{ id: u.staff, role: 'member', staff: true, email: `${u.staff}@marden.ai` }] });
    await db.query(`INSERT INTO product (id, name) VALUES ($1, 'No matrix (test)')`, [noMatrix]);
  });

  afterAll(async () => {
    await app.close();
    await dropOrgs(db, [orgS, orgOther, orgNew, orgStaff], Object.values(u));
    await db.query(`DELETE FROM product WHERE id = $1`, [noMatrix]);
    await db.close();
  });

  it('lets org owners and admins assign product roles, not members', async () => {
    const byMember = await assign(u.member, u.newbie, { productId: 'marden', role: 'viewer' });
    expect(byMember.statusCode).toBe(403);
    expect(byMember.json()).toMatchObject({ reason: 'role_lacks_permission' });
    // The org admin holds no product role and still manages product roles (org administration).
    const removed = await assign(u.admin, u.member, { productId: 'marden', role: null });
    expect(removed.statusCode).toBe(200);
    expect(removed.json()).toMatchObject({ userId: u.member, role: 'member', products: [] });
    expect((await call('GET', '/api/sites', [u.member, orgS])).json()).toEqual({ error: 'no_product' });
    const added = await assign(u.admin, u.newbie, { productId: 'marden', role: 'viewer' });
    expect(added.json()).toMatchObject({ userId: u.newbie, role: 'viewer', products: [{ productId: 'marden', role: 'viewer' }] });
    expect([401, 403]).not.toContain((await call('GET', '/api/sites', [u.newbie, orgS])).statusCode);
    const byOwner = await assign(u.owner, u.newbie, { productId: 'marden', role: 'member' });
    expect(byOwner.json().products).toEqual([{ productId: 'marden', role: 'member' }]);
  });

  it('enforces the seat limit: a new assignment needs a free seat, a role change does not', async () => {
    // Seats 2: owner + newbie hold them now.
    const full = await assign(u.owner, u.member, { productId: 'marden', role: 'viewer' });
    expect(full.statusCode).toBe(409);
    expect(full.json()).toEqual({ error: 'conflict', reason: 'seat_limit', seats: 2, used: 2 });
    expect((await assign(u.owner, u.newbie, { productId: 'marden', role: 'viewer' })).statusCode).toBe(200);
    const account = (await call('GET', '/api/account', [u.admin, orgS])).json();
    expect(account.products[0]).toMatchObject({ id: 'marden', seats: 2, seatsUsed: 2 });
    // Freeing a seat lets the next assignment in.
    expect((await assign(u.owner, u.newbie, { productId: 'marden', role: null })).statusCode).toBe(200);
    expect((await assign(u.owner, u.member, { productId: 'marden', role: 'member' })).statusCode).toBe(200);
  });

  it('answers 404 for another org, an unknown member or product, 400 for a bad body', async () => {
    expect((await assign(u.owner, u.other, { productId: 'marden', role: 'viewer' })).statusCode).toBe(404);
    const unknown = await assign(u.owner, u.member, { productId: 'not-a-product', role: 'viewer' });
    expect(unknown.json()).toEqual({ error: 'not_found', what: 'product' });
    expect((await assign(u.owner, u.member, { productId: 'marden', role: 'superuser' })).statusCode).toBe(400);
    expect((await assign(u.owner, u.member, { productId: 'marden' })).statusCode).toBe(400);
  });

  it('audit-logs every product role change with who, from and to', async () => {
    const rows = await auditRows(orgS, 'member.product_role');
    expect(rows.map((r) => [r.user_id, r.target, r.meta])).toEqual([
      [u.admin, `user:${u.member}`, { productId: 'marden', from: 'member', to: null }],
      [u.admin, `user:${u.newbie}`, { productId: 'marden', from: null, to: 'viewer' }],
      [u.owner, `user:${u.newbie}`, { productId: 'marden', from: 'viewer', to: 'member' }],
      [u.owner, `user:${u.newbie}`, { productId: 'marden', from: 'member', to: 'viewer' }],
      [u.owner, `user:${u.newbie}`, { productId: 'marden', from: 'viewer', to: null }],
      [u.owner, `user:${u.member}`, { productId: 'marden', from: null, to: 'member' }],
    ]);
  });

  it('admin product endpoints are staff only (MFA and a @marden.ai address)', async () => {
    const body = { productId: 'marden', plan: 'paid', seats: 5, startsOn: '2026-10-01' };
    const byOwner = await call('POST', `/api/admin/orgs/${orgNew}/products`, [u.owner, orgS], body);
    expect(byOwner.statusCode).toBe(403);
    expect(byOwner.json()).toMatchObject({ reason: 'role_lacks_permission' });
    expect((await asStaff('POST', `/api/admin/orgs/${orgNew}/products`, body, false)).statusCode).toBe(403);
    const patch = await call('PATCH', `/api/admin/orgs/${orgS}/products/marden`, [u.owner, orgS], { seats: 100 });
    expect(patch.statusCode).toBe(403);
  });

  it('staff give a firm a product; members get no role until an org owner or admin assigns one', async () => {
    const created = await asStaff('POST', `/api/admin/orgs/${orgNew}/products`, {
      productId: 'marden', plan: 'design_partner', seats: 5, startsOn: '2026-10-01',
    });
    expect(created.statusCode).toBe(201);
    expect(created.json()).toEqual({
      id: 'marden', name: 'Marden', plan: 'design_partner', status: 'active', seats: 5, seatsUsed: 0,
      startsOn: '2026-10-01', endsOn: null, live: true,
    });
    expect((await call('GET', '/api/sites', [u.first, orgNew])).json()).toEqual({ error: 'no_product' });
    expect((await call('PATCH', `/api/account/members/${u.first}/products`, [u.first, orgNew], { productId: 'marden', role: 'owner' })).statusCode).toBe(200);
    expect([401, 403]).not.toContain((await call('GET', '/api/sites', [u.first, orgNew])).statusCode);
    expect((await call('GET', '/api/me', [u.first, orgNew])).json().products).toEqual([
      { id: 'marden', name: 'Marden', plan: 'design_partner', status: 'active', role: 'owner', seats: 5, seatsUsed: 1 },
    ]);
  });

  it('refuses a duplicate, an unknown org or product, a product without a matrix and bad dates', async () => {
    const body = { productId: 'marden', plan: 'paid', seats: null, startsOn: '2026-10-01' };
    expect((await asStaff('POST', `/api/admin/orgs/${orgNew}/products`, body)).json()).toMatchObject({ reason: 'org_product_exists' });
    expect((await asStaff('POST', '/api/admin/orgs/org_missing/products', body)).json()).toEqual({ error: 'not_found', what: 'org' });
    expect((await asStaff('POST', `/api/admin/orgs/${orgOther}/products`, { ...body, productId: 'nope' })).statusCode).toBe(404);
    const empty = await asStaff('POST', `/api/admin/orgs/${orgOther}/products`, { ...body, productId: noMatrix });
    expect(empty.json()).toMatchObject({ reason: 'product_has_no_permission_matrix' });
    const dates = await asStaff('POST', `/api/admin/orgs/${orgStaff}/products`, { ...body, endsOn: '2026-09-30' });
    expect(dates.json()).toMatchObject({ reason: 'ends_on_before_starts_on' });
    expect((await asStaff('POST', `/api/admin/orgs/${orgStaff}/products`, { productId: 'marden', plan: 'paid' })).statusCode).toBe(400);
  });

  it('staff change plan, seats, dates and status; seats cannot drop below those in use', async () => {
    const url = `/api/admin/orgs/${orgNew}/products/marden`;
    const below = await asStaff('PATCH', url, { seats: 0 });
    expect(below.json()).toEqual({ error: 'conflict', reason: 'seats_in_use', seats: 0, used: 1 });
    const suspended = await asStaff('PATCH', url, { status: 'suspended' });
    expect(suspended.json()).toMatchObject({ status: 'suspended', live: false });
    expect((await call('GET', '/api/sites', [u.first, orgNew])).json()).toEqual({ error: 'no_product' });
    const back = await asStaff('PATCH', url, { status: 'active', plan: 'paid', seats: 1, endsOn: '2027-12-31' });
    expect(back.json()).toMatchObject({ status: 'active', plan: 'paid', seats: 1, seatsUsed: 1, endsOn: '2027-12-31', live: true });
    expect([401, 403]).not.toContain((await call('GET', '/api/sites', [u.first, orgNew])).statusCode);
    expect((await asStaff('PATCH', url, { endsOn: '2026-09-01' })).json()).toMatchObject({ reason: 'ends_on_before_starts_on' });
    expect((await asStaff('PATCH', url, {})).statusCode).toBe(400);
    expect((await asStaff('PATCH', `/api/admin/orgs/${orgOther}/products/${noMatrix}`, { seats: 1 })).statusCode).toBe(404);
  });

  it('audit-logs every firm product change with the staff user, before and after', async () => {
    const created = await auditRows(orgNew, 'org_product.create');
    expect(created.map((r) => [r.user_id, r.target, r.meta])).toEqual([[u.staff, `org_product:${orgNew}:marden`, {
      productId: 'marden', to: { plan: 'design_partner', seats: 5, startsOn: '2026-10-01', endsOn: null, status: 'active' },
    }]]);
    const updated = await auditRows(orgNew, 'org_product.update');
    expect(updated.map((r) => r.user_id)).toEqual([u.staff, u.staff]);
    expect(updated[0]?.meta).toMatchObject({ from: { status: 'active' }, to: { status: 'suspended' } });
    expect(updated[1]?.meta).toMatchObject({
      from: { status: 'suspended', plan: 'design_partner', seats: 5, endsOn: null },
      to: { status: 'active', plan: 'paid', seats: 1, endsOn: '2027-12-31' },
    });
  });
});
