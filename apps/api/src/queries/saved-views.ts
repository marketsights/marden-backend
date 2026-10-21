// Saved views (spec 03 header "Saved views"; specs 14, 15): a list screen's URL filters and sort under
// a name, per user within the org. Another user's or org's id reads as missing (-> 404).
import type { SavedView } from '../contracts/app.js';
import type { Queryable } from '../db.js';

type Who = { orgId: string; userId: string };

interface Row { id: string; screen: string; name: string; query: string; created_at: Date }

const COLUMNS = `id::text, screen, name, query, created_at`;
const toView = (r: Row): SavedView => ({ id: r.id, screen: r.screen, name: r.name, query: r.query, createdAt: r.created_at.toISOString() });

/** Newest first. */
export async function listSavedViews(c: Queryable, who: Who, screen: string): Promise<SavedView[]> {
  const res = await c.query<Row>(
    `SELECT ${COLUMNS} FROM saved_view WHERE org_id = $1 AND user_id = $2 AND screen = $3 ORDER BY created_at DESC, id`,
    [who.orgId, who.userId, screen],
  );
  return res.rows.map(toView);
}

/** Saving under a name the user already has replaces that view's query (no duplicate names). */
export async function upsertSavedView(c: Queryable, who: Who, input: { screen: string; name: string; query: string }): Promise<{ view: SavedView; created: boolean }> {
  const res = await c.query<Row & { inserted: boolean }>(
    `INSERT INTO saved_view (org_id, user_id, screen, name, query) VALUES ($1, $2, $3, $4, $5)
     ON CONFLICT (org_id, user_id, screen, name) DO UPDATE SET query = EXCLUDED.query
     RETURNING ${COLUMNS}, (xmax = 0) AS inserted`,
    [who.orgId, who.userId, input.screen, input.name, input.query],
  );
  const row = res.rows[0];
  if (!row) throw new Error('insert returned no row');
  return { view: toView(row), created: row.inserted };
}

export async function deleteSavedView(c: Queryable, who: Who, id: string): Promise<SavedView | null> {
  const res = await c.query<Row>(
    `DELETE FROM saved_view WHERE org_id = $1 AND user_id = $2 AND id::text = $3 RETURNING ${COLUMNS}`,
    [who.orgId, who.userId, id],
  );
  const row = res.rows[0];
  return row ? toView(row) : null;
}
