// Alert email delivery (spec 13): the signed one-click unsubscribe (same token as the Python worker signs:
// shared vector), the confirm page a GET shows (it changes nothing), the POST that switches the email off
// and cancels the member's queued emails of that kind (idempotent, audited), honest 503 / 400 pages, and
// GET /api/alert-delivery: email configured or not and the caller's recent emails (variable names for staff
// only).
import { createHmac } from 'node:crypto';
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { signUnsubscribe, unsubscribed, verifyUnsubscribe } from '../src/lib/unsubscribe-token.js';
import { defaultAlertSettings } from '../src/lib/defaults.js';
import { bearer, dropOrgs, makeApp, makeKeys, seedOrg, signToken, testDb, uid, type Keys } from './helpers.js';

const SECRET = 'marden-test-secret-0123456789abcdefghij';
// services/tests/alerts/test_alert_email.py signs the same payload to the same token.
const VECTOR = 'eyJpYXQiOjE3NjAwMDAwMDAsIm8iOiJvcmdfYSIsInMiOiJkYWlseSIsInUiOiJ1c2VyX2ExIiwidiI6MX0'
  + '.tj6HMNoMWaYnmrSaUoneFglMgTfad2WRhUpLsRGFRRQ';
const ONE_CLICK = { 'content-type': 'application/x-www-form-urlencoded' };

describe('unsubscribe token', () => {
  it('matches the worker byte for byte and refuses anything altered', () => {
    const token = signUnsubscribe(SECRET, { orgId: 'org_a', userId: 'user_a1', scope: 'daily', issuedAt: 1760000000 });
    expect(token).toBe(VECTOR);
    expect(verifyUnsubscribe(SECRET, VECTOR)).toEqual({ v: 1, o: 'org_a', u: 'user_a1', s: 'daily', iat: 1760000000 });
    const [body, mac] = VECTOR.split('.') as [string, string];
    expect(verifyUnsubscribe(SECRET, `${body}x.${mac}`)).toBeNull();
    expect(verifyUnsubscribe(`${SECRET}x`, VECTOR)).toBeNull();
    expect(verifyUnsubscribe(SECRET, `${body}.${mac.slice(0, -2)}`)).toBeNull();
    expect(verifyUnsubscribe(SECRET, 'nonsense')).toBeNull();
    expect(verifyUnsubscribe(SECRET, '')).toBeNull();
    // a well-signed payload with an unknown scope is refused too
    const bad = Buffer.from(JSON.stringify({ iat: 1, o: 'o', s: 'everything', u: 'u', v: 1 })).toString('base64url');
    const signed = `${bad}.${createHmac('sha256', SECRET).update(bad).digest('base64url')}`;
    expect(verifyUnsubscribe(SECRET, signed)).toBeNull();
  });

  it('switches off only the delivery settings of its scope', () => {
    const d = defaultAlertSettings();
    expect(unsubscribed(d, 'daily')).toEqual({ ...d, dailyDigest: false });
    expect(unsubscribed(d, 'all')).toEqual({ ...d, instantRed: false, dailyDigest: false, weeklyNote: false });
    expect(unsubscribed(d, 'instant').types).toEqual(d.types);
  });
});

describe('alert email delivery routes', () => {
  let db: Db;
  let app: FastifyInstance;
  let unconfigured: FastifyInstance;
  let keys: Keys;
  const org = uid('org_ad');
  const u = { member: uid('user_ad'), staff: uid('user_ads'), other: uid('user_ado') };
  const token = (scope: 'all' | 'instant' | 'daily' | 'weekly', userId = u.member, orgId = org) =>
    signUnsubscribe(SECRET, { orgId, userId, scope, issuedAt: 1760000000 });
  const url = (t: string) => `/api/email/unsubscribe?token=${encodeURIComponent(t)}`;
  const settingsOf = async (userId: string) =>
    (await db.query<{ settings: Record<string, unknown> }>(`SELECT settings FROM alert_settings WHERE org_id = $1 AND user_id = $2`,
      [org, userId])).rows[0]?.settings ?? null;
  const outbox = async (kind: string, status = 'queued', userId = u.member) => {
    const res = await db.query<{ id: string }>(
      `INSERT INTO email_outbox (org_id, user_id, kind, idempotency_key, to_email, subject, alert_ids, status, last_error)
       VALUES ($1, $2, $3, $4, $5, $6, '{1,2}', $7, $8) RETURNING id::text`,
      [org, userId, kind, uid(`${kind}:${org}:${userId}`), `${userId}@example.test`, `Test ${kind}`, status,
        status === 'not_configured' ? 'email is not configured: set POSTMARK_TOKEN or RESEND_API_KEY' : null],
    );
    return res.rows[0]!.id;
  };
  const statusOf = async (id: string) =>
    (await db.query<{ status: string; last_error: string | null }>(`SELECT status, last_error FROM email_outbox WHERE id = $1`, [id])).rows[0];

  beforeAll(async () => {
    db = testDb();
    keys = await makeKeys();
    app = await makeApp({ mode: 'clerk', db, keys, env: { ALERT_LINK_SECRET: SECRET } });
    unconfigured = await makeApp({ mode: 'clerk', db, keys, env: { ALERT_LINK_SECRET: 'too-short' } });
    await seedOrg(db, { id: org, members: [
      { id: u.member, role: 'member' }, { id: u.staff, role: 'member', staff: true, email: `${u.staff}@marden.ai` },
      { id: u.other, role: 'member' }] });
  });
  afterAll(async () => {
    await app.close();
    await unconfigured.close();
    await db.query(`DELETE FROM email_provider_state WHERE id = 'alerts'`);
    await dropOrgs(db, [org], Object.values(u));
    await db.close();
  });

  it('GET shows a confirm page and changes nothing (mail scanners follow links)', async () => {
    const res = await app.inject({ method: 'GET', url: url(token('daily')) }); // no session
    expect(res.statusCode).toBe(200);
    expect(res.headers['content-type']).toMatch(/^text\/html/);
    expect(res.headers['content-security-policy']).toContain("form-action 'self'");
    expect(res.headers['cache-control']).toBe('no-store');
    expect(res.body).toContain('Unsubscribe from the daily digest?');
    expect(res.body).toContain('<form method="post">');
    expect(res.body).toContain('/account?tab=alerts');
    expect(await settingsOf(u.member)).toBeNull();
  });

  it('the one-click POST switches the email off, cancels queued emails of that kind, once', async () => {
    const daily = await outbox('daily_digest');
    const instant = await outbox('instant');
    const othersDaily = await outbox('daily_digest', 'queued', u.other);
    const res = await app.inject({ method: 'POST', url: url(token('daily')), headers: ONE_CLICK, payload: 'List-Unsubscribe=One-Click' });
    expect(res.statusCode).toBe(200);
    expect(res.body).toContain('Unsubscribed from the daily digest');
    expect(await settingsOf(u.member)).toEqual({ ...defaultAlertSettings(), dailyDigest: false });
    expect(await statusOf(daily)).toEqual({ status: 'cancelled', last_error: 'unsubscribed (daily) by the email link' });
    expect((await statusOf(instant))?.status).toBe('queued'); // another kind
    expect((await statusOf(othersDaily))?.status).toBe('queued'); // another member
    expect(await settingsOf(u.other)).toBeNull();

    const again = await app.inject({ method: 'POST', url: url(token('daily')), headers: ONE_CLICK, payload: 'List-Unsubscribe=One-Click' });
    expect(again.statusCode).toBe(200);
    expect(again.body).toContain('already unsubscribed');
    const audits = await db.query<{ meta: { scope: string; cancelled: number } }>(
      `SELECT meta FROM audit_log WHERE org_id = $1 AND user_id = $2 AND action = 'alert_settings.unsubscribe'`, [org, u.member]);
    expect(audits.rows.map((r) => [r.meta.scope, r.meta.cancelled])).toEqual([['daily', 1]]);
  });

  it('scope all switches off every alert email; the page button (no body) works too', async () => {
    const res = await app.inject({ method: 'POST', url: url(token('all', u.other)) });
    expect(res.statusCode).toBe(200);
    expect(await settingsOf(u.other)).toEqual({ ...defaultAlertSettings(), instantRed: false, dailyDigest: false, weeklyNote: false });
  });

  it('answers honestly when the link is bad, the member is gone, or links are not configured', async () => {
    const tampered = `${token('weekly').slice(0, -3)}abc`;
    const bad = await app.inject({ method: 'POST', url: url(tampered) });
    expect(bad.statusCode).toBe(400);
    expect(bad.body).toContain('nothing was changed');
    expect((await app.inject({ method: 'GET', url: '/api/email/unsubscribe' })).statusCode).toBe(400);
    const gone = await app.inject({ method: 'POST', url: url(token('weekly', 'user_never_here')) });
    expect(gone.statusCode).toBe(200);
    expect(gone.body).toContain('Nothing to unsubscribe');
    const off = await unconfigured.inject({ method: 'POST', url: url(token('weekly')) });
    expect(off.statusCode).toBe(503);
    expect(off.body).toContain('not configured');
    expect((await settingsOf(u.member))?.weeklyNote).toBe(true);
  });

  it('GET /api/alert-delivery: not configured, the caller’s emails, variable names for staff only', async () => {
    const as = async (who: string) => bearer(await signToken(keys, { userId: who, orgId: org, mfa: true }));
    expect((await app.inject({ method: 'GET', url: '/api/alert-delivery' })).statusCode).toBe(401);

    await db.query(`DELETE FROM email_provider_state WHERE id = 'alerts'`);
    const before = (await app.inject({ method: 'GET', url: '/api/alert-delivery', headers: await as(u.staff) })).json();
    expect(before.email).toEqual({ configured: false, provider: null, checkedAt: null,
      reason: 'the worker has not checked the email configuration yet' });

    await db.query(`INSERT INTO email_provider_state (id, provider, configured, from_email, reason)
                    VALUES ('alerts', NULL, false, NULL, 'email is not configured: set POSTMARK_TOKEN or RESEND_API_KEY')`);
    const nc = await outbox('instant', 'not_configured', u.staff);
    const res = await app.inject({ method: 'GET', url: '/api/alert-delivery', headers: await as(u.member) });
    expect(res.statusCode).toBe(200);
    const mine = res.json();
    expect(mine.email).toMatchObject({ configured: false, provider: null, reason: null });
    expect(mine.email.checkedAt).toMatch(/^\d{4}-\d{2}-\d{2}T/);
    expect(mine.to).toBe(`${u.member}@example.test`);
    expect(mine.recent.map((e: { kind: string; status: string }) => `${e.kind}:${e.status}`).sort())
      .toEqual(['daily_digest:cancelled', 'instant:queued']);
    expect(mine.recent.find((e: { status: string }) => e.status === 'cancelled').error).toBe('unsubscribed (daily) by the email link');
    expect(mine.recent[0]).toMatchObject({ alerts: 2, attempts: 0, sentAt: null });
    expect(mine.counts).toEqual({ queued: 1, sent: 0, failed: 0, not_configured: 0, cancelled: 1 });

    const staff = (await app.inject({ method: 'GET', url: '/api/alert-delivery', headers: await as(u.staff) })).json();
    expect(staff.email.reason).toContain('POSTMARK_TOKEN or RESEND_API_KEY');
    expect(staff.recent).toEqual([expect.objectContaining({ id: nc, status: 'not_configured',
      error: 'email is not configured: set POSTMARK_TOKEN or RESEND_API_KEY' })]);
    expect(staff.counts.not_configured).toBe(1);
  });
});
