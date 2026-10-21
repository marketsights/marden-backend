// Production behaviour that the deploy relies on (docs/DEPLOY.md): /healthz reports database
// reachability (503 when it is gone), TRUST_PROXY as a hop count keys rate limits on the address
// Railway's edge appended (a client cannot pick its own key), and SIGTERM drains then exits.
import { readFileSync } from 'node:fs';
import Fastify from 'fastify';
import { describe, expect, it } from 'vitest';
import { loadConfig } from '../src/config.js';
import { checkHealth } from '../src/lib/health.js';
import { IPV4_ANY, listenWithFallback } from '../src/listen.js';
import { shutdownHandler } from '../src/shutdown.js';
import { makeApp, testDb } from './helpers.js';

const DATABASE_URL = 'postgres://marden:marden@localhost:5432/marden';

describe('GET /healthz', () => {
  it('answers 200 while the database answers and 503 once it is gone, with no detail', async () => {
    const db = testDb();
    const app = await makeApp({ mode: 'dev', db });
    try {
      const up = await app.inject({ method: 'GET', url: '/healthz' });
      expect(up.statusCode).toBe(200);
      expect(up.json()).toMatchObject({ ok: true, db: 'ok' });
      expect(up.headers['cache-control']).toBe('no-store');

      await db.close(); // the pool is gone: every query now fails, as with a lost database
      const down = await app.inject({ method: 'GET', url: '/healthz' });
      expect(down.statusCode).toBe(503);
      expect(down.json()).toEqual({ ok: false, db: 'unreachable', commit: null });
    } finally {
      await app.close();
    }
  });

  it('reports an unreachable server and a timeout as unreachable, and the deployed commit', async () => {
    const { createDb } = await import('../src/db.js');
    const nowhere = createDb('postgres://marden:marden@127.0.0.1:1/marden', 1);
    const errors: unknown[] = [];
    try {
      const h = await checkHealth(nowhere, { env: { RAILWAY_GIT_COMMIT_SHA: 'abcdef1234567890' }, onError: (e) => errors.push(e) });
      expect(h).toEqual({ ok: false, db: 'unreachable', commit: 'abcdef123456' });
      expect(errors).toHaveLength(1);
    } finally {
      await nowhere.close();
    }
    const hanging = { query: () => new Promise<never>(() => undefined) };
    const slow = await checkHealth(hanging, { env: {}, timeoutMs: 50 });
    expect(slow).toEqual({ ok: false, db: 'unreachable', commit: null });
  });
});

describe('TRUST_PROXY', () => {
  it('accepts true, false or a hop count and nothing else', () => {
    const base = { DATABASE_URL, NODE_ENV: 'test', MARDEN_AUTH_MODE: 'dev' };
    expect(loadConfig({ ...base }).trustProxy).toBe(false);
    expect(loadConfig({ ...base, TRUST_PROXY: 'true' }).trustProxy).toBe(true);
    expect(loadConfig({ ...base, TRUST_PROXY: '0' }).trustProxy).toBe(false);
    expect(loadConfig({ ...base, TRUST_PROXY: '1' }).trustProxy).toBe(1);
    expect(() => loadConfig({ ...base, TRUST_PROXY: 'yes' })).toThrow(/TRUST_PROXY/);
    expect(loadConfig({ ...base }).shutdownTimeoutMs).toBe(20_000);
  });

  it('with one trusted hop, a spoofed X-Forwarded-For does not get a client a fresh rate-limit key', async () => {
    const db = testDb();
    const hops = await makeApp({ mode: 'dev', db, env: { TRUST_PROXY: '1', RATE_LIMIT_PER_MINUTE: '2' } });
    const all = await makeApp({ mode: 'dev', db, env: { TRUST_PROXY: 'true', RATE_LIMIT_PER_MINUTE: '2' } });
    const hit = (app: typeof hops, spoof: string) =>
      app.inject({ method: 'GET', url: '/healthz', headers: { 'x-forwarded-for': `${spoof}, 203.0.113.7` } });
    try {
      for (const app of [hops, all]) {
        expect((await hit(app, '198.51.100.1')).statusCode).toBe(200);
        expect((await hit(app, '198.51.100.1')).statusCode).toBe(200);
        expect((await hit(app, '198.51.100.1')).statusCode).toBe(429);
      }
      // Changing the left-most (client-written) entry: one hop still sees 203.0.113.7; "trust all" is fooled.
      expect((await hit(hops, '198.51.100.99')).statusCode).toBe(429);
      expect((await hit(all, '198.51.100.99')).statusCode).toBe(200);
    } finally {
      await hops.close();
      await all.close();
      await db.close();
    }
  });
});

describe('graceful shutdown', () => {
  it('lets an in-flight request finish, then exits 0', async () => {
    const app = Fastify({ logger: false });
    let release: () => void = () => undefined;
    app.get('/slow', async () => {
      await new Promise<void>((resolve) => { release = resolve; });
      return { done: true };
    });
    await app.listen({ host: '127.0.0.1', port: 0 });
    const address = app.server.address();
    const port = typeof address === 'object' && address ? address.port : 0;
    const inFlight = fetch(`http://127.0.0.1:${port}/slow`).then((r) => r.json());
    await new Promise((r) => setTimeout(r, 50));
    const exits: number[] = [];
    const stopping = shutdownHandler(app, { timeoutMs: 5_000, exit: (c) => exits.push(c) })('SIGTERM');
    await new Promise((r) => setTimeout(r, 50));
    expect(exits).toEqual([]); // still draining
    release();
    expect(await inFlight).toEqual({ done: true });
    await stopping;
    expect(exits).toEqual([0]);
  });

  it('exits 1 when closing takes longer than the timeout', async () => {
    const app = Fastify({ logger: false });
    app.addHook('onClose', () => new Promise<void>(() => undefined)); // never finishes
    await app.ready();
    const exits: number[] = [];
    const handler = shutdownHandler(app, { timeoutMs: 100, exit: (c) => exits.push(c) });
    void handler('SIGTERM');
    void handler('SIGTERM'); // a second signal does not start a second shutdown
    await new Promise((r) => setTimeout(r, 300));
    expect(exits).toEqual([1]);
  });
});

describe('railway.json (config as code)', () => {
  const railway = JSON.parse(readFileSync(new URL('../railway.json', import.meta.url), 'utf8')) as {
    build: { builder: string; dockerfilePath: string; watchPatterns: string[] };
    deploy: Record<string, unknown>;
  };
  const dockerfile = readFileSync(new URL('../Dockerfile', import.meta.url), 'utf8');

  it('builds this Dockerfile, migrates before deploy and checks /healthz (tested above)', () => {
    expect(railway.build).toMatchObject({ builder: 'DOCKERFILE', dockerfilePath: 'apps/api/Dockerfile' });
    expect(railway.build.watchPatterns).toEqual(expect.arrayContaining(['apps/api/**', 'db/migrations/**']));
    expect(railway.deploy.preDeployCommand).toEqual(['marden-migrate']);
    expect(dockerfile).toContain('scripts/migrate.sh /usr/local/bin/marden-migrate');
    expect(railway.deploy.healthcheckPath).toBe('/healthz');
    // The start command is the image's CMD, so `docker run` and Railway run the same process.
    expect(dockerfile).toContain(`CMD ["${String(railway.deploy.startCommand).split(' ').join('", "')}"]`);
    expect(dockerfile).toMatch(/^USER node$/m);
  });

  it('gives a stopping API longer than its own drain timeout before Railway kills it', () => {
    const config = loadConfig({ DATABASE_URL, NODE_ENV: 'test', MARDEN_AUTH_MODE: 'dev' });
    expect(Number(railway.deploy.drainingSeconds) * 1000).toBeGreaterThan(config.shutdownTimeoutMs);
  });

  it('refuses dev auth on Railway or Render even when NODE_ENV says development', () => {
    for (const marker of ['RAILWAY_ENVIRONMENT', 'RAILWAY_ENVIRONMENT_NAME', 'RAILWAY_ENVIRONMENT_ID', 'RAILWAY_PROJECT_ID', 'RENDER', 'RENDER_SERVICE_ID']) {
      expect(() => loadConfig({ DATABASE_URL, NODE_ENV: 'development', MARDEN_AUTH_MODE: 'dev', [marker]: 'x' }))
        .toThrow(new RegExp(`${marker} is set`));
    }
  });
});

describe('listenWithFallback (HOST=:: in the image)', () => {
  const errno = (code: string) => Object.assign(new Error(code), { code });

  it('binds :: where IPv6 exists, else 0.0.0.0, and the API answers either way', async () => {
    const app = Fastify({ logger: false });
    app.get('/ping', () => Promise.resolve({ ok: true }));
    try {
      const host = await listenWithFallback(app, { host: '::', port: 0 });
      expect(['::', IPV4_ANY]).toContain(host); // this sandbox has no IPv6, so it exercises the fallback
      const address = app.server.address();
      const port = typeof address === 'object' && address ? address.port : 0;
      expect(await (await fetch(`http://127.0.0.1:${port}/ping`)).json()).toEqual({ ok: true });
    } finally {
      await app.close();
    }
  });

  it('falls back only for EAFNOSUPPORT on ::, and rethrows anything else', async () => {
    const app = Fastify({ logger: false });
    const calls: string[] = [];
    const noV6 = (t: { host: string }) => {
      calls.push(t.host);
      return t.host === '::' ? Promise.reject(errno('EAFNOSUPPORT')) : Promise.resolve();
    };
    expect(await listenWithFallback(app, { host: '::', port: 1 }, noV6)).toBe(IPV4_ANY);
    expect(calls).toEqual(['::', IPV4_ANY]);
    await expect(listenWithFallback(app, { host: '::', port: 1 }, () => Promise.reject(errno('EADDRINUSE'))))
      .rejects.toThrow('EADDRINUSE');
    await expect(listenWithFallback(app, { host: '10.0.0.1', port: 1 }, () => Promise.reject(errno('EAFNOSUPPORT'))))
      .rejects.toThrow('EAFNOSUPPORT');
    await app.close();
  });
});
