// Watchlist and alerts (spec 11). Org-owned rows: every query is scoped to the caller's org,
// another org's ids answer 404, and every change is audit-logged.
import type { FastifyInstance } from 'fastify';
import { z } from 'zod';
import { authOf } from '../auth/plugin.js';
import type { Db } from '../db.js';
import { notFound } from '../errors.js';
import { asOfOf, flag, num, queryOf } from '../lib/params.js';
import { toIssuerCandidate, toWatchItem } from '../lib/watch-items.js';
import { listAlerts, markAlertsRead } from '../queries/alerts.js';
import { audit } from '../queries/audit.js';
import { addWatchItem, issuerCandidates, refExists, removeWatchItem, watchRows } from '../queries/watchlist.js';
import { param, parseBody } from './serve.js';

const itemSchema = z.strictObject({ kind: z.enum(['site', 'instrument']), refId: z.string().min(1).max(200) });
const addSchema = z.union([itemSchema, z.strictObject({ items: z.array(itemSchema).min(1).max(200) })]);
const readSchema = z.strictObject({ ids: z.array(z.string().min(1).max(40)).min(1).max(500).optional() });

export function watchlistRoutes(app: FastifyInstance, db: Db): void {
  app.get('/api/watchlist', async (req) => {
    const { orgId } = authOf(req);
    const rows = await db.withOrg(orgId, (c) => watchRows(c, orgId, asOfOf(req)));
    return rows.map(toWatchItem);
  });

  // Adding an issuer adds its instruments and the sites they depend on; the user can uncheck
  // names before POST /api/watchlist/items { items } (spec 11 Acceptance).
  app.get('/api/watchlist/issuers/:id', async (req) => {
    const { orgId } = authOf(req);
    const id = param(req, 'id');
    const found = await db.withOrg(orgId, (c) => issuerCandidates(c, orgId, id));
    if (!found.issuer) throw notFound('issuer');
    return { issuer: found.issuer, items: found.rows.map(toIssuerCandidate) };
  });

  app.post('/api/watchlist/items', async (req, reply) => {
    const auth = authOf(req);
    const body = parseBody(addSchema, req.body);
    const items = 'items' in body ? body.items : [body];
    const added = await db.withOrg(auth.orgId, async (c) => {
      const out = [];
      for (const item of items) {
        if (!(await refExists(c, item.kind, item.refId))) throw notFound(item.kind);
        const res = await addWatchItem(c, auth, item);
        if (res.created) {
          await audit(c, { orgId: auth.orgId, userId: auth.userId, action: 'watchlist.add', target: `${item.kind}:${item.refId}`, ip: req.ip });
        }
        out.push({ id: res.id, kind: item.kind, refId: item.refId, created: res.created });
      }
      return out;
    });
    return reply.code(201).send({ items: added });
  });

  app.delete('/api/watchlist/items/:id', async (req, reply) => {
    const auth = authOf(req);
    const id = param(req, 'id');
    await db.withOrg(auth.orgId, async (c) => {
      const removed = await removeWatchItem(c, auth.orgId, id);
      if (!removed) throw notFound('watchlist_item');
      await audit(c, {
        orgId: auth.orgId, userId: auth.userId, action: 'watchlist.remove',
        target: `${removed.kind}:${removed.refId}`, ip: req.ip, meta: { id },
      });
    });
    return reply.code(204).send();
  });

  app.get('/api/alerts', async (req) => {
    const { orgId, userId } = authOf(req);
    const q = queryOf(req);
    const limit = Math.min(Math.max(Math.trunc(num(q, 'limit') ?? 100), 1), 500);
    return db.withOrg(orgId, (c) => listAlerts(c, orgId, userId, { asOf: asOfOf(req), unreadOnly: flag(q, 'unread'), limit }));
  });

  app.post('/api/alerts/read', async (req, reply) => {
    const auth = authOf(req);
    const { ids } = parseBody(readSchema, req.body ?? {});
    await db.withOrg(auth.orgId, async (c) => {
      const changed = await markAlertsRead(c, auth.orgId, auth.userId, ids ?? null);
      await audit(c, {
        orgId: auth.orgId, userId: auth.userId, action: 'alerts.read', ip: req.ip,
        meta: { ids: changed, all: ids === undefined },
      });
    });
    return reply.code(204).send();
  });
}
