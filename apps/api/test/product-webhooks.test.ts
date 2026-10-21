// Clerk webhooks with per-product access (docs/access-control.md): webhooks keep creating org, user and
// membership rows; a new member gets no product role (fail closed), except the first member of an org
// that already holds a live product, who becomes owner of each live product (seats permitting).
import { randomBytes } from 'node:crypto';
import type { FastifyInstance } from 'fastify';
import { Webhook } from 'svix';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { applyClerkEvent, type ClerkEvent } from '../src/queries/clerk-sync.js';
import { bearer, dropOrgs, makeApp, makeKeys, signToken, testDb, uid, type Keys } from './helpers.js';

describe('Clerk webhooks and product roles', () => {
  let db: Db;
  let app: FastifyInstance;
  let keys: Keys;
  const secret = `whsec_${randomBytes(24).toString('base64')}`;
  const signer = new Webhook(secret);
  const orgP = uid('org_wh_prod');
  const orgBare = uid('org_wh_bare');
  const orgFull = uid('org_wh_full');
  const users = { first: uid('user_wh_first'), second: uid('user_wh_second'), bare: uid('user_wh_bare'), full: uid('user_wh_full') };

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
  const orgCreated = (orgId: string) => send({ type: 'organization.created', timestamp: Date.now(), data: { id: orgId, name: orgId } });
  const joined = (orgId: string, userId: string, role = 'org:member', type = 'organizationMembership.created') => send({
    type, timestamp: Date.now(), object: 'event',
    data: { id: uid('orgmem'), role, organization: { id: orgId, name: orgId }, public_user_data: { user_id: userId, identifier: `${userId}@example.test` } },
  });
  const rolesOf = async (orgId: string, userId: string) =>
    (await db.query<{ product_id: string; role: string }>(
      `SELECT product_id, role FROM member_product_role WHERE org_id = $1 AND user_id = $2 ORDER BY product_id`, [orgId, userId],
    )).rows;
  const addProduct = (orgId: string, seats: number | null = null, status = 'active') => db.query(
    `INSERT INTO org_product (org_id, product_id, plan, seats, status, starts_on) VALUES ($1, 'marden', 'paid', $2, $3, '2026-01-01')`,
    [orgId, seats, status],
  );

  beforeAll(async () => {
    db = testDb();
    keys = await makeKeys();
    app = await makeApp({ mode: 'clerk', db, keys, webhookSecret: secret });
    for (const orgId of [orgP, orgBare, orgFull]) expect((await orgCreated(orgId)).statusCode).toBe(204);
    await addProduct(orgP);
    await addProduct(orgFull, 0);
  });
  afterAll(async () => {
    await app.close();
    await dropOrgs(db, [orgP, orgBare, orgFull], Object.values(users));
    await db.close();
  });

  it('makes the first member of an org with a live product its owner, and audit-logs it', async () => {
    expect((await joined(orgP, users.first, 'org:admin')).statusCode).toBe(204);
    expect(await rolesOf(orgP, users.first)).toEqual([{ product_id: 'marden', role: 'owner' }]);
    const audit = await db.query<{ user_id: string | null; target: string; meta: Record<string, unknown> }>(
      `SELECT user_id, target, meta FROM audit_log WHERE org_id = $1 AND action = 'member.product_role'`, [orgP],
    );
    expect(audit.rows).toEqual([{ user_id: null, target: `user:${users.first}`, meta: { productId: 'marden', from: null, to: 'owner', rule: 'first_member' } }]);
    const me = await app.inject({ method: 'GET', url: '/api/me', headers: bearer(await signToken(keys, { userId: users.first, orgId: orgP })) });
    expect(me.json()).toMatchObject({ orgRole: 'admin', products: [{ id: 'marden', role: 'owner', seatsUsed: 1 }] });
  });

  it('gives later members no product role until one is assigned (fail closed)', async () => {
    expect((await joined(orgP, users.second)).statusCode).toBe(204);
    expect(await rolesOf(orgP, users.second)).toEqual([]);
    const tok = await signToken(keys, { userId: users.second, orgId: orgP });
    expect((await app.inject({ method: 'GET', url: '/api/sites', headers: bearer(tok) })).json()).toEqual({ error: 'no_product' });
    const me = (await app.inject({ method: 'GET', url: '/api/me', headers: bearer(tok) })).json();
    expect(me.products).toEqual([{ id: 'marden', name: 'Marden', plan: 'paid', status: 'active', role: null, seats: null, seatsUsed: 1 }]);
  });

  it('does not re-grant on a membership update or change an existing product role', async () => {
    await db.query(`UPDATE member_product_role SET role = 'viewer' WHERE org_id = $1 AND user_id = $2`, [orgP, users.first]);
    expect((await joined(orgP, users.first, 'org:member', 'organizationMembership.updated')).statusCode).toBe(204);
    expect(await rolesOf(orgP, users.first)).toEqual([{ product_id: 'marden', role: 'viewer' }]);
    expect((await db.query(`SELECT role FROM membership WHERE org_id = $1 AND user_id = $2`, [orgP, users.first])).rows[0]?.role).toBe('member');
  });

  it('grants nothing in an org without a live product or without a free seat', async () => {
    expect((await joined(orgBare, users.bare)).statusCode).toBe(204);
    expect(await rolesOf(orgBare, users.bare)).toEqual([]);
    expect((await joined(orgFull, users.full)).statusCode).toBe(204);
    expect(await rolesOf(orgFull, users.full)).toEqual([]);
    expect((await db.query(`SELECT 1 FROM membership WHERE org_id = $1 AND user_id = $2`, [orgFull, users.full])).rowCount).toBe(1);
  });

  it('makes only one first member when two memberships of a new org arrive at once', async () => {
    const orgRace = uid('org_wh_race');
    const [a, b] = [uid('user_wh_a'), uid('user_wh_b')];
    expect((await orgCreated(orgRace)).statusCode).toBe(204);
    await addProduct(orgRace);
    const created = (userId: string): ClerkEvent => ({
      type: 'organizationMembership.created', timestamp: Date.now(),
      data: { id: uid('orgmem'), role: 'org:member', organization: { id: orgRace, name: orgRace }, public_user_data: { user_id: userId } },
    });
    const [ca, cb] = [await db.pool.connect(), await db.pool.connect()];
    try {
      await ca.query('BEGIN');
      await cb.query('BEGIN');
      await applyClerkEvent(ca, created(a), { id: uid('msg'), at: Date.now() }); // a joins first, not committed yet
      const pid = (await cb.query<{ pid: number }>('SELECT pg_backend_pid() AS pid')).rows[0]?.pid;
      const second = applyClerkEvent(cb, created(b), { id: uid('msg'), at: Date.now() });
      for (let i = 0; i < 100; i += 1) { // b must be waiting on a's transaction before a commits
        const w = await db.query<{ t: string | null }>(`SELECT wait_event_type AS t FROM pg_stat_activity WHERE pid = $1`, [pid]);
        if (w.rows[0]?.t === 'Lock') break;
        await new Promise((r) => setTimeout(r, 50));
      }
      await ca.query('COMMIT');
      await second;
      await cb.query('COMMIT');
    } finally {
      ca.release();
      cb.release();
    }
    expect(await rolesOf(orgRace, a)).toEqual([{ product_id: 'marden', role: 'owner' }]);
    expect(await rolesOf(orgRace, b)).toEqual([]);
    await dropOrgs(db, [orgRace], [a, b]);
  });

  it("removes the member's product roles when the membership is deleted", async () => {
    await db.query(`INSERT INTO member_product_role (org_id, user_id, product_id, role) VALUES ($1, $2, 'marden', 'member')`, [orgP, users.second]);
    expect(await rolesOf(orgP, users.second)).toEqual([{ product_id: 'marden', role: 'member' }]);
    expect((await joined(orgP, users.second, 'org:member', 'organizationMembership.deleted')).statusCode).toBe(204);
    expect(await rolesOf(orgP, users.second)).toEqual([]);
  });
});
