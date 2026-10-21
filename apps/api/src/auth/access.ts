// Spec 23 steps 2-3 with per-product access (docs/access-control.md): resolve the active organization,
// load the org-level membership role and the member's roles on the org's live products from Marden's
// tables. Clerk is identity only. Staff comes from app_user.is_staff, never from Clerk metadata.
import type { Role } from '../contracts/app.js';
import type { Db, Queryable } from '../db.js';
import { forbidden } from '../errors.js';
import { LIVE_GRANTS_SQL } from '../queries/products.js';
import type { Identity } from './clerk.js';
import type { Holder, ProductGrant } from './grants.js';

export interface AuthContext extends Holder {
  userId: string;
  orgId: string;
  orgRole: Role;                    // membership.role: org administration only
  grants: ProductGrant[];           // roles on the org's live products: what the member may use
  staff: boolean;
  email: string;
  sessionId: string | null;
}

export const DEV_IDENTITY: Identity = { userId: 'dev-user', orgId: 'dev', sessionId: null, mfa: true };
const STAFF_DOMAIN = '@marden.ai';

interface AccessRow { role: Role; is_staff: boolean; email: string; grants: ProductGrant[] }

async function accessRow(c: Queryable, orgId: string, userId: string): Promise<AccessRow | null> {
  const res = await c.query<AccessRow>(
    `SELECT m.role, u.is_staff, u.email, ${LIVE_GRANTS_SQL} AS grants
     FROM membership m JOIN app_user u ON u.id = m.user_id
     WHERE m.org_id = $1 AND m.user_id = $2`,
    [orgId, userId],
  );
  return res.rows[0] ?? null;
}

/**
 * 403 when there is no active org, the org is unknown or the user is not a member. A member whose org
 * has no live product (or who holds no role on one) still resolves: /api/me answers, data routes 403.
 * Staff needs is_staff plus (outside dev mode) a @marden.ai address and a verified second factor.
 */
export async function resolveAccess(db: Db, identity: Identity, devMode: boolean): Promise<AuthContext> {
  if (!identity.orgId) throw forbidden('no_active_org');
  const row = await accessRow(db, identity.orgId, identity.userId);
  if (!row) throw forbidden('not_a_member');
  const staffEligible = devMode || (row.email.toLowerCase().endsWith(STAFF_DOMAIN) && identity.mfa);
  return {
    userId: identity.userId,
    orgId: identity.orgId,
    orgRole: row.role,
    grants: row.grants,
    staff: row.is_staff && staffEligible,
    email: row.email,
    sessionId: identity.sessionId,
  };
}

/**
 * The member's current access, for re-checks of a long-lived session (the SSE stream): null once the
 * membership is gone. Staff stays staff only while app_user.is_staff still holds.
 */
export async function currentHolder(db: Db, who: { orgId: string; userId: string; staff: boolean }): Promise<Holder | null> {
  const row = await accessRow(db, who.orgId, who.userId);
  return row ? { orgRole: row.role, grants: row.grants, staff: who.staff && row.is_staff } : null;
}

/**
 * Dev mode only: make sure the fixed local owner + staff user exists in org `dev`, and that org `dev`
 * holds product 'marden' (plan paid, unlimited seats) with the dev user as its owner.
 */
export async function ensureDevIdentity(db: Db): Promise<void> {
  await db.tx(async (c) => {
    await c.query(`INSERT INTO org (id, name) VALUES ('dev', 'Local development') ON CONFLICT (id) DO NOTHING`);
    await c.query(
      `INSERT INTO app_user (id, email, name, is_staff) VALUES ('dev-user', 'dev-user@localhost', 'Local developer', true)
       ON CONFLICT (id) DO UPDATE SET is_staff = true`,
    );
    await c.query(
      `INSERT INTO membership (org_id, user_id, role) VALUES ('dev', 'dev-user', 'owner')
       ON CONFLICT (org_id, user_id) DO UPDATE SET role = 'owner'`,
    );
    await c.query(
      `INSERT INTO org_product (org_id, product_id, plan, seats, status, starts_on)
       VALUES ('dev', 'marden', 'paid', NULL, 'active', (now() AT TIME ZONE 'UTC')::date)
       ON CONFLICT (org_id, product_id) DO UPDATE SET plan = 'paid', seats = NULL, status = 'active', ends_on = NULL,
         starts_on = LEAST(org_product.starts_on, EXCLUDED.starts_on)`,
    );
    await c.query(
      `INSERT INTO member_product_role (org_id, user_id, product_id, role) VALUES ('dev', 'dev-user', 'marden', 'owner')
       ON CONFLICT (org_id, user_id, product_id) DO UPDATE SET role = 'owner'`,
    );
  });
}
