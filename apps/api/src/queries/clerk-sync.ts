// Applies Clerk webhook events to org / app_user / membership (spec 23 "Sync"). Idempotent by svix
// message id and safe out of order: each applied event is recorded in audit_log with its event time,
// and an event older than the last one applied to the same subject (or older than a deletion of a
// related user or org) is recorded as stale and not applied.
// Roles are Marden's (docs/access-control.md). The Clerk instance is shared with another product, so a
// Clerk organization role is never read: webhooks create and delete memberships, and Marden sets the
// org role on creation (queries/org-roles.ts) and never changes it from a webhook. A new member gets no
// product role, except the first member of an org that already holds live products, who becomes owner
// of each of them.
import type { Queryable } from '../db.js';
import { audit } from './audit.js';
import { createMembership, revokeInvitation } from './org-roles.js';
import { grantFirstMemberOwner } from './products.js';

export interface ClerkEvent {
  type: string;
  data: Record<string, unknown>;
  timestamp?: number;
}

export type Outcome = 'applied' | 'stale' | 'duplicate' | 'ignored';

export interface SyncResult {
  outcome: Outcome;
  sessionId: string | null;   // session.revoked / session.ended: streams for it must close
  userId: string | null;      // user.deleted: streams for the user must close
}

interface Plan {
  subject: string;
  related: string[];           // subjects whose later deletion makes this event stale
  orgId: string | null;
  userId: string | null;
  deletes: boolean;
  apply: (c: Queryable, at: number) => Promise<void>;   // at: the event time in ms
}

const s = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null);
const obj = (v: unknown): Record<string, unknown> => (v && typeof v === 'object' ? (v as Record<string, unknown>) : {});
const fullName = (d: Record<string, unknown>): string | null =>
  [s(d.first_name), s(d.last_name)].filter(Boolean).join(' ') || null;

function primaryEmail(d: Record<string, unknown>): string {
  const emails = Array.isArray(d.email_addresses) ? d.email_addresses.map(obj) : [];
  const primary = emails.find((e) => e.id === d.primary_email_address_id) ?? emails[0];
  return s(primary?.email_address) ?? '';
}

function userPlan(type: string, d: Record<string, unknown>): Plan | null {
  const id = s(d.id);
  if (!id) return null;
  const deletes = type === 'user.deleted';
  return {
    subject: `user:${id}`, related: [], orgId: null, userId: id, deletes,
    apply: async (c) => {
      if (deletes) await c.query(`DELETE FROM app_user WHERE id = $1`, [id]);
      else {
        await c.query(
          `INSERT INTO app_user (id, email, name) VALUES ($1, $2, $3)
           ON CONFLICT (id) DO UPDATE SET email = EXCLUDED.email, name = EXCLUDED.name`,
          [id, primaryEmail(d), fullName(d)],
        );
      }
    },
  };
}

function orgPlan(type: string, d: Record<string, unknown>): Plan | null {
  const id = s(d.id);
  if (!id) return null;
  const deletes = type === 'organization.deleted';
  return {
    subject: `org:${id}`, related: [], orgId: id, userId: null, deletes,
    apply: async (c) => {
      if (deletes) await c.query(`DELETE FROM org WHERE id = $1`, [id]);
      // The plan is Marden's (billing or staff); Clerk only names the org.
      else await c.query(`INSERT INTO org (id, name) VALUES ($1, $2) ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name`, [id, s(d.name) ?? id]);
    },
  };
}

function membershipPlan(type: string, d: Record<string, unknown>): Plan | null {
  const org = obj(d.organization);
  const user = obj(d.public_user_data);
  const orgId = s(org.id);
  const userId = s(user.user_id);
  if (!orgId || !userId) return null;
  const deletes = type === 'organizationMembership.deleted';
  // d.role (the Clerk role) is deliberately not read: it may be set by the other product.
  return {
    subject: `membership:${orgId}:${userId}`, related: [`user:${userId}`, `org:${orgId}`], orgId, userId, deletes,
    apply: async (c, at) => {
      if (deletes) {
        await c.query(`DELETE FROM membership WHERE org_id = $1 AND user_id = $2`, [orgId, userId]);
        return;
      }
      await c.query(`INSERT INTO org (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING`, [orgId, s(org.name) ?? orgId]);
      await c.query(`INSERT INTO app_user (id, email, name) VALUES ($1, $2, $3) ON CONFLICT (id) DO NOTHING`, [
        userId, s(user.identifier) ?? '', fullName(user),
      ]);
      // created, or updated before Marden saw it created: create it; an existing membership keeps its role.
      const joined = await createMembership(c, { orgId, userId, identifier: s(user.identifier), at });
      if (joined?.rule === 'first_member') await grantFirstMemberOwner(c, orgId, userId);
    },
  };
}

/** Only a revocation matters: nobody joins through a revoked invitation. The role on it is Marden's. */
function invitationPlan(type: string, d: Record<string, unknown>): Plan | null {
  const id = s(d.id);
  if (!id || type !== 'organizationInvitation.revoked') return null;
  return {
    subject: `invitation:${id}`, related: [], orgId: s(d.organization_id), userId: null, deletes: false,
    apply: (c) => revokeInvitation(c, id),
  };
}

function planFor(evt: ClerkEvent): Plan | null {
  const [kind] = evt.type.split('.');
  if (kind === 'user') return userPlan(evt.type, evt.data);
  if (kind === 'organization') return orgPlan(evt.type, evt.data);
  if (kind === 'organizationMembership') return membershipPlan(evt.type, evt.data);
  if (kind === 'organizationInvitation') return invitationPlan(evt.type, evt.data);
  return null;
}

/** Event time in ms: Clerk's event timestamp, else the object's updated_at, else the svix send time. */
export function eventTime(evt: ClerkEvent, svixTimestampSeconds: number): number {
  if (typeof evt.timestamp === 'number') return evt.timestamp;
  if (typeof evt.data.updated_at === 'number') return evt.data.updated_at;
  return svixTimestampSeconds * 1000;
}

async function isStale(c: Queryable, plan: Plan, at: number): Promise<boolean> {
  const res = await c.query<{ stale: boolean }>(
    `SELECT EXISTS (
       SELECT 1 FROM audit_log WHERE action = 'clerk.webhook' AND meta ->> 'outcome' = 'applied'
         AND (meta ->> 'eventAt')::bigint > $2
         AND (meta ->> 'subject' = $1 OR (meta ->> 'subject' = ANY($3::text[]) AND meta ->> 'deletes' = 'true'))
     ) AS stale`,
    [plan.subject, at, plan.related],
  );
  return res.rows[0]?.stale ?? false;
}

function subjectOf(evt: ClerkEvent, plan: Plan | null, msgId: string): { subject: string; sessionId: string | null } {
  const sessionId = evt.type.startsWith('session.') ? s(evt.data.id) : null;
  if (plan) return { subject: plan.subject, sessionId };
  if (sessionId) return { subject: `session:${sessionId}`, sessionId };
  return { subject: `${evt.type}:${s(evt.data.id) ?? msgId}`, sessionId };
}

async function decide(c: Queryable, plan: Plan | null, at: number): Promise<Outcome> {
  if (!plan) return 'ignored';
  if (await isStale(c, plan, at)) return 'stale';
  await plan.apply(c, at);
  return 'applied';
}

export async function applyClerkEvent(c: Queryable, evt: ClerkEvent, msg: { id: string; at: number }): Promise<SyncResult> {
  const plan = planFor(evt);
  const { subject, sessionId } = subjectOf(evt, plan, msg.id);
  await c.query(`SELECT pg_advisory_xact_lock(hashtext($1))`, [`clerk:${subject}`]);
  const dup = await c.query(`SELECT 1 FROM audit_log WHERE action = 'clerk.webhook' AND target = $1 LIMIT 1`, [msg.id]);
  if ((dup.rowCount ?? 0) > 0) return { outcome: 'duplicate', sessionId: null, userId: null };

  const outcome = await decide(c, plan, msg.at);
  await audit(c, {
    orgId: plan?.orgId ?? null,
    userId: plan?.userId ?? s(evt.data.user_id),
    action: 'clerk.webhook',
    target: msg.id,
    meta: { type: evt.type, subject, eventAt: msg.at, outcome, deletes: plan?.deletes ?? false },
  });
  const userDeleted = outcome === 'applied' && evt.type === 'user.deleted';
  return { outcome, sessionId, userId: userDeleted ? (plan?.userId ?? null) : null };
}
