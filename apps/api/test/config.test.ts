// Fail closed (spec 23): dev auth is refused outside development/test and on any deployed
// environment, also when the app is built from a hand-made config; production needs Clerk keys.
import { afterEach, describe, expect, it } from 'vitest';
import { buildApp } from '../src/app.js';
import { issuerFromPublishableKey, loadConfig } from '../src/config.js';

const DATABASE_URL = 'postgres://marden:marden@localhost:5432/marden';

describe('loadConfig', () => {
  const saved = { ...process.env };
  afterEach(() => {
    process.env = { ...saved };
  });

  it('uses the direct Neon endpoint for the stream when DATABASE_URL_UNPOOLED is set', () => {
    const base = { NODE_ENV: 'test', MARDEN_AUTH_MODE: 'dev' };
    expect(loadConfig({ ...base, DATABASE_URL }).databaseUrlUnpooled).toBe(DATABASE_URL);
    const pooled = 'postgresql://u:p@ep-x-pooler.c-7.us-east-2.aws.neon.tech/app?sslmode=require';
    const direct = 'postgresql://u:p@ep-x.c-7.us-east-2.aws.neon.tech/app?sslmode=require';
    const c = loadConfig({ ...base, DATABASE_URL: pooled, DATABASE_URL_UNPOOLED: direct });
    expect(c.databaseUrl).toBe(pooled);
    expect(c.databaseUrlUnpooled).toBe(direct);
  });

  it('refuses MARDEN_AUTH_MODE=dev when NODE_ENV=production', () => {
    expect(() => loadConfig({ DATABASE_URL, NODE_ENV: 'production', MARDEN_AUTH_MODE: 'dev' })).toThrow(/refused/);
  });

  it('refuses dev auth for any NODE_ENV but development or test, and on Railway', () => {
    for (const NODE_ENV of ['staging', 'Production', 'prod', '']) {
      expect(() => loadConfig({ DATABASE_URL, NODE_ENV, MARDEN_AUTH_MODE: 'dev' }), NODE_ENV).toThrow(/refused/);
    }
    expect(() => loadConfig({ DATABASE_URL, MARDEN_AUTH_MODE: 'dev', RAILWAY_ENVIRONMENT_NAME: 'staging' })).toThrow(/deployed/);
    expect(() => loadConfig({ DATABASE_URL, NODE_ENV: 'test', MARDEN_AUTH_MODE: 'dev', RAILWAY_PROJECT_ID: 'p1' })).toThrow(/deployed/);
  });

  it('buildApp refuses a dev-mode config in production even when the config object says otherwise', async () => {
    const config = loadConfig({ DATABASE_URL, NODE_ENV: 'test', MARDEN_AUTH_MODE: 'dev', LOG_LEVEL: 'silent' });
    await expect(buildApp({ config: { ...config, nodeEnv: 'production' } })).rejects.toThrow(/refused/);
    process.env.NODE_ENV = 'production';
    await expect(buildApp({ config })).rejects.toThrow(/refused/);
    process.env.NODE_ENV = 'test';
    process.env.RAILWAY_ENVIRONMENT = 'production';
    await expect(buildApp({ config })).rejects.toThrow(/deployed/);
  });

  it('refuses production without CLERK_SECRET_KEY and CLERK_PUBLISHABLE_KEY', () => {
    expect(() => loadConfig({ DATABASE_URL, NODE_ENV: 'production', CLERK_JWT_KEY: 'pem' })).toThrow(/CLERK_SECRET_KEY/);
  });

  it('refuses Clerk mode with no way to verify tokens', () => {
    expect(() => loadConfig({ DATABASE_URL })).toThrow(/CLERK_SECRET_KEY or CLERK_JWT_KEY/);
  });

  it('allows dev mode outside production and reads the defaults', () => {
    const c = loadConfig({ DATABASE_URL, MARDEN_AUTH_MODE: 'dev' });
    expect(c).toMatchObject({ authMode: 'dev', webOrigins: ['http://localhost:3000'], heartbeatMs: 25_000, trustProxy: false });
  });

  it('derives the token issuer from the publishable key', () => {
    const key = `pk_test_${Buffer.from('example.clerk.accounts.dev$').toString('base64')}`;
    expect(issuerFromPublishableKey(key)).toBe('https://example.clerk.accounts.dev');
    expect(() => issuerFromPublishableKey('not-a-key')).toThrow();
  });
});
