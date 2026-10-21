// Effective permissions (docs/access-control.md): the org-level role's permissions, plus the union over
// the member's roles on the firm's live products of that product's matrix for the role, each limited by
// the plan gates in the route matrix for that product's plan. Staff add STAFF_PERMISSIONS, which no
// product or plan limits, and are not plan-limited on what their product roles grant.
import type { Plan, Role } from '../contracts/app.js';
import {
  ORG_PERMISSIONS, ORG_ROLE_PERMISSIONS, plansFor, PRODUCT_PERMISSIONS, STAFF_PERMISSIONS,
  type Permission, type RouteRule,
} from './permissions.js';
import { matrixFor, type ProductRegistry } from './products/index.js';

/** The member's role on one product the org holds live (status active, within dates, product active). */
export interface ProductGrant { productId: string; role: Role; plan: Plan }

export interface Holder {
  orgRole: Role;
  staff: boolean;
  grants: readonly ProductGrant[];
}

export type Denial = 'role' | 'plan' | 'no_product';
export type Decision = { ok: true } | { ok: false; reason: Denial };

const OK: Decision = { ok: true };
const deny = (reason: Denial): Decision => ({ ok: false, reason });

function grantsWith(permission: Permission, who: Holder, products: ProductRegistry): ProductGrant[] {
  return who.grants.filter((g) => matrixFor(products, g.productId)?.[g.role].some((p) => p === permission) ?? false);
}

/**
 * Org permissions follow the org role. Product permissions need a live product role that grants them
 * (none at all: 'no_product') under a plan the route allows ('plan'). Staff permissions need staff.
 */
export function decide(rule: RouteRule, who: Holder, products: ProductRegistry): Decision {
  const p = rule.permission;
  if (who.staff && STAFF_PERMISSIONS.includes(p)) return OK;
  if (ORG_PERMISSIONS.has(p)) return ORG_ROLE_PERMISSIONS[who.orgRole].some((x) => x === p) ? OK : deny('role');
  if (!PRODUCT_PERMISSIONS.has(p)) return deny('role');
  if (who.grants.length === 0) return deny('no_product');
  const granting = grantsWith(p, who, products);
  if (granting.length === 0) return deny('role');
  if (who.staff || granting.some((g) => rule.plans.includes(g.plan))) return OK;
  return deny('plan');
}

/** Every permission the holder has, sorted: what GET /api/me reports and the web app gates on. */
export function effectivePermissions(who: Holder, products: ProductRegistry): Permission[] {
  const set = new Set<Permission>(ORG_ROLE_PERMISSIONS[who.orgRole]);
  for (const g of who.grants) {
    for (const p of matrixFor(products, g.productId)?.[g.role] ?? []) {
      if (who.staff || plansFor(p).includes(g.plan)) set.add(p);
    }
  }
  if (who.staff) for (const p of STAFF_PERMISSIONS) set.add(p);
  return [...set].sort();
}
