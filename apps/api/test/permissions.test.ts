// The permission matrix covers every endpoint in handoff/api/endpoints.md and every registered route,
// startup refuses an unmapped route, and role x plan decisions match spec 23.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import {
  decide, permissionsFor, PLANS, roleFromClerk, ROLE_PERMISSIONS, ROLES, ROUTE_RULES, unmappedRoutes,
} from '../src/auth/permissions.js';
import type { Db } from '../src/db.js';
import { makeApp, testDb } from './helpers.js';

const root = (p: string): string => fileURLToPath(new URL(`../../../${p}`, import.meta.url));

/** "GET / PUT" + "`/a`, `/b`": one path -> every method; equal counts -> pairwise; `{x,y}` expands. */
export function endpointsFromDoc(markdown: string): string[] {
  const out = new Set<string>();
  for (const line of markdown.split('\n')) {
    const cells = line.split('|').map((c) => c.trim());
    const methods = (cells[1] ?? '').split('/').map((m) => m.trim()).filter((m) => /^[A-Z]+$/.test(m));
    const paths = [...(cells[2] ?? '').matchAll(/`([^`]+)`/g)].map((m) => (m[1] ?? '').split('?')[0] ?? '');
    if (methods.length === 0 || paths.length === 0) continue;
    const pairs: [string, string][] = paths.length === 1
      ? methods.map((m) => [m, paths[0] ?? ''])
      : methods.length === 1
        ? paths.map((p) => [methods[0] ?? '', p])
        : methods.map((m, i) => [m, paths[i] ?? '']);
    for (const [method, path] of pairs) {
      const brace = /\{([^}]+)\}/.exec(path);
      const expanded = brace ? (brace[1] ?? '').split(',').map((part) => path.replace(brace[0], part)) : [path];
      for (const p of expanded) out.add(`${method} ${p}`);
    }
  }
  return [...out].sort();
}

describe('permission matrix', () => {
  let db: Db;
  let app: FastifyInstance;
  const documented = endpointsFromDoc(readFileSync(root('handoff/api/endpoints.md'), 'utf8'));

  beforeAll(async () => {
    db = testDb();
    app = await makeApp({ mode: 'dev', db });
  });
  afterAll(async () => {
    await app.close();
    await db.close();
  });

  it('parses every endpoint from endpoints.md', () => {
    expect(documented.length).toBeGreaterThan(55);
    expect(documented).toContain('GET /api/sites/:slug/lease');
    expect(documented).toContain('DELETE /api/watchlist/items/:id');
    expect(documented).toContain('GET /api/export/:table.xlsx');
  });

  it('maps every documented endpoint to a rule', () => {
    expect(documented.filter((e) => !(e in ROUTE_RULES))).toEqual([]);
  });

  it('registers a route for every documented endpoint', () => {
    expect(documented.filter((e) => !app.registeredRoutes.has(e))).toEqual([]);
  });

  it('has a rule for every registered route and no rule without a route', () => {
    expect(unmappedRoutes(app.registeredRoutes)).toEqual([]);
    expect(Object.keys(ROUTE_RULES).filter((k) => !app.registeredRoutes.has(k))).toEqual([]);
  });

  it('refuses to start when a route has no permission (default deny)', async () => {
    await expect(
      makeApp({ mode: 'dev', db, extraRoutes: (a) => a.get('/api/unmapped', (_req, reply) => reply.send({ ok: true })) }),
    ).rejects.toThrow(/without a permission rule.*GET \/api\/unmapped/);
  });

  it('copies the response contract verbatim', () => {
    const handoff = readFileSync(root('handoff/api/types.ts'), 'utf8');
    const copy = readFileSync(fileURLToPath(new URL('../src/contracts/types.ts', import.meta.url)), 'utf8');
    expect(copy).toBe(handoff);
  });
});

describe('roles and plans (spec 23)', () => {
  const rule = (key: string) => ROUTE_RULES[key]!;

  it('maps Clerk roles and gives unknown roles no access', () => {
    expect(roleFromClerk('org:owner')).toBe('owner');
    expect(roleFromClerk('org:admin')).toBe('admin');
    expect(roleFromClerk('org:member')).toBe('member');
    expect(roleFromClerk('org:viewer')).toBe('viewer');
    expect(roleFromClerk('org:billing')).toBeNull();
    expect(roleFromClerk(undefined)).toBeNull();
  });

  it('viewer is read only: no exports, stress runs or watchlist edits', () => {
    const viewer = { role: 'viewer' as const, staff: false, plan: 'paid' as const };
    expect(decide(rule('GET /api/completion'), viewer)).toEqual({ ok: true });
    expect(decide(rule('GET /api/export/:table.xlsx'), viewer)).toEqual({ ok: false, reason: 'role' });
    expect(decide(rule('POST /api/stress/run'), viewer)).toEqual({ ok: false, reason: 'role' });
    expect(decide(rule('POST /api/watchlist/items'), viewer)).toEqual({ ok: false, reason: 'role' });
  });

  it('member uses the plan; only owners and admins manage the team', () => {
    const member = { role: 'member' as const, staff: false, plan: 'paid' as const };
    expect(decide(rule('POST /api/watchlist/items'), member)).toEqual({ ok: true });
    expect(decide(rule('PATCH /api/account/members/:id'), member)).toEqual({ ok: false, reason: 'role' });
    expect(decide(rule('PATCH /api/account/members/:id'), { ...member, role: 'admin' })).toEqual({ ok: true });
  });

  it('trial plans cannot export; staff are not plan-limited', () => {
    const trialOwner = { role: 'owner' as const, staff: false, plan: 'trial' as const };
    expect(decide(rule('GET /api/export/:table.xlsx'), trialOwner)).toEqual({ ok: false, reason: 'plan' });
    expect(decide(rule('GET /api/export/:table.xlsx'), { ...trialOwner, staff: true })).toEqual({ ok: true });
  });

  it('every role x permission x plan outcome follows the matrix', () => {
    for (const [key, r] of Object.entries(ROUTE_RULES)) {
      if (r.scope === 'public') continue;
      for (const role of ROLES) {
        for (const plan of PLANS) {
          const expected = !ROLE_PERMISSIONS[role].includes(r.permission) ? 'role' : !r.plans.includes(plan) ? 'plan' : 'ok';
          const got = decide(r, { role, staff: false, plan });
          expect(got.ok ? 'ok' : got.reason, `${key} ${role} ${plan}`).toBe(expected);
        }
      }
    }
    expect(permissionsFor('viewer', true)).toContain('ops:read');
  });
});
