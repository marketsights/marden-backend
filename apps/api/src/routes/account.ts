// Account (spec 13, 23): team, plan, invites, role changes and removals, and GET /api/me.
import type { FastifyInstance, FastifyRequest } from 'fastify';
import { z } from 'zod';
import type { AuthContext } from '../auth/access.js';
import type { ClerkAdmin } from '../auth/clerk-admin.js';
import { permissionsFor } from '../auth/permissions.js';
import { authOf } from '../auth/plugin.js';
import type { AccountResponse, MeResponse, Role } from '../contracts/app.js';
import type { Db, Queryable } from '../db.js';
import { notFound, unavailable } from '../errors.js';
import { checkInvite, checkRemoval, checkRoleChange } from '../lib/member-rules.js';
import { getOrg, getUser, lockMembers, removeMember, setMemberRole, team } from '../queries/account.js';
import { audit } from '../queries/audit.js';
import { param, parseBody } from './serve.js';

export interface AccountDeps {
  db: Db;
  clerkAdmin: ClerkAdmin | null;
  devMode: boolean;
}

const ROLE = z.enum(['owner', 'admin', 'member', 'viewer']);
const inviteSchema = z.strictObject({ email: z.email().max(320), role: ROLE.default('viewer') });
const roleSchema = z.strictObject({ role: ROLE });

/** Team changes go through Clerk; only dev mode (no Clerk) changes the local rows alone. */
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
  const clerk = clerkFor(deps);
  if (role) {
    checkRoleChange(auth.role, locked.role, role, locked.owners);
    await clerk?.updateRole(auth.orgId, userId, role);
    await setMemberRole(c, auth.orgId, userId, role);
  } else {
    checkRemoval(auth.role, locked.role, locked.owners);
    await clerk?.removeMember(auth.orgId, userId);
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
    const [user, org] = await Promise.all([getUser(db, auth.userId), getOrg(db, auth.orgId)]);
    if (!user || !org) throw notFound('member');
    return {
      user, org: { id: org.id, name: org.name, plan: org.plan }, role: auth.role, staff: auth.staff,
      permissions: permissionsFor(auth.role, auth.staff),
    };
  });

  app.get('/api/account', async (req): Promise<AccountResponse> => {
    const auth = authOf(req);
    const [org, members] = await db.withOrg(auth.orgId, (c) => Promise.all([getOrg(c, auth.orgId), team(c, auth.orgId)]));
    if (!org) throw notFound('org');
    return {
      org: { id: org.id, name: org.name, plan: org.plan, createdAt: org.created_at.toISOString() },
      team: members,
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
    checkInvite(auth.role, body.role);
    if (!deps.clerkAdmin) throw unavailable('clerk_backend_api_not_configured');
    const invitation = await deps.clerkAdmin.invite(auth.orgId, body.email, body.role, auth.userId);
    await audit(db, {
      orgId: auth.orgId, userId: auth.userId, action: 'member.invite', target: `invitation:${invitation.id}`,
      ip: req.ip, meta: { email: body.email, role: body.role },
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
