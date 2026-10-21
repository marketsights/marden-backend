// Spec 23 steps 2-3: resolve the active organization and load the membership role from Marden's
// tables (kept current by Clerk webhooks). Staff comes from app_user.is_staff, never from Clerk metadata.
import type { Plan, Role } from '../contracts/app.js';
import type { Db } from '../db.js';
import { forbidden } from '../errors.js';
import type { Identity } from './clerk.js';

export interface AuthContext {
  userId: string;
  orgId: string;
  role: Role;
  plan: Plan;
  staff: boolean;
  email: string;
  sessionId: string | null;
}

export const DEV_IDENTITY: Identity = { userId: 'dev-user', orgId: 'dev', sessionId: null, mfa: true };
const STAFF_DOMAIN = '@marden.ai';

interface AccessRow { role: Role; plan: Plan; is_staff: boolean; email: string }

/**
 * 403 when there is no active org, the org is unknown or the user is not a member.
 * Staff needs is_staff plus (outside dev mode) a @marden.ai address and a verified second factor.
 */
export async function resolveAccess(db: Db, identity: Identity, devMode: boolean): Promise<AuthContext> {
  if (!identity.orgId) throw forbidden('no_active_org');
  const res = await db.query<AccessRow>(
    `SELECT m.role, o.plan, u.is_staff, u.email
     FROM membership m JOIN org o ON o.id = m.org_id JOIN app_user u ON u.id = m.user_id
     WHERE m.org_id = $1 AND m.user_id = $2`,
    [identity.orgId, identity.userId],
  );
  const row = res.rows[0];
  if (!row) throw forbidden('not_a_member');
  const staffEligible = devMode || (row.email.toLowerCase().endsWith(STAFF_DOMAIN) && identity.mfa);
  return {
    userId: identity.userId,
    orgId: identity.orgId,
    role: row.role,
    plan: row.plan,
    staff: row.is_staff && staffEligible,
    email: row.email,
    sessionId: identity.sessionId,
  };
}

/** Dev mode only: make sure the fixed local owner + staff user exists in org `dev`. */
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
  });
}
