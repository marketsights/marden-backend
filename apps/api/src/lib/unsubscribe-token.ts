// Signed one-click unsubscribe links in alert emails (services/marden/alerts/links.py writes them).
// Token = base64url(JSON payload) + "." + base64url(HMAC-SHA256(ALERT_LINK_SECRET, first part)); payload
// {"iat": unix seconds, "o": org id, "s": scope, "u": user id, "v": 1}. Scope says what the link switches
// off: every alert email ('all'), instant red alerts, the daily digest or the weekly summary. Both sides
// share a test vector (services/tests/alerts/test_alert_email.py, test/alert-delivery.test.ts).
import { createHmac, timingSafeEqual } from 'node:crypto';
import type { AlertSettings } from '../contracts/types.js';

export const UNSUBSCRIBE_SCOPES = ['all', 'instant', 'daily', 'weekly'] as const;
export type UnsubscribeScope = (typeof UNSUBSCRIBE_SCOPES)[number];
export interface UnsubscribePayload { v: 1; o: string; u: string; s: UnsubscribeScope; iat: number }

/** The AlertSettings switches a scope turns off. */
export const SCOPE_SETTINGS: Readonly<Record<UnsubscribeScope, readonly ('instantRed' | 'dailyDigest' | 'weeklyNote')[]>> = {
  all: ['instantRed', 'dailyDigest', 'weeklyNote'],
  instant: ['instantRed'],
  daily: ['dailyDigest'],
  weekly: ['weeklyNote'],
};
/** email_outbox.kind per scope: queued emails of these kinds are cancelled by the unsubscribe. */
export const SCOPE_KINDS: Readonly<Record<UnsubscribeScope, readonly string[]>> = {
  all: ['instant', 'daily_digest', 'weekly_digest'],
  instant: ['instant'],
  daily: ['daily_digest'],
  weekly: ['weekly_digest'],
};
export const SCOPE_LABEL: Readonly<Record<UnsubscribeScope, string>> = {
  all: 'all alert email', instant: 'instant red alerts', daily: 'the daily digest', weekly: 'the weekly summary',
};
export const MIN_SECRET_LENGTH = 32;

const mac = (secret: string, body: string): Buffer => createHmac('sha256', secret).update(body, 'ascii').digest();

/** Signs a payload the way the worker does (tests and tools; the API only verifies). */
export function signUnsubscribe(secret: string, p: { orgId: string; userId: string; scope: UnsubscribeScope; issuedAt: number }): string {
  const payload = { iat: Math.trunc(p.issuedAt), o: p.orgId, s: p.scope, u: p.userId, v: 1 };
  const body = Buffer.from(JSON.stringify(payload), 'utf8').toString('base64url');
  return `${body}.${mac(secret, body).toString('base64url')}`;
}

const BASE64URL = /^[A-Za-z0-9_-]+$/;

/** The payload when the signature matches and the fields are well formed, else null. */
export function verifyUnsubscribe(secret: string, token: string): UnsubscribePayload | null {
  const dot = token.indexOf('.');
  if (dot <= 0) return null;
  const body = token.slice(0, dot);
  const given = token.slice(dot + 1);
  if (!BASE64URL.test(body) || !BASE64URL.test(given)) return null;
  const expected = mac(secret, body);
  const actual = Buffer.from(given, 'base64url');
  if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) return null;
  let payload: unknown;
  try {
    payload = JSON.parse(Buffer.from(body, 'base64url').toString('utf8'));
  } catch {
    return null;
  }
  if (typeof payload !== 'object' || payload === null) return null;
  const p = payload as Record<string, unknown>;
  const ok = p.v === 1 && typeof p.o === 'string' && typeof p.u === 'string'
    && (UNSUBSCRIBE_SCOPES as readonly unknown[]).includes(p.s) && typeof p.iat === 'number';
  return ok ? { v: 1, o: p.o as string, u: p.u as string, s: p.s as UnsubscribeScope, iat: p.iat as number } : null;
}

/** The settings after an unsubscribe of `scope` (only the delivery switches change). */
export function unsubscribed(settings: AlertSettings, scope: UnsubscribeScope): AlertSettings {
  const next = { ...settings };
  for (const key of SCOPE_SETTINGS[scope]) next[key] = false;
  return next;
}
