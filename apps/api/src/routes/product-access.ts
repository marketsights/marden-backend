// Per-product access (docs/access-control.md):
//   PATCH /api/account/members/:id/products         org owners and admins set or remove a member's role on
//                                                   one of the firm's products (seat limits hold)
//   POST  /api/admin/orgs/:orgId/products            staff give a firm a product (plan, seats, dates, status)
//   PATCH /api/admin/orgs/:orgId/products/:productId staff change it
// Every change writes audit_log.
import type { FastifyInstance } from 'fastify';
import { z } from 'zod';
import { authOf } from '../auth/plugin.js';
import { matrixFor, type ProductRegistry } from '../auth/products/index.js';
import type { OrgProduct, TeamMember } from '../contracts/app.js';
import type { Db, Queryable } from '../db.js';
import { badRequest, conflict, HttpError, notFound } from '../errors.js';
import { isMember, team } from '../queries/account.js';
import { audit } from '../queries/audit.js';
import { assignProductRole, orgProduct } from '../queries/products.js';
import { param, parseBody } from './serve.js';

export interface ProductAccessDeps { db: Db; products: ProductRegistry }

const ROLE = z.enum(['owner', 'admin', 'member', 'viewer']);
const PRODUCT_ID = z.string().min(1).max(64);
const assignSchema = z.strictObject({ productId: PRODUCT_ID, role: ROLE.nullable() });

const fields = {
  plan: z.enum(['trial', 'design_partner', 'paid']),
  seats: z.number().int().min(0).max(1_000_000).nullable(),   // null = unlimited
  startsOn: z.iso.date(),
  endsOn: z.iso.date().nullable(),
  status: z.enum(['active', 'suspended', 'ended']),
};
const createSchema = z.strictObject({
  productId: PRODUCT_ID, plan: fields.plan, seats: fields.seats, startsOn: fields.startsOn,
  endsOn: fields.endsOn.default(null), status: fields.status.default('active'),
});
const updateSchema = z.strictObject(fields).partial()
  .refine((b) => Object.keys(b).length > 0, { message: 'nothing to change' });

type Terms = Pick<OrgProduct, 'plan' | 'seats' | 'startsOn' | 'endsOn' | 'status'>;
const termsOf = (p: Terms): Terms => ({ plan: p.plan, seats: p.seats, startsOn: p.startsOn, endsOn: p.endsOn, status: p.status });

function checkDates(t: Terms): void {
  if (t.endsOn !== null && t.endsOn < t.startsOn) throw badRequest('ends_on_before_starts_on');
}

async function knownProduct(c: Queryable, products: ProductRegistry, productId: string): Promise<void> {
  const res = await c.query(`SELECT 1 FROM product WHERE id = $1`, [productId]);
  if ((res.rowCount ?? 0) === 0) throw notFound('product');
  // A product the code has no matrix for would grant nothing: refuse rather than sell an empty product.
  if (!matrixFor(products, productId)) throw badRequest('product_has_no_permission_matrix');
}

async function memberOf(db: Db, orgId: string, userId: string): Promise<TeamMember> {
  const member = (await team(db, orgId)).find((m) => m.userId === userId);
  if (!member) throw notFound('member');
  return member;
}

export function productAccessRoutes(app: FastifyInstance, deps: ProductAccessDeps): void {
  const { db } = deps;

  app.patch('/api/account/members/:id/products', async (req) => {
    const auth = authOf(req);
    const userId = param(req, 'id');
    const { productId, role } = parseBody(assignSchema, req.body);
    await db.withOrg(auth.orgId, async (c) => {
      if (!(await isMember(c, auth.orgId, userId))) throw notFound('member');
      const res = await assignProductRole(c, { orgId: auth.orgId, userId, productId, role, grantedBy: auth.userId });
      if (!res.ok && res.reason === 'no_product') throw notFound('product');
      if (!res.ok) throw new HttpError(409, 'conflict', { reason: 'seat_limit', seats: res.seats, used: res.used });
      if (res.from === res.to) return;
      await audit(c, {
        orgId: auth.orgId, userId: auth.userId, action: 'member.product_role', target: `user:${userId}`,
        ip: req.ip, meta: { productId, from: res.from, to: res.to },
      });
    });
    return memberOf(db, auth.orgId, userId);
  });

  app.post('/api/admin/orgs/:orgId/products', async (req, reply) => {
    const auth = authOf(req);
    const orgId = param(req, 'orgId');
    const body = parseBody(createSchema, req.body);
    const terms = termsOf(body);
    checkDates(terms);
    const created = await db.tx(async (c) => {
      const org = await c.query(`SELECT 1 FROM org WHERE id = $1`, [orgId]);
      if ((org.rowCount ?? 0) === 0) throw notFound('org');
      await knownProduct(c, deps.products, body.productId);
      const res = await c.query(
        `INSERT INTO org_product (org_id, product_id, plan, seats, status, starts_on, ends_on)
         VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (org_id, product_id) DO NOTHING`,
        [orgId, body.productId, terms.plan, terms.seats, terms.status, terms.startsOn, terms.endsOn],
      );
      if ((res.rowCount ?? 0) === 0) throw conflict('org_product_exists');
      await audit(c, {
        orgId, userId: auth.userId, action: 'org_product.create', target: `org_product:${orgId}:${body.productId}`,
        ip: req.ip, meta: { productId: body.productId, to: terms },
      });
      return orgProduct(c, orgId, body.productId);
    });
    return reply.code(201).send(created);
  });

  app.patch('/api/admin/orgs/:orgId/products/:productId', async (req) => {
    const auth = authOf(req);
    const orgId = param(req, 'orgId');
    const productId = param(req, 'productId');
    const body = parseBody(updateSchema, req.body);
    return db.tx(async (c) => {
      const locked = await c.query(`SELECT 1 FROM org_product WHERE org_id = $1 AND product_id = $2 FOR UPDATE`, [orgId, productId]);
      const before = (locked.rowCount ?? 0) > 0 ? await orgProduct(c, orgId, productId) : null;
      if (!before) throw notFound('org_product');
      const next: Terms = { ...termsOf(before), ...body };
      checkDates(next);
      if (next.seats !== null && next.seats < before.seatsUsed) {
        throw new HttpError(409, 'conflict', { reason: 'seats_in_use', seats: next.seats, used: before.seatsUsed });
      }
      await c.query(
        `UPDATE org_product SET plan = $3, seats = $4, status = $5, starts_on = $6, ends_on = $7
         WHERE org_id = $1 AND product_id = $2`,
        [orgId, productId, next.plan, next.seats, next.status, next.startsOn, next.endsOn],
      );
      await audit(c, {
        orgId, userId: auth.userId, action: 'org_product.update', target: `org_product:${orgId}:${productId}`,
        ip: req.ip, meta: { productId, from: termsOf(before), to: next },
      });
      return orgProduct(c, orgId, productId);
    });
  });
}
