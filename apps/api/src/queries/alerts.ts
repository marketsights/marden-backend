// Alerts for one member of one org (the alert engine writes one row per member: alert.user_id) and
// their read state, which is per member.
import type { Alert, Tag } from '../contracts/types.js';
import type { Db, Queryable } from '../db.js';
import { dayEnd } from '../lib/business-day.js';

interface AlertRow {
  id: string; at: Date; headline: string; tag: Tag; rule_label: string; ref_url: string;
  read_at: Date | null; emailed_at: Date | null;
}

const toAlert = (r: AlertRow): Alert => ({
  id: r.id,
  at: r.at.toISOString(),
  headline: r.headline,
  tag: r.tag,
  ruleLabel: r.rule_label,
  refUrl: r.ref_url,
  read: r.read_at !== null,
  emailed: r.emailed_at !== null,
});

export async function listAlerts(
  db: Queryable,
  orgId: string,
  userId: string,
  opts: { asOf: string | null; unreadOnly: boolean; limit: number },
): Promise<Alert[]> {
  const res = await db.query<AlertRow>(
    `SELECT id::text, at, headline, tag, rule_label, ref_url, read_at, emailed_at FROM alert
     WHERE org_id = $1 AND user_id = $5 AND ($2::date IS NULL OR at < ${dayEnd('$2')}) AND (NOT $3 OR read_at IS NULL)
     ORDER BY at DESC, id DESC LIMIT $4`,
    [orgId, opts.asOf, opts.unreadOnly, opts.limit, userId],
  );
  return res.rows.map(toAlert);
}

export async function unreadAlerts(
  db: Db, orgId: string, userId: string, asOf: string | null,
): Promise<{ count: number; latestAt: string | null }> {
  const res = await db.query<{ count: number; latest: Date | null }>(
    `SELECT count(*)::int AS count, max(at) AS latest FROM alert
     WHERE org_id = $1 AND user_id = $3 AND read_at IS NULL AND ($2::date IS NULL OR at < ${dayEnd('$2')})`,
    [orgId, asOf, userId],
  );
  const row = res.rows[0];
  return { count: row?.count ?? 0, latestAt: row?.latest ? row.latest.toISOString() : null };
}

/** Marks the member's given alerts (or all their unread when ids is null) read; returns ids changed. */
export async function markAlertsRead(c: Queryable, orgId: string, userId: string, ids: string[] | null): Promise<string[]> {
  const res = await c.query<{ id: string }>(
    `UPDATE alert SET read_at = now()
     WHERE org_id = $1 AND user_id = $3 AND read_at IS NULL AND ($2::text[] IS NULL OR id::text = ANY($2::text[]))
     RETURNING id::text`,
    [orgId, ids, userId],
  );
  return res.rows.map((r) => r.id);
}
