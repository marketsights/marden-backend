// Watchlist rows (org-owned) joined to the latest site and instrument read models.
import type { Db, Queryable } from '../db.js';

export type WatchKind = 'site' | 'instrument';

export interface WatchJoinRow {
  id: string;
  kind: WatchKind;
  ref_id: string;
  identity_name: string | null;
  instrument_kind: string | null;
  site_doc: Record<string, unknown> | null;     // SiteDetail of a watched site
  inst_doc: Record<string, unknown> | null;     // InstrumentDetail of a watched instrument
  inst_site_doc: Record<string, unknown> | null; // SiteDetail of the site an instrument depends on
}

const latestDocs = (view: string, keys: string): string => `
  SELECT DISTINCT ON (key) key, doc FROM read_model
  WHERE view = '${view}' AND ($2::date IS NULL OR as_of <= $2::date) AND key IN (${keys})
  ORDER BY key, as_of DESC`;

export async function watchRows(db: Queryable, orgId: string, asOf: string | null): Promise<WatchJoinRow[]> {
  const res = await db.query<WatchJoinRow>(
    `WITH items AS (SELECT id, kind, ref_id, created_at FROM watchlist_item WHERE org_id = $1),
     site_keys AS (
       SELECT ref_id AS key FROM items WHERE kind = 'site'
       UNION SELECT i.site_slug FROM instrument i JOIN items w ON w.kind = 'instrument' AND w.ref_id = i.id
       WHERE i.site_slug IS NOT NULL),
     site_docs AS (${latestDocs('site_detail', 'SELECT key FROM site_keys')}),
     inst_docs AS (${latestDocs('instrument_detail', `SELECT ref_id FROM items WHERE kind = 'instrument'`)})
     SELECT w.id::text, w.kind, w.ref_id, coalesce(s.name, i.name) AS identity_name, i.kind AS instrument_kind,
            sd.doc AS site_doc, idoc.doc AS inst_doc, isd.doc AS inst_site_doc
     FROM items w
     LEFT JOIN site s ON w.kind = 'site' AND s.slug = w.ref_id
     LEFT JOIN instrument i ON w.kind = 'instrument' AND i.id = w.ref_id
     LEFT JOIN site_docs sd ON w.kind = 'site' AND sd.key = w.ref_id
     LEFT JOIN inst_docs idoc ON w.kind = 'instrument' AND idoc.key = w.ref_id
     LEFT JOIN site_docs isd ON isd.key = i.site_slug
     ORDER BY w.created_at, w.id`,
    [orgId, asOf],
  );
  return res.rows;
}

/** Site slugs on the org's watchlist (for "Watchlist only" filters). */
export async function watchedSiteSlugs(db: Db, orgId: string): Promise<Set<string>> {
  const res = await db.query<{ ref_id: string }>(
    `SELECT ref_id FROM watchlist_item WHERE org_id = $1 AND kind = 'site'`,
    [orgId],
  );
  return new Set(res.rows.map((r) => r.ref_id));
}

export async function refExists(c: Queryable, kind: WatchKind, refId: string): Promise<boolean> {
  const sql = kind === 'site' ? `SELECT 1 FROM site WHERE slug = $1` : `SELECT 1 FROM instrument WHERE id = $1`;
  return ((await c.query(sql, [refId])).rowCount ?? 0) > 0;
}

/** Idempotent add: an item already on the org's list keeps its id. */
export async function addWatchItem(
  c: Queryable,
  scope: { orgId: string; userId: string },
  item: { kind: WatchKind; refId: string },
): Promise<{ id: string; created: boolean }> {
  const ins = await c.query<{ id: string }>(
    // clock_timestamp keeps the request's order when several items are added in one transaction.
    `INSERT INTO watchlist_item (org_id, user_id, kind, ref_id, created_at) VALUES ($1, $2, $3, $4, clock_timestamp())
     ON CONFLICT (org_id, kind, ref_id) DO NOTHING RETURNING id::text`,
    [scope.orgId, scope.userId, item.kind, item.refId],
  );
  if (ins.rows[0]) return { id: ins.rows[0].id, created: true };
  const existing = await c.query<{ id: string }>(
    `SELECT id::text FROM watchlist_item WHERE org_id = $1 AND kind = $2 AND ref_id = $3`,
    [scope.orgId, item.kind, item.refId],
  );
  return { id: existing.rows[0]?.id ?? '', created: false };
}

/** Deletes only within the caller's org; false (-> 404) for unknown ids and other orgs' items. */
export async function removeWatchItem(c: Queryable, orgId: string, id: string): Promise<{ kind: string; refId: string } | null> {
  const res = await c.query<{ kind: string; ref_id: string }>(
    `DELETE FROM watchlist_item WHERE org_id = $1 AND id::text = $2 RETURNING kind, ref_id`,
    [orgId, id],
  );
  const row = res.rows[0];
  return row ? { kind: row.kind, refId: row.ref_id } : null;
}
