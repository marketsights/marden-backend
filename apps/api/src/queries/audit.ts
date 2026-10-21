// Append-only audit log: every mutation, member change and processed webhook writes one row.
import type { Queryable } from '../db.js';

export interface AuditEntry {
  orgId: string | null;
  userId: string | null;
  action: string;          // 'watchlist.add', 'member.role', 'clerk.webhook', ...
  target?: string | null;
  ip?: string | null;
  meta?: Record<string, unknown>;
}

export async function audit(c: Queryable, e: AuditEntry): Promise<void> {
  await c.query(
    `INSERT INTO audit_log (org_id, user_id, action, target, ip, meta) VALUES ($1, $2, $3, $4, $5, $6)`,
    [e.orgId, e.userId, e.action, e.target ?? null, e.ip ?? null, JSON.stringify(e.meta ?? {})],
  );
}
