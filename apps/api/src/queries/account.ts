// Org, team and membership reads/writes (Marden side of the Clerk sync).
import type { Plan, Role, TeamMember } from '../contracts/app.js';
import type { Queryable } from '../db.js';

export interface OrgRow { id: string; name: string; plan: Plan; created_at: Date }

export async function getOrg(c: Queryable, orgId: string): Promise<OrgRow | null> {
  const res = await c.query<OrgRow>(`SELECT id, name, plan, created_at FROM org WHERE id = $1`, [orgId]);
  return res.rows[0] ?? null;
}

export async function getUser(c: Queryable, userId: string): Promise<{ id: string; email: string; name: string | null } | null> {
  const res = await c.query<{ id: string; email: string; name: string | null }>(
    `SELECT id, email, name FROM app_user WHERE id = $1`,
    [userId],
  );
  return res.rows[0] ?? null;
}

export async function team(c: Queryable, orgId: string): Promise<TeamMember[]> {
  const res = await c.query<{ user_id: string; email: string; name: string | null; role: Role }>(
    `SELECT m.user_id, u.email, u.name, m.role FROM membership m JOIN app_user u ON u.id = m.user_id
     WHERE m.org_id = $1
     ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 WHEN 'member' THEN 2 ELSE 3 END, u.email`,
    [orgId],
  );
  return res.rows.map((r) => ({ userId: r.user_id, email: r.email, name: r.name, role: r.role }));
}

/**
 * Locks the org's memberships for a role change or removal and returns the target's role and the
 * owner count, so "the last owner can't be removed or demoted" holds under concurrent requests.
 */
export async function lockMembers(c: Queryable, orgId: string, userId: string): Promise<{ role: Role | null; owners: number }> {
  const res = await c.query<{ user_id: string; role: Role }>(
    `SELECT user_id, role FROM membership WHERE org_id = $1 ORDER BY user_id FOR UPDATE`,
    [orgId],
  );
  const target = res.rows.find((r) => r.user_id === userId);
  return { role: target?.role ?? null, owners: res.rows.filter((r) => r.role === 'owner').length };
}

export async function setMemberRole(c: Queryable, orgId: string, userId: string, role: Role): Promise<void> {
  await c.query(`UPDATE membership SET role = $3 WHERE org_id = $1 AND user_id = $2`, [orgId, userId, role]);
}

export async function removeMember(c: Queryable, orgId: string, userId: string): Promise<void> {
  await c.query(`DELETE FROM membership WHERE org_id = $1 AND user_id = $2`, [orgId, userId]);
}

export async function isMember(c: Queryable, orgId: string, userId: string): Promise<boolean> {
  const res = await c.query(`SELECT 1 FROM membership WHERE org_id = $1 AND user_id = $2`, [orgId, userId]);
  return (res.rowCount ?? 0) > 0;
}
