// The permission matrix covers every endpoint in handoff/api/endpoints.md and every registered route,
// startup refuses an unmapped route, and org role x product role x plan decisions follow the matrix
// (spec 23, docs/access-control.md).
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { decide, effectivePermissions, type Holder } from '../src/auth/grants.js';
import {
  ORG_PERMISSIONS, ORG_ROLE_PERMISSIONS, PLANS, PRODUCT_PERMISSIONS, PUBLIC_PERMISSIONS, ROLES, ROUTE_RULES,
  STAFF_PERMISSIONS, unmappedRoutes, type Permission,
} from '../src/auth/permissions.js';
import { PRODUCTS } from '../src/auth/products/index.js';
import type { Plan, Role } from '../src/contracts/app.js';
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

describe('roles and plans (spec 23, per product)', () => {
  const rule = (key: string) => ROUTE_RULES[key]!;
  /** One live 'marden' role; the org role is the same unless given. */
  const holder = (role: Role, plan: Plan, extra: Partial<Holder> = {}): Holder =>
    ({ orgRole: role, staff: false, grants: [{ productId: 'marden', role, plan }], ...extra });
  const outcome = (key: string, who: Holder): string => {
    const d = decide(rule(key), who, PRODUCTS);
    return d.ok ? 'ok' : d.reason;
  };

  it("has no Clerk role mapping: org roles are Marden's (docs/access-control.md)", async () => {
    const mod: Record<string, unknown> = await import('../src/auth/permissions.js');
    expect(Object.keys(mod).filter((k) => /clerk/i.test(k))).toEqual([]);
  });

  it('classifies every route permission as org, product, staff or public', () => {
    const staffOnly = STAFF_PERMISSIONS.filter((p) => !PRODUCT_PERMISSIONS.has(p) && !ORG_PERMISSIONS.has(p));
    for (const [key, r] of Object.entries(ROUTE_RULES)) {
      const kinds = [ORG_PERMISSIONS.has(r.permission), PRODUCT_PERMISSIONS.has(r.permission),
        staffOnly.includes(r.permission), (PUBLIC_PERMISSIONS as readonly Permission[]).includes(r.permission)];
      expect(kinds.filter(Boolean).length, key).toBe(1);
      if (!PRODUCT_PERMISSIONS.has(r.permission) && r.scope !== 'public') expect(r.plans, key).toEqual(PLANS);
    }
    expect(rule('POST /api/admin/orgs/:orgId/products').scope).toBe('staff');
    expect(rule('PATCH /api/account/members/:id/products').permission).toBe('members:manage');
  });

  it('viewer is read only: no exports, stress runs or watchlist edits', () => {
    const viewer = holder('viewer', 'paid');
    expect(outcome('GET /api/completion', viewer)).toBe('ok');
    expect(outcome('GET /api/export/:table.xlsx', viewer)).toBe('role');
    expect(outcome('POST /api/stress/run', viewer)).toBe('role');
    expect(outcome('POST /api/watchlist/items', viewer)).toBe('role');
  });

  it('team management follows the org role, product use follows the product role', () => {
    const member = holder('member', 'paid');
    expect(outcome('POST /api/watchlist/items', member)).toBe('ok');
    expect(outcome('PATCH /api/account/members/:id', member)).toBe('role');
    expect(outcome('PATCH /api/account/members/:id/products', member)).toBe('role');
    // An org admin who is only a viewer on the product manages the team but cannot edit watchlists.
    const adminViewer = holder('viewer', 'paid', { orgRole: 'admin' });
    expect(outcome('PATCH /api/account/members/:id', adminViewer)).toBe('ok');
    expect(outcome('PATCH /api/account/members/:id/products', adminViewer)).toBe('ok');
    expect(outcome('POST /api/watchlist/items', adminViewer)).toBe('role');
    // Product owner with org role member: no team management.
    expect(outcome('POST /api/account/invites', holder('owner', 'paid', { orgRole: 'member' }))).toBe('role');
  });

  it('trial plans cannot export; staff are not plan-limited', () => {
    const trialOwner = holder('owner', 'trial');
    expect(outcome('GET /api/export/:table.xlsx', trialOwner)).toBe('plan');
    expect(outcome('GET /api/export/:table.xlsx', { ...trialOwner, staff: true })).toBe('ok');
    expect(effectivePermissions(trialOwner, PRODUCTS)).not.toContain('export:xlsx');
    expect(effectivePermissions({ ...trialOwner, staff: true }, PRODUCTS)).toContain('export:xlsx');
  });

  it('no live product role: data routes answer no_product, org routes and /api/me still work', () => {
    const none: Holder = { orgRole: 'owner', staff: false, grants: [] };
    expect(outcome('GET /api/sites', none)).toBe('no_product');
    expect(outcome('POST /api/watchlist/items', none)).toBe('no_product');
    expect(outcome('GET /api/me', none)).toBe('ok');
    expect(outcome('GET /api/account', none)).toBe('ok');
    expect(outcome('POST /api/admin/orgs/:orgId/products', none)).toBe('role');
    expect(effectivePermissions(none, PRODUCTS)).toEqual([...ORG_ROLE_PERMISSIONS.owner].sort());
    // Staff keep their read superset without a product, but writing still needs a product role.
    expect(outcome('GET /api/sites', { ...none, staff: true })).toBe('ok');
    expect(outcome('POST /api/watchlist/items', { ...none, staff: true })).toBe('no_product');
    expect(outcome('POST /api/admin/orgs/:orgId/products', { ...none, staff: true })).toBe('ok');
  });

  it('every org role x product role x plan outcome follows the matrix, and /api/me agrees', () => {
    const marden = PRODUCTS.marden!;
    const combos = ROLES.flatMap((orgRole) => ROLES.flatMap((role) => PLANS.map((plan) => ({ orgRole, role, plan }))));
    const routes = Object.entries(ROUTE_RULES).filter(([, r]) => r.scope !== 'public');
    for (const { orgRole, role, plan } of combos) {
      const who = holder(role, plan, { orgRole });
      const perms = effectivePermissions(who, PRODUCTS);
      for (const [key, r] of routes) {
        const p = r.permission;
        const expected = ORG_PERMISSIONS.has(p)
          ? (ORG_ROLE_PERMISSIONS[orgRole].some((x) => x === p) ? 'ok' : 'role')
          : !marden[role].some((x) => x === p) ? 'role' : !r.plans.includes(plan) ? 'plan' : 'ok';
        const got = outcome(key, who);
        expect(got, `${key} org:${orgRole} product:${role} ${plan}`).toBe(expected);
        expect(perms.includes(p), `/api/me ${key} org:${orgRole} product:${role} ${plan}`).toBe(got === 'ok');
      }
    }
    expect(effectivePermissions(holder('viewer', 'paid', { staff: true }), PRODUCTS)).toContain('ops:read');
  });
});
