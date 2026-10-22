// The org's watched names for GET /api/home (watchlist items are org-owned, spec 11).
import type { Db } from '../db.js';
import type { Watched } from '../lib/home-doc.js';

export async function watchedRefs(db: Db, orgId: string): Promise<Watched> {
  const res = await db.query<{ kind: 'site' | 'instrument'; ref_id: string }>(
    `SELECT kind, ref_id FROM watchlist_item WHERE org_id = $1 ORDER BY kind, ref_id`,
    [orgId],
  );
  return {
    sites: res.rows.filter((r) => r.kind === 'site').map((r) => r.ref_id),
    instruments: res.rows.filter((r) => r.kind === 'instrument').map((r) => r.ref_id),
  };
}

/** When the member's previous session started (membership.last_visit_at, stamped by auth/activity.ts);
 * null until the API has seen it start. */
export async function lastVisitOf(db: Db, orgId: string, userId: string): Promise<string | null> {
  const res = await db.query<{ at: Date | null }>(
    `SELECT last_visit_at AS at FROM membership WHERE org_id = $1 AND user_id = $2`,
    [orgId, userId],
  );
  return res.rows[0]?.at?.toISOString() ?? null;
}
