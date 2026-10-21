// Per-product access rows (docs/access-control.md): the firm's products (org_product) and each member's
// role on them (member_product_role). Seat limits hold under concurrent requests: every assignment locks
// its org_product row first, then counts.
import type { ProductGrant } from '../auth/grants.js';
import type { OrgProduct, Role } from '../contracts/app.js';
import type { Queryable } from '../db.js';
import { audit } from './audit.js';

/** Today in UTC: org_product dates are calendar days in UTC, ends_on inclusive. */
const TODAY = `(now() AT TIME ZONE 'UTC')::date`;
/** True when org_product `op` of product `p` grants access today. */
export const LIVE_SQL = `(op.status = 'active' AND p.active AND op.starts_on <= ${TODAY} AND (op.ends_on IS NULL OR op.ends_on >= ${TODAY}))`;


const ORG_PRODUCT_COLUMNS = `
  op.product_id AS id, p.name, op.plan, op.status, op.seats, op.starts_on AS "startsOn", op.ends_on AS "endsOn",
  ${LIVE_SQL} AS live,
  (SELECT count(*)::int FROM member_product_role r WHERE r.org_id = op.org_id AND r.product_id = op.product_id) AS "seatsUsed"`;

/** The org's products, live or not, by name. */
export async function orgProducts(c: Queryable, orgId: string): Promise<OrgProduct[]> {
  const res = await c.query<OrgProduct>(
    `SELECT ${ORG_PRODUCT_COLUMNS} FROM org_product op JOIN product p ON p.id = op.product_id
     WHERE op.org_id = $1 ORDER BY p.name, op.product_id`,
    [orgId],
  );
  return res.rows;
}

export async function orgProduct(c: Queryable, orgId: string, productId: string): Promise<OrgProduct | null> {
  const res = await c.query<OrgProduct>(
    `SELECT ${ORG_PRODUCT_COLUMNS} FROM org_product op JOIN product p ON p.id = op.product_id
     WHERE op.org_id = $1 AND op.product_id = $2`,
    [orgId, productId],
  );
  return res.rows[0] ?? null;
}

/** SQL (a json array) of the member's roles on live products; `m` is the membership row. */
export const LIVE_GRANTS_SQL = `COALESCE((
  SELECT json_agg(json_build_object('productId', r.product_id, 'role', r.role, 'plan', op.plan) ORDER BY r.product_id)
  FROM member_product_role r
  JOIN org_product op ON op.org_id = r.org_id AND op.product_id = r.product_id
  JOIN product p ON p.id = r.product_id
  WHERE r.org_id = m.org_id AND r.user_id = m.user_id AND ${LIVE_SQL}), '[]'::json)`;

export async function liveGrants(c: Queryable, orgId: string, userId: string): Promise<ProductGrant[] | null> {
  const res = await c.query<{ grants: ProductGrant[] }>(
    `SELECT ${LIVE_GRANTS_SQL} AS grants FROM membership m WHERE m.org_id = $1 AND m.user_id = $2`,
    [orgId, userId],
  );
  return res.rows[0]?.grants ?? null;
}

export interface Assignment {
  orgId: string;
  userId: string;
  productId: string;
  role: Role | null;           // null removes the member's role on the product
  grantedBy: string | null;    // null: a webhook rule
}

export type AssignResult =
  | { ok: true; from: Role | null; to: Role | null }
  | { ok: false; reason: 'no_product' | 'seat_limit'; seats?: number; used?: number };

/** Sets or removes a member's role on one of the org's products. A new assignment needs a free seat. */
export async function assignProductRole(c: Queryable, a: Assignment): Promise<AssignResult> {
  const locked = await c.query<{ seats: number | null }>(
    `SELECT seats FROM org_product WHERE org_id = $1 AND product_id = $2 FOR UPDATE`,
    [a.orgId, a.productId],
  );
  const op = locked.rows[0];
  if (!op) return { ok: false, reason: 'no_product' };
  const current = await c.query<{ role: Role }>(
    `SELECT role FROM member_product_role WHERE org_id = $1 AND user_id = $2 AND product_id = $3`,
    [a.orgId, a.userId, a.productId],
  );
  const from = current.rows[0]?.role ?? null;
  if (a.role === null) {
    await c.query(`DELETE FROM member_product_role WHERE org_id = $1 AND user_id = $2 AND product_id = $3`, [a.orgId, a.userId, a.productId]);
    return { ok: true, from, to: null };
  }
  if (from === null && op.seats !== null) {
    const used = await c.query<{ n: number }>(
      `SELECT count(*)::int AS n FROM member_product_role WHERE org_id = $1 AND product_id = $2`,
      [a.orgId, a.productId],
    );
    const n = used.rows[0]?.n ?? 0;
    if (n >= op.seats) return { ok: false, reason: 'seat_limit', seats: op.seats, used: n };
  }
  if (from !== a.role) {
    await c.query(
      `INSERT INTO member_product_role (org_id, user_id, product_id, role, granted_by) VALUES ($1, $2, $3, $4, $5)
       ON CONFLICT (org_id, user_id, product_id)
       DO UPDATE SET role = EXCLUDED.role, granted_by = EXCLUDED.granted_by, granted_at = now()`,
      [a.orgId, a.userId, a.productId, a.role, a.grantedBy],
    );
  }
  return { ok: true, from, to: a.role };
}

/**
 * Webhook rule: the first member of an org that already holds live products becomes owner of each of
 * them (seats permitting). Everyone else starts with no product role until an org owner or admin
 * assigns one (fail closed). Returns the products granted; each grant is audit-logged.
 */
export async function grantFirstMemberOwner(c: Queryable, orgId: string, userId: string): Promise<string[]> {
  // One org's membership webhooks take turns here, so two members joining at once cannot both see an
  // otherwise empty org and both become owner; the check below then sees the other's committed row.
  await c.query(`SELECT pg_advisory_xact_lock(hashtext($1))`, [`first_member:${orgId}`]);
  const others = await c.query(`SELECT 1 FROM membership WHERE org_id = $1 AND user_id <> $2 LIMIT 1`, [orgId, userId]);
  if ((others.rowCount ?? 0) > 0) return [];
  const live = await c.query<{ product_id: string }>(
    `SELECT op.product_id FROM org_product op JOIN product p ON p.id = op.product_id
     WHERE op.org_id = $1 AND ${LIVE_SQL} ORDER BY op.product_id`,
    [orgId],
  );
  const granted: string[] = [];
  for (const { product_id: productId } of live.rows) {
    const res = await assignProductRole(c, { orgId, userId, productId, role: 'owner', grantedBy: null });
    if (!res.ok) continue;
    granted.push(productId);
    await audit(c, {
      orgId, userId: null, action: 'member.product_role', target: `user:${userId}`,
      meta: { productId, from: res.from, to: 'owner', rule: 'first_member' },
    });
  }
  return granted;
}
