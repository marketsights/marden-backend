// TEMPORARY adversarial probe (review); removed after the review.
import { randomBytes } from 'node:crypto';
import type { FastifyInstance } from 'fastify';
import { Webhook } from 'svix';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { ClerkAdmin } from '../src/auth/clerk-admin.js';
import type { Db } from '../src/db.js';
import { bearer, dropOrgs, makeApp, makeKeys, signToken, testDb, uid, type Keys } from './helpers.js';

describe('probe', () => {
  let db: Db;
  let app: FastifyInstance;
  let keys: Keys;
  const secret = `whsec_${randomBytes(24).toString('base64')}`;
  const signer = new Webhook(secret);
  const org = uid('org_probe');
  const org2 = uid('org_probe2');
  const u = { a: uid('ua'), b: uid('ub'), c: uid('uc'), d: uid('ud'), e: uid('ue'), f: uid('uf') };
  const clerkAdmin: ClerkAdmin = {
    invite: () => Promise.resolve({ id: uid('orginv'), expiresAt: Date.now() + 86_400_000 }),
    removeMember: () => Promise.resolve(),
    mfaEnabled: () => Promise.resolve(true),
  };
  const send = (evt: Record<string, unknown>) => {
    const body = JSON.stringify(evt);
    const msgId = uid('msg');
    const now = new Date();
    return app.inject({
      method: 'POST', url: '/api/webhooks/clerk', payload: body,
      headers: { 'content-type': 'application/json', 'svix-id': msgId, 'svix-timestamp': String(Math.floor(now.getTime() / 1000)), 'svix-signature': signer.sign(msgId, now, body) },
    });
  };
  const mem = (o: string, userId: string, type = 'organizationMembership.created', identifier: string | undefined = `${userId}@x.test`, ts = Date.now()) =>
    send({ type, timestamp: ts, data: { id: uid('orgmem'), role: 'org:admin', organization: { id: o, name: o }, public_user_data: { user_id: userId, ...(identifier ? { identifier } : {}) } } });
  const row = async (o: string, userId: string) =>
    (await db.query<{ role: string; role_source: string }>(`SELECT role, role_source FROM membership WHERE org_id = $1 AND user_id = $2`, [o, userId])).rows[0] ?? null;
  const call = async (method: 'POST' | 'PATCH' | 'DELETE', url: string, by: string, payload?: object, o = org) =>
    app.inject({ method, url, headers: bearer(await signToken(keys, { userId: by, orgId: o })), ...(payload ? { payload } : {}) });

  beforeAll(async () => {
    db = testDb();
    keys = await makeKeys();
    app = await makeApp({ mode: 'clerk', db, keys, webhookSecret: secret, clerkAdmin });
    await send({ type: 'organization.created', timestamp: Date.now(), data: { id: org, name: org } });
    await send({ type: 'organization.created', timestamp: Date.now(), data: { id: org2, name: org2 } });
  });
  afterAll(async () => {
    await app.close();
    await dropOrgs(db, [org, org2], Object.values(u));
    await db.close();
  });

  it('probes', async () => {
    await mem(org, u.a);
    await mem(org, u.b);
    expect(await row(org, u.a)).toMatchObject({ role: 'owner' });
    expect(await row(org, u.b)).toMatchObject({ role: 'viewer' });
    // promote b to admin
    expect((await call('PATCH', `/api/account/members/${u.b}`, u.a, { role: 'admin' })).statusCode).toBe(200);
    // admin sets owner's role to owner (no-op) -> 403
    const r1 = await call('PATCH', `/api/account/members/${u.a}`, u.b, { role: 'owner' });
    console.log('admin same-role on owner', r1.statusCode, r1.body);
    // updated for unseen membership
    await mem(org, u.c, 'organizationMembership.updated');
    console.log('updated unseen', await row(org, u.c));
    // pending invite matched through stored email when identifier missing
    await send({ type: 'user.created', timestamp: Date.now(), data: { id: u.d, email_addresses: [{ id: 'e1', email_address: 'D@Fund.test' }], primary_email_address_id: 'e1' } });
    expect((await call('POST', '/api/account/invites', u.a, { email: 'd@fund.test', role: 'member' })).statusCode).toBe(201);
    await mem(org, u.d, 'organizationMembership.created', undefined);
    console.log('stored email match', await row(org, u.d));
    // owner invites as owner, admin re-invites same email as viewer: which wins?
    expect((await call('POST', '/api/account/invites', u.a, { email: 'e@fund.test', role: 'owner' })).statusCode).toBe(201);
    expect((await call('POST', '/api/account/invites', u.b, { email: 'e@fund.test', role: 'viewer' })).statusCode).toBe(201);
    await mem(org, u.e, 'organizationMembership.created', 'e@fund.test');
    console.log('owner-invite superseded by admin invite', await row(org, u.e));
    // owner removed in Clerk and re-added: role?
    await mem(org, u.a, 'organizationMembership.deleted');
    await mem(org, u.a, 'organizationMembership.created');
    console.log('owner deleted+re-added', await row(org, u.a));
    // out of order: creator's event (earlier ts) arrives after another member's
    const t0 = Date.now() - 60_000;
    await mem(org2, u.f, 'organizationMembership.created', undefined, t0 + 1000);
    await mem(org2, u.c, 'organizationMembership.created', undefined, t0);
    console.log('out-of-order first', await row(org2, u.f), await row(org2, u.c));
  });
});
