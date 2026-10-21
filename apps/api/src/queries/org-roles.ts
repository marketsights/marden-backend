// Marden-owned org roles (docs/access-control.md). membership.role is set by Marden when a membership
// is created and changes only through an org owner or admin (routes/account.ts). Clerk holds identity,
// membership existence and invitations, always with its built-in basic role; Clerk roles are never read.
//   - the first member of an org becomes owner;
//   - a member who joins through a Marden invitation gets the role recorded on it (org_invitation);
//   - anyone else (added in Clerk's dashboard or by the other product on the shared instance) is a viewer.
import type { Role } from '../contracts/app.js';
import type { Queryable } from '../db.js';
import { audit } from './audit.js';

/** Role for a member who joins without a Marden invitation and is not the org's first member. */
export const DEFAULT_JOIN_ROLE: Role = 'viewer';

export type JoinRule = 'first_member' | 'invitation' | 'default';

export interface Joined { role: Role; rule: JoinRule; invitationId: string | null }

/**
 * One org's joins take turns on this lock (also taken by grantFirstMemberOwner), so two members
 * joining at the same moment cannot both count as first.
 */
export async function lockOrgJoins(c: Queryable, orgId: string): Promise<void> {
  await c.query(`SELECT pg_advisory_xact_lock(hashtext($1))`, [`first_member:${orgId}`]);
}

/** The newest open Marden invitation to one of these addresses, valid at `at` (ms), locked. */
async function openInvitation(c: Queryable, orgId: string, emails: string[], at: number): Promise<{ id: string; role: Role } | null> {
  const res = await c.query<{ id: string; role: Role }>(
    `SELECT id, role FROM org_invitation
     WHERE org_id = $1 AND email = ANY($2::text[]) AND status = 'pending'
       AND (expires_at IS NULL OR expires_at >= to_timestamp($3::double precision / 1000))
     ORDER BY created_at DESC, id DESC LIMIT 1 FOR UPDATE`,
    [orgId, emails, at],
  );
  return res.rows[0] ?? null;
}

/**
 * Creates the membership with the role Marden decides; returns null when it already exists (a Clerk
 * update of an existing membership never changes its role). `identifier` is Clerk's identifier for the
 * user (their email); the app_user's stored email is matched too. `at` is the event time in ms.
 */
export async function createMembership(
  c: Queryable,
  m: { orgId: string; userId: string; identifier: string | null; at: number },
): Promise<Joined | null> {
  const exists = await c.query(`SELECT 1 FROM membership WHERE org_id = $1 AND user_id = $2`, [m.orgId, m.userId]);
  if ((exists.rowCount ?? 0) > 0) return null;
  await lockOrgJoins(c, m.orgId);
  const others = await c.query(`SELECT 1 FROM membership WHERE org_id = $1 LIMIT 1`, [m.orgId]);
  const stored = await c.query<{ email: string }>(`SELECT email FROM app_user WHERE id = $1`, [m.userId]);
  const emails = [...new Set([m.identifier, stored.rows[0]?.email].filter((e): e is string => !!e).map((e) => e.toLowerCase()))];
  const invitation = emails.length > 0 ? await openInvitation(c, m.orgId, emails, m.at) : null;
  const first = (others.rowCount ?? 0) === 0;
  const joined: Joined = first
    ? { role: 'owner', rule: 'first_member', invitationId: invitation?.id ?? null }
    : invitation
      ? { role: invitation.role, rule: 'invitation', invitationId: invitation.id }
      : { role: DEFAULT_JOIN_ROLE, rule: 'default', invitationId: null };
  const inserted = await c.query(
    `INSERT INTO membership (org_id, user_id, role, role_source) VALUES ($1, $2, $3, $4) ON CONFLICT (org_id, user_id) DO NOTHING`,
    [m.orgId, m.userId, joined.role, joined.rule],
  );
  if ((inserted.rowCount ?? 0) === 0) return null;
  if (joined.invitationId) {
    await c.query(
      `UPDATE org_invitation SET status = 'accepted', accepted_by = $2, accepted_at = now() WHERE id = $1`,
      [joined.invitationId, m.userId],
    );
  }
  await audit(c, {
    orgId: m.orgId, userId: null, action: 'member.join', target: `user:${m.userId}`,
    meta: { role: joined.role, rule: joined.rule, invitationId: joined.invitationId },
  });
  return joined;
}

export interface InvitationRow {
  id: string;            // Clerk organization invitation id
  orgId: string;
  email: string;
  role: Role;
  invitedBy: string;
  expiresAt: number | null;  // ms
}

/** Records the Marden role of an invitation Clerk just created. */
export async function recordInvitation(c: Queryable, inv: InvitationRow): Promise<void> {
  await c.query(
    `INSERT INTO org_invitation (id, org_id, email, role, invited_by, expires_at)
     VALUES ($1, $2, $3, $4, $5, to_timestamp($6::double precision / 1000))
     ON CONFLICT (id) DO NOTHING`,
    [inv.id, inv.orgId, inv.email.toLowerCase(), inv.role, inv.invitedBy, inv.expiresAt],
  );
}

/** Clerk revoked the invitation: nobody joins through it any more. */
export async function revokeInvitation(c: Queryable, id: string): Promise<void> {
  await c.query(`UPDATE org_invitation SET status = 'revoked' WHERE id = $1 AND status = 'pending'`, [id]);
}
