// Account (spec 13, 23, docs/access-control.md): team with per-product roles, the org's products and
// seats, invites, org role changes and removals, and GET /api/me.
// Roles are Marden's: an invite records the Marden role in org_invitation and asks Clerk for an
// invitation with its basic member role; a role change writes Marden's tables and audit_log only.
// Clerk is called for invitations and removals (membership existence), never for a role.
import type { FastifyInstance, FastifyRequest } from 'fastify';
import { z } from 'zod';
import type { AuthContext } from '../auth/access.js';
import type { ClerkAdmin } from '../auth/clerk-admin.js';
import { effectivePermissions } from '../auth/grants.js';
import { authOf } from '../auth/plugin.js';
import type { ProductRegistry } from '../auth/products/index.js';
import type { AccountResponse, MeProduct, MeResponse, Role } from '../contracts/app.js';
import type { Db, Queryable } from '../db.js';
import { notFound, unavailable } from '../errors.js';
import { checkInvite, checkRemoval, checkRoleChange } from '../lib/member-rules.js';
import { getOrg, getUser, lockMembers, removeMember, setMemberRole, team } from '../queries/account.js';
import { audit } from '../queries/audit.js';
import { recordInvitation } from '../queries/org-roles.js';
import { orgProducts } from '../queries/products.js';
import { param, parseBody } from './serve.js';

export interface AccountDeps {
  db: Db;
  clerkAdmin: ClerkAdmin | null;
  devMode: boolean;
  products: ProductRegistry;
}

const ROLE = z.enum(['owner', 'admin', 'member', 'viewer']);
const inviteSchema = z.strictObject({ email: z.email().max(320), role: ROLE.default('viewer') });
const roleSchema = z.strictObject({ role: ROLE });

/** Removals end the Clerk membership too; only dev mode (no Clerk) changes the local rows alone. */
function clerkFor(deps: AccountDeps): ClerkAdmin | null {
  if (!deps.clerkAdmin && !deps.devMode) throw unavailable('clerk_backend_api_not_configured');
  return deps.clerkAdmin;
}

async function changeMember(
  c: Queryable,
  deps: AccountDeps,
  req: FastifyRequest,
  change: { auth: AuthContext; userId: string; role: Role | null },
): Promise<void> {
  const { auth, userId, role } = change;
  const locked = await lockMembers(c, auth.orgId, userId);
  if (!locked.role) throw notFound('member');
  if (role) {
    checkRoleChange(auth.orgRole, locked.role, role, locked.owners);
    if (role === locked.role) return;
    // Marden's role: no Clerk call (the Clerk membership keeps Clerk's basic member role).
    await setMemberRole(c, auth.orgId, userId, role);
  } else {
    checkRemoval(auth.orgRole, locked.role, locked.owners);
    await clerkFor(deps)?.removeMember(auth.orgId, userId);
    await removeMember(c, auth.orgId, userId);
  }
  await audit(c, {
    orgId: auth.orgId, userId: auth.userId, action: role ? 'member.role' : 'member.remove',
    target: `user:${userId}`, ip: req.ip, meta: { from: locked.role, to: role },
  });
}

export function accountRoutes(app: FastifyInstance, deps: AccountDeps): void {
  const { db } = deps;

  app.get('/api/me', async (req): Promise<MeResponse> => {
    const auth = authOf(req);
    const [user, org, held] = await Promise.all([getUser(db, auth.userId), getOrg(db, auth.orgId), orgProducts(db, auth.orgId)]);
    if (!user || !org) throw notFound('member');
    const roles = new Map(auth.grants.map((g) => [g.productId, g.role]));
    const products: MeProduct[] = held.filter((p) => p.live).map((p) => ({
      id: p.id, name: p.name, plan: p.plan, status: p.status, role: roles.get(p.id) ?? null, seats: p.seats, seatsUsed: p.seatsUsed,
    }));
    return {
      user, org: { id: org.id, name: org.name }, orgRole: auth.orgRole, products,
      permissions: effectivePermissions(auth, deps.products), staff: auth.staff,
    };
  });

  app.get('/api/account', async (req): Promise<AccountResponse> => {
    const auth = authOf(req);
    const [org, members, products] = await db.withOrg(auth.orgId, async (c) =>
      [await getOrg(c, auth.orgId), await team(c, auth.orgId), await orgProducts(c, auth.orgId)] as const);
    if (!org) throw notFound('org');
    return {
      org: { id: org.id, name: org.name, createdAt: org.created_at.toISOString() },
      team: members,
      products,
      seats: { used: members.length, total: null },
      security: {
        authProvider: deps.devMode ? 'dev' : 'clerk',
        mfaEnabled: deps.clerkAdmin ? await deps.clerkAdmin.mfaEnabled(auth.userId) : null,
      },
    };
  });

  app.post('/api/account/invites', async (req, reply) => {
    const auth = authOf(req);
    const body = parseBody(inviteSchema, req.body);
    checkInvite(auth.orgRole, body.role);
    if (!deps.clerkAdmin) throw unavailable('clerk_backend_api_not_configured');
    // Clerk sends the email and, on acceptance, creates the membership (basic member role); the Marden
    // role travels in org_invitation and is applied when the membership webhook arrives.
    const invitation = await deps.clerkAdmin.invite(auth.orgId, body.email, auth.userId);
    await db.withOrg(auth.orgId, async (c) => {
      await recordInvitation(c, {
        id: invitation.id, orgId: auth.orgId, email: body.email, role: body.role, invitedBy: auth.userId, expiresAt: invitation.expiresAt,
      });
      await audit(c, {
        orgId: auth.orgId, userId: auth.userId, action: 'member.invite', target: `invitation:${invitation.id}`,
        ip: req.ip, meta: { email: body.email, role: body.role },
      });
    });
    return reply.code(201).send({ invitationId: invitation.id, email: body.email, role: body.role });
  });

  app.patch('/api/account/members/:id', async (req) => {
    const auth = authOf(req);
    const userId = param(req, 'id');
    const { role } = parseBody(roleSchema, req.body);
    await db.withOrg(auth.orgId, (c) => changeMember(c, deps, req, { auth, userId, role }));
    const member = (await team(db, auth.orgId)).find((m) => m.userId === userId);
    if (!member) throw notFound('member');
    return member;
  });

  app.delete('/api/account/members/:id', async (req, reply) => {
    const auth = authOf(req);
    const userId = param(req, 'id');
    await db.withOrg(auth.orgId, (c) => changeMember(c, deps, req, { auth, userId, role: null }));
    return reply.code(204).send();
  });
}
