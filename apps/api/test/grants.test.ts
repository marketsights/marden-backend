// Effective permissions with several products (docs/access-control.md), as pure functions: a
// permission is granted if any live product role grants it under a plan its routes allow; a product
// without a matrix in code grants nothing; staff keep their superset.
import { describe, expect, it } from 'vitest';
import { decide, effectivePermissions, type Holder } from '../src/auth/grants.js';
import { ORG_ROLE_PERMISSIONS, ROUTE_RULES } from '../src/auth/permissions.js';
import { PRODUCTS, type ProductRegistry } from '../src/auth/products/index.js';
import { MARDEN } from '../src/auth/products/marden.js';

const REGISTRY: ProductRegistry = {
  marden: MARDEN,
  pricing: { viewer: ['instruments:read'], member: ['instruments:read', 'export:xlsx'], admin: ['instruments:read'], owner: ['instruments:read'] },
};
const rule = (key: string) => ROUTE_RULES[key]!;
const holder = (grants: Holder['grants'], extra: Partial<Holder> = {}): Holder => ({ orgRole: 'member', staff: false, grants, ...extra });

describe('effective permissions across products', () => {
  it('ships product marden with the V1 matrix', () => {
    expect(Object.keys(PRODUCTS)).toEqual(['marden']);
    expect(MARDEN.viewer).not.toContain('export:xlsx');
    expect(MARDEN.member).toEqual(expect.arrayContaining(['export:xlsx', 'stress:run', 'watchlist:write', 'prefs:write']));
  });

  it("grants only product B's matrix to a member with a role only on B", () => {
    const onlyB = holder([{ productId: 'pricing', role: 'member', plan: 'paid' }]);
    expect(effectivePermissions(onlyB, REGISTRY)).toEqual([...ORG_ROLE_PERMISSIONS.member, 'export:xlsx', 'instruments:read'].sort());
    expect(decide(rule('GET /api/instruments'), onlyB, REGISTRY)).toEqual({ ok: true });
    expect(decide(rule('GET /api/sites'), onlyB, REGISTRY)).toEqual({ ok: false, reason: 'role' });
    // Same role name, different product: B's admin cannot export, marden's admin can.
    const adminB = holder([{ productId: 'pricing', role: 'admin', plan: 'paid' }]);
    expect(decide(rule('GET /api/export/:table.xlsx'), adminB, REGISTRY)).toEqual({ ok: false, reason: 'role' });
  });

  it('takes the union of roles, each under its own product plan', () => {
    const both = holder([
      { productId: 'marden', role: 'viewer', plan: 'paid' },
      { productId: 'pricing', role: 'member', plan: 'trial' },
    ]);
    const perms = effectivePermissions(both, REGISTRY);
    expect(perms).toContain('sites:read');
    expect(perms).not.toContain('export:xlsx'); // pricing grants it, but on trial; marden viewer does not
    expect(decide(rule('GET /api/export/:table.xlsx'), both, REGISTRY)).toEqual({ ok: false, reason: 'plan' });
    const paidB = holder([{ productId: 'marden', role: 'viewer', plan: 'trial' }, { productId: 'pricing', role: 'member', plan: 'paid' }]);
    expect(decide(rule('GET /api/export/:table.xlsx'), paidB, REGISTRY)).toEqual({ ok: true });
  });

  it('grants nothing for a product without a matrix, and no_product without any live role', () => {
    const unknown = holder([{ productId: 'not_in_code', role: 'owner', plan: 'paid' }]);
    expect(effectivePermissions(unknown, REGISTRY)).toEqual([...ORG_ROLE_PERMISSIONS.member].sort());
    expect(decide(rule('GET /api/sites'), unknown, REGISTRY)).toEqual({ ok: false, reason: 'role' });
    expect(decide(rule('GET /api/sites'), holder([]), REGISTRY)).toEqual({ ok: false, reason: 'no_product' });
  });

  it('keeps the staff superset: every product read, review, ops and product management', () => {
    const staff = holder([], { staff: true });
    const perms = effectivePermissions(staff, REGISTRY);
    expect(perms).toEqual(expect.arrayContaining(['sites:read', 'instruments:read', 'ops:read', 'review:approve', 'products:manage']));
    expect(perms).not.toContain('watchlist:write');
    expect(decide(rule('PATCH /api/admin/orgs/:orgId/products/:productId'), staff, REGISTRY)).toEqual({ ok: true });
    expect(decide(rule('PATCH /api/admin/orgs/:orgId/products/:productId'), holder([]), REGISTRY)).toEqual({ ok: false, reason: 'role' });
  });
});
