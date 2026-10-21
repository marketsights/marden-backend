// Saved views (spec 03 "Saved views"; also specs 14, 15): GET /api/saved-views?screen= lists the
// caller's views of one screen, POST saves the current filters under a name (same name: replaced),
// DELETE removes one. Per user within the org; the stored query is the screen's URL query string.
import type { FastifyInstance } from 'fastify';
import { z } from 'zod';
import { authOf } from '../auth/plugin.js';
import type { Db } from '../db.js';
import { badRequest, conflict, notFound } from '../errors.js';
import { first, queryOf } from '../lib/params.js';
import { audit } from '../queries/audit.js';
import { deleteSavedView, listSavedViews, upsertSavedView } from '../queries/saved-views.js';
import { param, parseBody } from './serve.js';

/** List screens whose header has "Saved views" (specs 03, 14, 15). */
export const SAVED_VIEW_SCREENS = ['completion', 'data-centers', 'power'] as const;
export const MAX_SAVED_VIEWS = 50;
/** Parameters a view never keeps: the snapshot date is a moment, not a view. */
const DROPPED = new Set(['asOf']);

const screenSchema = z.enum(SAVED_VIEW_SCREENS);

/** 'tenant=Oracle&sort=-debt' as stored: no '?', no empty values, no asOf, parameters in a stable order. */
export function normalizeQuery(raw: string): string {
  const params = new URLSearchParams(raw.replace(/^\?/, ''));
  const kept = [...params].filter(([k, v]) => v.trim() !== '' && !DROPPED.has(k));
  kept.sort(([a], [b]) => a.localeCompare(b));
  return new URLSearchParams(kept).toString();
}

const saveSchema = z.object({
  screen: screenSchema,
  name: z.string().trim().min(1).max(80),
  query: z.string().max(2000),
});

export function savedViewRoutes(app: FastifyInstance, db: Db): void {
  app.get('/api/saved-views', async (req) => {
    const auth = authOf(req);
    const screen = screenSchema.safeParse(first(queryOf(req), 'screen'));
    if (!screen.success) throw badRequest(`screen must be one of ${SAVED_VIEW_SCREENS.join(', ')}`);
    return db.withOrg(auth.orgId, (c) => listSavedViews(c, auth, screen.data));
  });

  app.post('/api/saved-views', async (req, reply) => {
    const auth = authOf(req);
    const input = parseBody(saveSchema, req.body);
    const query = normalizeQuery(input.query);
    const { view, created } = await db.withOrg(auth.orgId, async (c) => {
      const existing = await listSavedViews(c, auth, input.screen);
      if (!existing.some((v) => v.name === input.name) && existing.length >= MAX_SAVED_VIEWS) {
        throw conflict('saved_view_limit');
      }
      const saved = await upsertSavedView(c, auth, { screen: input.screen, name: input.name, query });
      await audit(c, {
        orgId: auth.orgId, userId: auth.userId, action: 'saved_view.save', target: `saved_view:${saved.view.id}`, ip: req.ip,
        meta: { screen: input.screen, name: input.name, query },
      });
      return saved;
    });
    return reply.code(created ? 201 : 200).send(view);
  });

  app.delete('/api/saved-views/:id', async (req, reply) => {
    const auth = authOf(req);
    const id = param(req, 'id');
    await db.withOrg(auth.orgId, async (c) => {
      const removed = await deleteSavedView(c, auth, id);
      if (!removed) throw notFound('saved_view');
      await audit(c, {
        orgId: auth.orgId, userId: auth.userId, action: 'saved_view.delete', target: `saved_view:${id}`, ip: req.ip,
        meta: { screen: removed.screen, name: removed.name },
      });
    });
    return reply.code(204).send();
  });
}
