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
