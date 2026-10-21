// Test helpers: app builders for dev and Clerk mode, locally generated Clerk-style session tokens,
// and inserts for rows a test needs (each test removes what it adds).
import { randomBytes } from 'node:crypto';
import type { FastifyInstance } from 'fastify';
import { exportSPKI, generateKeyPair, SignJWT, type CryptoKey } from 'jose';
import { inject } from 'vitest';
import { buildApp, type AppDeps } from '../src/app.js';
import type { ClerkAdmin } from '../src/auth/clerk-admin.js';
import { loadConfig } from '../src/config.js';
import { createDb, type Db } from '../src/db.js';

export const WEB_ORIGIN = 'http://localhost:3000';
export const FRONTEND_API = 'test-instance.clerk.accounts.dev';
export const ISSUER = `https://${FRONTEND_API}`;
const PUBLISHABLE_KEY = `pk_test_${Buffer.from(`${FRONTEND_API}$`).toString('base64')}`;

export const dbUrl = (): string => inject('databaseUrl');
export const testDb = (): Db => createDb(dbUrl(), 4);
export const uid = (prefix: string): string => `${prefix}_${randomBytes(4).toString('hex')}`;

export interface Keys { privateKey: CryptoKey; publicPem: string }

export async function makeKeys(): Promise<Keys> {
  const { privateKey, publicKey } = await generateKeyPair('RS256', { extractable: true });
  return { privateKey, publicPem: await exportSPKI(publicKey) };
}

export interface TokenClaims {
  userId: string;
  orgId?: string | null;
  azp?: string;
  iss?: string;
  expiresIn?: number;   // seconds from now; negative = already expired
  notBefore?: number;   // seconds from now
  sessionId?: string;
  mfa?: boolean;
}

export async function signToken(keys: Keys, c: TokenClaims): Promise<string> {
  const now = Math.floor(Date.now() / 1000);
  const payload: Record<string, unknown> = { azp: c.azp ?? WEB_ORIGIN, sid: c.sessionId ?? uid('sess'), v: 2, fva: [0, c.mfa ? 0 : -1] };
  if (c.orgId) payload.o = { id: c.orgId, rol: 'member', slg: c.orgId };
  return new SignJWT(payload)
    .setProtectedHeader({ alg: 'RS256', typ: 'JWT', kid: 'ins_test' })
    .setIssuer(c.iss ?? ISSUER)
    .setSubject(c.userId)
    .setIssuedAt(now - 10)
    .setNotBefore(now + (c.notBefore ?? -10))
    .setExpirationTime(now + (c.expiresIn ?? 60))
    .sign(keys.privateKey);
}

export interface AppOptions {
  mode: 'dev' | 'clerk';
  db: Db;
  keys?: Keys;
  webhookSecret?: string;
  clerkAdmin?: ClerkAdmin | null;
  env?: Record<string, string>;
  extraRoutes?: AppDeps['extraRoutes'];
}

export async function makeApp(o: AppOptions): Promise<FastifyInstance> {
  const config = loadConfig({
    NODE_ENV: 'test',
    DATABASE_URL: dbUrl(),
    MARDEN_AUTH_MODE: o.mode,
    WEB_ORIGIN,
    LOG_LEVEL: 'silent',
    ...(o.keys ? { CLERK_JWT_KEY: o.keys.publicPem, CLERK_PUBLISHABLE_KEY: PUBLISHABLE_KEY } : {}),
    ...(o.webhookSecret ? { CLERK_WEBHOOK_SIGNING_SECRET: o.webhookSecret } : {}),
    ...(o.env ?? {}),
  });
  const app = await buildApp({
    config,
    db: o.db,
    clerkAdmin: o.clerkAdmin ?? null,
    ...(o.extraRoutes ? { extraRoutes: o.extraRoutes } : {}),
  });
  await app.ready();
  return app;
}

export interface Member { id: string; role: 'owner' | 'admin' | 'member' | 'viewer'; email?: string; staff?: boolean }

export async function seedOrg(db: Db, org: { id: string; plan?: string; members: Member[] }): Promise<void> {
  await db.query(`INSERT INTO org (id, name, plan) VALUES ($1, $1, $2)`, [org.id, org.plan ?? 'paid']);
  for (const m of org.members) {
    await db.query(`INSERT INTO app_user (id, email, is_staff) VALUES ($1, $2, $3) ON CONFLICT (id) DO NOTHING`, [
      m.id, m.email ?? `${m.id}@example.test`, m.staff ?? false,
    ]);
    await db.query(`INSERT INTO membership (org_id, user_id, role) VALUES ($1, $2, $3)`, [org.id, m.id, m.role]);
  }
}

export async function dropOrgs(db: Db, orgIds: string[], userIds: string[]): Promise<void> {
  await db.query(`DELETE FROM org WHERE id = ANY($1)`, [orgIds]);
  await db.query(`DELETE FROM app_user WHERE id = ANY($1)`, [userIds]);
}

export async function putReadModel(db: Db, ref: { view: string; key: string; asOf: string }, doc: unknown): Promise<void> {
  const { view, key, asOf } = ref;
  await db.query(
    `INSERT INTO read_model (view, key, as_of, doc) VALUES ($1, $2, $3, $4)
     ON CONFLICT (view, key, as_of) DO UPDATE SET doc = EXCLUDED.doc`,
    [view, key, asOf, JSON.stringify(doc)],
  );
}

export async function clearReadModels(db: Db, views: string[]): Promise<void> {
  await db.query(`DELETE FROM read_model WHERE view = ANY($1)`, [views]);
}

export const sourced = <T>(value: T | null, tag: 'F' | 'R' | 'M' = 'F', asOf = '2026-10-15', url = 'https://example.test/doc') =>
  ({ value, tag, asOf, sourceUrl: url, sourceName: 'Test record' });

export const bearer = (token: string): Record<string, string> => ({ authorization: `Bearer ${token}` });
