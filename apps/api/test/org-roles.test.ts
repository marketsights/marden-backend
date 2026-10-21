// Org roles are Marden's (docs/access-control.md): the shared Clerk instance only says who exists and
// who belongs to which org. The first member is owner, an invited member gets the role on Marden's
// invitation, anyone else is a viewer; Clerk webhooks never change a role after creation; admins can't
// change or remove an owner's org or product roles (owners can); Clerk is never asked to change a role.
import { randomBytes } from 'node:crypto';
import type { FastifyInstance } from 'fastify';
import { Webhook } from 'svix';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { ClerkAdmin } from '../src/auth/clerk-admin.js';
import type { Db } from '../src/db.js';
import { bearer, dropOrgs, makeApp, makeKeys, signToken, testDb, uid, type Keys } from './helpers.js';

describe('Marden-owned org roles', () => {
  let db: Db;
  let app: FastifyInstance;
  let keys: Keys;
  const secret = `whsec_${randomBytes(24).toString('base64')}`;
  const signer = new Webhook(secret);
  const org = uid('org_roles');
  const u = { owner: uid('user_owner'), extra: uid('user_extra'), analyst: uid('user_analyst'), gone: uid('user_gone'), late: uid('user_late') };
  /** Every ClerkAdmin call, by method: the interface has no role call, and these prove none is attempted. */
  const calls: string[] = [];
  let expiresAt: number | null = null;
  const clerkAdmin: ClerkAdmin = {
    invite: (orgId, email, inviter) => {
      calls.push(`invite ${orgId} ${email} ${inviter}`);
      return Promise.resolve({ id: uid('orginv'), expiresAt });
    },
    removeMember: (orgId, userId) => { calls.push(`remove ${orgId} ${userId}`); return Promise.resolve(); },
    mfaEnabled: () => Promise.resolve(true),
  };

  const send = (evt: Record<string, unknown>) => {
    const body = JSON.stringify(evt);
    const msgId = uid('msg');
    const now = new Date();
    return app.inject({
      method: 'POST', url: '/api/webhooks/clerk', payload: body,
      headers: {
        'content-type': 'application/json', 'svix-id': msgId,
        'svix-timestamp': String(Math.floor(now.getTime() / 1000)), 'svix-signature': signer.sign(msgId, now, body),
      },
    });
  };
  /** A Clerk membership event; `clerkRole` is whatever the shared instance says, which Marden ignores. */
  const membership = (userId: string, clerkRole: string, type = 'organizationMembership.created', identifier = `${userId}@example.test`) =>
    send({
      type, timestamp: Date.now(), object: 'event',
      data: { id: uid('orgmem'), role: clerkRole, organization: { id: org, name: 'Roles Fund' }, public_user_data: { user_id: userId, identifier } },
    });
  const row = async (userId: string) =>
    (await db.query<{ role: string; role_source: string }>(
      `SELECT role, role_source FROM membership WHERE org_id = $1 AND user_id = $2`, [org, userId],
    )).rows[0] ?? null;
  const call = async (method: 'POST' | 'PATCH' | 'DELETE', url: string, by: string, payload?: object) =>
    app.inject({ method, url, headers: bearer(await signToken(keys, { userId: by, orgId: org })), ...(payload ? { payload } : {}) });
  const invite = (by: string, email: string, role: string) => call('POST', '/api/account/invites', by, { email, role });
  const setRole = (by: string, target: string, role: string) => call('PATCH', `/api/account/members/${target}`, by, { role });
  const setProduct = (by: string, target: string, role: string | null) =>
    call('PATCH', `/api/account/members/${target}/products`, by, { productId: 'marden', role });

  beforeAll(async () => {
    db = testDb();
    keys = await makeKeys();
    app = await makeApp({ mode: 'clerk', db, keys, webhookSecret: secret, clerkAdmin });
    expect((await send({ type: 'organization.created', timestamp: Date.now(), data: { id: org, name: 'Roles Fund' } })).statusCode).toBe(204);
    await db.query(`INSERT INTO org_product (org_id, product_id, plan, starts_on) VALUES ($1, 'marden', 'paid', '2026-01-01')`, [org]);
  });
  afterAll(async () => {
    await app.close();
    await dropOrgs(db, [org], Object.values(u));
    await db.close();
  });

  it('makes the first member owner and a member without an invitation a viewer, whatever Clerk says', async () => {
    expect((await membership(u.owner, 'org:member')).statusCode).toBe(204);
    expect(await row(u.owner)).toEqual({ role: 'owner', role_source: 'first_member' });
    expect((await membership(u.extra, 'org:owner')).statusCode).toBe(204);
    expect(await row(u.extra)).toEqual({ role: 'viewer', role_source: 'default' });
    const joins = await db.query(`SELECT target, meta FROM audit_log WHERE org_id = $1 AND action = 'member.join' ORDER BY id`, [org]);
    expect(joins.rows).toEqual([
      { target: `user:${u.owner}`, meta: { role: 'owner', rule: 'first_member', invitationId: null } },
      { target: `user:${u.extra}`, meta: { role: 'viewer', rule: 'default', invitationId: null } },
    ]);
  });

  it("carries the invited role on Marden's invitation, never as a Clerk role", async () => {
    expiresAt = Date.now() + 7 * 86_400_000;
    const res = await invite(u.owner, 'Analyst@Fund.test', 'admin');
    expect(res.statusCode).toBe(201);
    const { invitationId } = res.json<{ invitationId: string }>();
    expect(res.json()).toEqual({ invitationId, email: 'Analyst@Fund.test', role: 'admin' });
    expect(calls).toEqual([`invite ${org} Analyst@Fund.test ${u.owner}`]);
    // Clerk creates the membership with its basic role; the address may differ in case.
    expect((await membership(u.analyst, 'org:member', 'organizationMembership.created', 'analyst@fund.TEST')).statusCode).toBe(204);
    expect(await row(u.analyst)).toEqual({ role: 'admin', role_source: 'invitation' });
    const inv = await db.query(`SELECT email, role, status, invited_by, accepted_by FROM org_invitation WHERE id = $1`, [invitationId]);
    expect(inv.rows).toEqual([{ email: 'analyst@fund.test', role: 'admin', status: 'accepted', invited_by: u.owner, accepted_by: u.analyst }]);
    // No product role comes with the org role (fail closed); the org admin assigns one.
    expect((await db.query(`SELECT 1 FROM member_product_role WHERE org_id = $1 AND user_id = $2`, [org, u.analyst])).rowCount).toBe(0);
    expect((await invite(u.analyst, 'boss@fund.test', 'owner')).json()).toMatchObject({ reason: 'admins_cannot_manage_owners' });
    expect((await invite(u.extra, 'friend@fund.test', 'viewer')).statusCode).toBe(403);
  });

  it("stops an admin from replacing an owner's pending owner invitation; the owner can", async () => {
    const before = calls.length;
    expect((await invite(u.owner, 'chair@fund.test', 'owner')).statusCode).toBe(201);
    // The newest open invitation decides the role on joining, so a lesser one from an admin is refused.
    for (const role of ['viewer', 'admin']) {
      expect((await invite(u.analyst, 'Chair@Fund.test', role)).json()).toMatchObject({ reason: 'admins_cannot_manage_owners' });
    }
    expect(calls.length).toBe(before + 1);
    // Admins still invite anyone else; the owner can replace their own invitation.
    expect((await invite(u.analyst, 'deputy@fund.test', 'admin')).statusCode).toBe(201);
    expect((await invite(u.owner, 'chair@fund.test', 'admin')).statusCode).toBe(201);
    const open = await db.query(
      `SELECT role, invited_by FROM org_invitation WHERE org_id = $1 AND email = 'chair@fund.test' AND status = 'pending' ORDER BY created_at, role`, [org],
    );
    expect(open.rows).toEqual([{ role: 'owner', invited_by: u.owner }, { role: 'admin', invited_by: u.owner }]);
  });

  it('ignores revoked and expired invitations', async () => {
    const revoked = (await invite(u.owner, 'gone@fund.test', 'admin')).json<{ invitationId: string }>();
    const revoke = { type: 'organizationInvitation.revoked', timestamp: Date.now(), data: { id: revoked.invitationId, organization_id: org, status: 'revoked', role: 'org:member' } };
    expect((await send(revoke)).statusCode).toBe(204);
    expect((await membership(u.gone, 'org:admin', 'organizationMembership.created', 'gone@fund.test')).statusCode).toBe(204);
    expect(await row(u.gone)).toEqual({ role: 'viewer', role_source: 'default' });
    expiresAt = Date.now() - 60_000;
    expect((await invite(u.owner, 'late@fund.test', 'admin')).statusCode).toBe(201);
    expect((await membership(u.late, 'org:member', 'organizationMembership.created', 'late@fund.test')).statusCode).toBe(204);
    expect(await row(u.late)).toEqual({ role: 'viewer', role_source: 'default' });
    const open = await db.query(`SELECT email, status FROM org_invitation WHERE org_id = $1 AND email IN ('gone@fund.test', 'late@fund.test') ORDER BY email`, [org]);
    expect(open.rows).toEqual([{ email: 'gone@fund.test', status: 'revoked' }, { email: 'late@fund.test', status: 'pending' }]);
  });

  it('never changes a role from a Clerk webhook, before or after a Marden change', async () => {
    for (const clerkRole of ['org:owner', 'org:admin', 'org:viewer', 'org:billing_contact']) {
      expect((await membership(u.extra, clerkRole, 'organizationMembership.updated')).statusCode).toBe(204);
      expect(await row(u.extra)).toEqual({ role: 'viewer', role_source: 'default' });
    }
    expect((await setRole(u.owner, u.extra, 'member')).statusCode).toBe(200);
    expect(await row(u.extra)).toEqual({ role: 'member', role_source: 'changed' });
    expect((await membership(u.extra, 'org:viewer', 'organizationMembership.updated')).statusCode).toBe(204);
    expect((await membership(u.extra, 'org:owner', 'organizationMembership.created')).statusCode).toBe(204);
    expect(await row(u.extra)).toEqual({ role: 'member', role_source: 'changed' });
    const audit = await db.query(`SELECT user_id, meta FROM audit_log WHERE org_id = $1 AND action = 'member.role'`, [org]);
    expect(audit.rows).toEqual([{ user_id: u.owner, meta: { from: 'viewer', to: 'member' } }]);
  });

  it("stops admins from changing or removing an owner's org role or product roles; owners can", async () => {
    const admin = u.analyst;
    const refused = { error: 'forbidden', reason: 'admins_cannot_manage_owners' };
    expect((await setRole(admin, u.owner, 'viewer')).json()).toMatchObject(refused);
    expect((await call('DELETE', `/api/account/members/${u.owner}`, admin)).json()).toMatchObject(refused);
    expect((await setProduct(admin, u.owner, 'viewer')).json()).toMatchObject(refused);
    expect((await setProduct(admin, u.owner, null)).json()).toMatchObject(refused);
    expect((await setRole(admin, u.extra, 'owner')).json()).toMatchObject(refused);
    // Admins manage everyone else's product roles.
    expect((await setProduct(admin, u.extra, 'member')).statusCode).toBe(200);
    // Owners can make an owner and change another owner's product roles; then admins can't touch them.
    expect((await setRole(u.owner, u.extra, 'owner')).statusCode).toBe(200);
    expect((await setProduct(u.owner, u.extra, 'viewer')).statusCode).toBe(200);
    expect((await setProduct(admin, u.extra, 'member')).json()).toMatchObject(refused);
    expect((await db.query(`SELECT role FROM member_product_role WHERE org_id = $1 AND user_id = $2`, [org, u.extra])).rows)
      .toEqual([{ role: 'viewer' }]);
    expect((await db.query(`SELECT role FROM member_product_role WHERE org_id = $1 AND user_id = $2`, [org, u.owner])).rows)
      .toEqual([{ role: 'owner' }]);
    // Owners remove owners; the last owner can't be removed or demoted.
    expect((await call('DELETE', `/api/account/members/${u.extra}`, u.owner)).statusCode).toBe(204);
    expect(calls).toContain(`remove ${org} ${u.extra}`);
    expect((await setRole(u.owner, u.owner, 'admin')).json()).toMatchObject({ error: 'conflict', reason: 'last_owner' });
    expect((await call('DELETE', `/api/account/members/${u.owner}`, u.owner)).json()).toMatchObject({ error: 'conflict', reason: 'last_owner' });
    expect(await row(u.owner)).toEqual({ role: 'owner', role_source: 'first_member' });
  });

  it('calls Clerk only to invite and remove, never for a role', () => {
    expect(calls.length).toBeGreaterThan(0);
    for (const c of calls) expect(c).toMatch(/^(invite|remove) /);
    expect(Object.keys(clerkAdmin).sort()).toEqual(['invite', 'mfaEnabled', 'removeMember']);
  });

  it('changes roles without Clerk configured; invitations and removals need it', async () => {
    const bare = await makeApp({ mode: 'clerk', db, keys, clerkAdmin: null });
    try {
      const as = async (method: 'POST' | 'PATCH' | 'DELETE', url: string, payload?: object) =>
        bare.inject({ method, url, headers: bearer(await signToken(keys, { userId: u.owner, orgId: org })), ...(payload ? { payload } : {}) });
      expect((await as('PATCH', `/api/account/members/${u.late}`, { role: 'member' })).statusCode).toBe(200);
      expect(await row(u.late)).toEqual({ role: 'member', role_source: 'changed' });
      expect((await as('DELETE', `/api/account/members/${u.late}`)).json()).toMatchObject({ reason: 'clerk_backend_api_not_configured' });
      expect((await as('POST', '/api/account/invites', { email: 'x@fund.test', role: 'viewer' })).statusCode).toBe(503);
    } finally {
      await bare.close();
    }
  });
});
