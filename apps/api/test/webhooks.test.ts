// Clerk webhooks (spec 23 test 3): signature checked, processed once, replay changes nothing,
// out-of-order membership events end in the right state, deleting a user removes access.
import { randomBytes } from 'node:crypto';
import type { FastifyInstance } from 'fastify';
import { Webhook } from 'svix';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { bearer, dropOrgs, makeApp, makeKeys, signToken, testDb, uid, type Keys } from './helpers.js';

describe('POST /api/webhooks/clerk', () => {
  let db: Db;
  let app: FastifyInstance;
  let keys: Keys;
  const secret = `whsec_${randomBytes(24).toString('base64')}`;
  const signer = new Webhook(secret);
  const orgId = uid('org_wh');
  const userId = uid('user_wh');

  const send = (evt: Record<string, unknown>, opts: { id?: string; signature?: string } = {}) => {
    const body = JSON.stringify(evt);
    const msgId = opts.id ?? uid('msg');
    const now = new Date();
    return app.inject({
      method: 'POST',
      url: '/api/webhooks/clerk',
      headers: {
        'content-type': 'application/json',
        'svix-id': msgId,
        'svix-timestamp': String(Math.floor(now.getTime() / 1000)),
        'svix-signature': opts.signature ?? signer.sign(msgId, now, body),
      },
      payload: body,
    });
  };
  const membership = (type: string, role: string, timestamp: number) => ({
    type, timestamp, object: 'event',
    data: { id: 'orgmem_1', role, organization: { id: orgId, name: 'Webhook Fund' }, public_user_data: { user_id: userId, identifier: 'pm@example.test', first_name: 'Pat', last_name: 'Lee' } },
  });
  const roleOf = async () =>
    (await db.query<{ role: string }>(`SELECT role FROM membership WHERE org_id = $1 AND user_id = $2`, [orgId, userId])).rows[0]?.role ?? null;

  beforeAll(async () => {
    db = testDb();
    keys = await makeKeys();
    app = await makeApp({ mode: 'clerk', db, keys, webhookSecret: secret });
  });
  afterAll(async () => {
    await app.close();
    await dropOrgs(db, [orgId], [userId]);
    await db.close();
  });

  it('rejects a bad or missing signature with 400', async () => {
    const evt = { type: 'user.created', data: { id: userId }, timestamp: Date.now() };
    const bad = await send(evt, { signature: `v1,${randomBytes(32).toString('base64')}` });
    expect(bad.statusCode).toBe(400);
    expect(bad.json()).toMatchObject({ reason: 'bad_signature' });
    const missing = await app.inject({ method: 'POST', url: '/api/webhooks/clerk', headers: { 'content-type': 'application/json' }, payload: JSON.stringify(evt) });
    expect(missing.statusCode).toBe(400);
    expect((await db.query(`SELECT 1 FROM app_user WHERE id = $1`, [userId])).rowCount).toBe(0);
  });

  it('rejects a tampered body, another secret and a stale or future timestamp', async () => {
    const body = JSON.stringify({ type: 'user.created', data: { id: userId }, timestamp: Date.now() });
    const post = (headers: Record<string, string>, payload = body) =>
      app.inject({ method: 'POST', url: '/api/webhooks/clerk', headers: { 'content-type': 'application/json', ...headers }, payload });
    const signed = (msgId: string, at: Date, signedBody: string, by = signer) => ({
      'svix-id': msgId, 'svix-timestamp': String(Math.floor(at.getTime() / 1000)), 'svix-signature': by.sign(msgId, at, signedBody),
    });
    const now = new Date();
    const tampered = body.replace(userId, `${userId}_other`);
    expect((await post(signed('msg_t1', now, body), tampered)).statusCode).toBe(400);
    const otherSecret = new Webhook(`whsec_${randomBytes(24).toString('base64')}`);
    expect((await post(signed('msg_t2', now, body, otherSecret))).statusCode).toBe(400);
    const stale = new Date(now.getTime() - 10 * 60_000);
    expect((await post(signed('msg_t3', stale, body))).statusCode).toBe(400);
    const future = new Date(now.getTime() + 10 * 60_000);
    expect((await post(signed('msg_t4', future, body))).statusCode).toBe(400);
    const replayedId = signed('msg_t5', now, body);
    expect((await post({ ...replayedId, 'svix-id': 'msg_t6' })).statusCode).toBe(400); // id is signed too
    expect((await db.query(`SELECT 1 FROM app_user WHERE id = $1`, [userId])).rowCount).toBe(0);
    expect((await db.query(`SELECT 1 FROM audit_log WHERE target LIKE 'msg_t%'`)).rowCount).toBe(0);
    // A charset parameter on the content type still reaches the signature check with the exact bytes.
    const ignored = JSON.stringify({ type: 'email.created', data: { id: 'ema_1' }, timestamp: Date.now() });
    const ok = await post({ ...signed('msg_c1', now, ignored), 'content-type': 'application/json; charset=utf-8' }, ignored);
    expect(ok.statusCode).toBe(204);
  });

  it('answers 503 when the signing secret is not configured', async () => {
    const bare = await makeApp({ mode: 'clerk', db, keys });
    const res = await bare.inject({ method: 'POST', url: '/api/webhooks/clerk', headers: { 'content-type': 'application/json' }, payload: '{}' });
    expect(res.statusCode).toBe(503);
    await bare.close();
  });

  it('processes a signed event once; a replay changes nothing', async () => {
    const evt = {
      type: 'user.created', timestamp: Date.now(), object: 'event',
      data: { id: userId, first_name: 'Pat', last_name: 'Lee', primary_email_address_id: 'idn_1', email_addresses: [{ id: 'idn_1', email_address: 'pm@example.test' }] },
    };
    expect((await send(evt, { id: 'msg_replay_1' })).statusCode).toBe(204);
    const user = await db.query(`SELECT email, name FROM app_user WHERE id = $1`, [userId]);
    expect(user.rows[0]).toEqual({ email: 'pm@example.test', name: 'Pat Lee' });
    const count = async () => (await db.query(`SELECT count(*)::int AS n FROM audit_log WHERE target = 'msg_replay_1'`)).rows[0]?.n;
    expect(await count()).toBe(1);
    await db.query(`UPDATE app_user SET name = 'Changed locally' WHERE id = $1`, [userId]);
    expect((await send(evt, { id: 'msg_replay_1' })).statusCode).toBe(204);
    expect((await db.query(`SELECT name FROM app_user WHERE id = $1`, [userId])).rows[0]?.name).toBe('Changed locally');
    expect(await count()).toBe(1);
  });

  it('ends out-of-order membership events in the state of the newest event', async () => {
    const t = Date.now();
    expect((await send(membership('organizationMembership.created', 'org:member', t))).statusCode).toBe(204);
    expect(await roleOf()).toBe('member');
    expect((await send(membership('organizationMembership.deleted', 'org:member', t + 2000))).statusCode).toBe(204);
    expect(await roleOf()).toBeNull();
    // An older update delivered late must not bring the membership back.
    expect((await send(membership('organizationMembership.updated', 'org:admin', t + 1000))).statusCode).toBe(204);
    expect(await roleOf()).toBeNull();
    expect((await send(membership('organizationMembership.created', 'org:viewer', t + 3000))).statusCode).toBe(204);
    expect(await roleOf()).toBe('viewer');
    expect((await db.query(`SELECT plan FROM org WHERE id = $1`, [orgId])).rows[0]?.plan).toBe('trial');
  });

  it('maps an unknown Clerk role to no access', async () => {
    expect((await send(membership('organizationMembership.updated', 'org:billing_contact', Date.now() + 4000))).statusCode).toBe(204);
    expect(await roleOf()).toBeNull();
    expect((await send(membership('organizationMembership.updated', 'org:viewer', Date.now() + 5000))).statusCode).toBe(204);
    expect(await roleOf()).toBe('viewer');
  });

  it('deleting a user removes access on the next request', async () => {
    const tok = await signToken(keys, { userId, orgId });
    expect((await app.inject({ method: 'GET', url: '/api/me', headers: bearer(tok) })).statusCode).toBe(200);
    expect((await send({ type: 'user.deleted', timestamp: Date.now() + 10_000, data: { id: userId, deleted: true } })).statusCode).toBe(204);
    const after = await app.inject({ method: 'GET', url: '/api/me', headers: bearer(tok) });
    expect(after.statusCode).toBe(403);
  });
});
