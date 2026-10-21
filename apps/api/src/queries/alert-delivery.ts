// Alert email delivery for one member (spec 13): the worker's email configuration (email_provider_state),
// the member's recent emails (email_outbox), and the one-click unsubscribe that switches emails off.
import type { AlertDelivery, AlertEmail, AlertEmailKind, AlertEmailStatus } from '../contracts/app.js';
import type { Queryable } from '../db.js';
import { SCOPE_KINDS, unsubscribed, type UnsubscribeScope } from '../lib/unsubscribe-token.js';
import { audit } from './audit.js';
import { getAlertSettings, putAlertSettings } from './prefs.js';

const STATUSES: readonly AlertEmailStatus[] = ['queued', 'sent', 'failed', 'not_configured', 'cancelled'];
const RECENT = 10;
const NOT_CHECKED = 'the worker has not checked the email configuration yet';

interface OutboxRow {
  id: string; kind: AlertEmailKind; subject: string; status: AlertEmailStatus; alerts: number; created_at: Date;
  sent_at: Date | null; attempts: number; last_error: string | null;
}

/** Errors a member sees: why an email failed or was cancelled. 'not_configured' says so by its status (the
 * missing variable names are for staff). */
const errorFor = (r: OutboxRow, staff: boolean): string | null =>
  staff || r.status === 'failed' || r.status === 'cancelled' ? r.last_error : null;

export async function alertDelivery(
  c: Queryable, who: { orgId: string; userId: string; staff: boolean; email: string },
): Promise<AlertDelivery> {
  const state = (await c.query<{ provider: 'resend' | 'postmark' | null; configured: boolean; reason: string | null; checked_at: Date }>(
    `SELECT provider, configured, reason, checked_at FROM email_provider_state WHERE id = 'alerts'`,
  )).rows[0];
  const counts = Object.fromEntries(STATUSES.map((s) => [s, 0])) as Record<AlertEmailStatus, number>;
  const grouped = await c.query<{ status: AlertEmailStatus; n: number }>(
    `SELECT status, count(*)::int AS n FROM email_outbox
     WHERE org_id = $1 AND user_id = $2 AND created_at > now() - interval '30 days' GROUP BY status`,
    [who.orgId, who.userId],
  );
  for (const row of grouped.rows) counts[row.status] = row.n;
  const recent = await c.query<OutboxRow>(
    `SELECT id::text, kind, subject, status, cardinality(alert_ids)::int AS alerts, created_at, sent_at, attempts, last_error
     FROM email_outbox WHERE org_id = $1 AND user_id = $2 ORDER BY created_at DESC, id DESC LIMIT $3`,
    [who.orgId, who.userId, RECENT],
  );
  const emails: AlertEmail[] = recent.rows.map((r) => ({
    id: r.id, kind: r.kind, subject: r.subject, status: r.status, alerts: r.alerts, createdAt: r.created_at.toISOString(),
    sentAt: r.sent_at ? r.sent_at.toISOString() : null, attempts: r.attempts, error: errorFor(r, who.staff),
  }));
  return {
    email: {
      configured: state?.configured ?? false,
      provider: state?.provider ?? null,
      checkedAt: state ? state.checked_at.toISOString() : null,
      reason: who.staff ? (state ? state.reason : NOT_CHECKED) : null,
    },
    to: who.email,
    counts,
    recent: emails,
  };
}

export type UnsubscribeOutcome = { ok: true; changed: boolean; cancelled: number } | { ok: false; reason: 'not_a_member' };

/**
 * Switches the scope's emails off for the member and cancels their queued emails of those kinds. Idempotent:
 * a second click changes nothing. Runs inside db.withOrg(orgId).
 */
export async function applyUnsubscribe(
  c: Queryable, p: { orgId: string; userId: string; scope: UnsubscribeScope; ip: string | null },
): Promise<UnsubscribeOutcome> {
  const member = await c.query(`SELECT 1 FROM membership WHERE org_id = $1 AND user_id = $2`, [p.orgId, p.userId]);
  if (member.rowCount === 0) return { ok: false, reason: 'not_a_member' };
  const who = { orgId: p.orgId, userId: p.userId };
  const before = await getAlertSettings(c, who);
  const after = unsubscribed(before, p.scope);
  const changed = JSON.stringify(before) !== JSON.stringify(after);
  if (changed) await putAlertSettings(c, who, after);
  const cancelled = await c.query(
    `UPDATE email_outbox SET status = 'cancelled', last_error = $4, updated_at = now()
     WHERE org_id = $1 AND user_id = $2 AND status = 'queued' AND kind = ANY($3::text[])`,
    [p.orgId, p.userId, [...SCOPE_KINDS[p.scope]], `unsubscribed (${p.scope}) by the email link`],
  );
  if (changed || (cancelled.rowCount ?? 0) > 0) {
    await audit(c, {
      orgId: p.orgId, userId: p.userId, action: 'alert_settings.unsubscribe', ip: p.ip,
      meta: { scope: p.scope, settings: after, cancelled: cancelled.rowCount ?? 0 },
    });
  }
  return { ok: true, changed, cancelled: cancelled.rowCount ?? 0 };
}
