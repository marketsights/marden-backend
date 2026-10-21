// Fail closed (spec 23): dev auth is refused in production, production needs Clerk keys.
import { describe, expect, it } from 'vitest';
import { issuerFromPublishableKey, loadConfig } from '../src/config.js';

const DATABASE_URL = 'postgres://marden:marden@localhost:5432/marden';

describe('loadConfig', () => {
  it('refuses MARDEN_AUTH_MODE=dev when NODE_ENV=production', () => {
    expect(() => loadConfig({ DATABASE_URL, NODE_ENV: 'production', MARDEN_AUTH_MODE: 'dev' })).toThrow(/refused/);
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
