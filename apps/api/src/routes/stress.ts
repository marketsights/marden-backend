// Stress (spec 09, 18): runs go to the stress model; saved scenarios are org-owned.
import type { FastifyInstance } from 'fastify';
import { z } from 'zod';
import { authOf } from '../auth/plugin.js';
import type { Db } from '../db.js';
import { badRequest, notFound, notReady } from '../errors.js';
import { stressRequestSchema } from '../lib/defaults.js';
import { asOfOf } from '../lib/params.js';
import { audit } from '../queries/audit.js';
import {
  getSaved, insertSaved, isPendingStress, isStressError, listSaved, runStress, runStressWaiting, stressModelAvailable,
} from '../queries/stress.js';
import { param, parseBody } from './serve.js';

const saveSchema = z.strictObject({ name: z.string().trim().min(1).max(120), request: stressRequestSchema });

export function stressRoutes(app: FastifyInstance, db: Db): void {
  // The API never computes a stress result itself: until the model is deployed this answers 503. A saved
  // scenario answers at once; a custom run is queued by stress_run and polled here for a bounded time,
  // then answered 202 {status, jobId} (the client repeats the same POST to collect it).
  app.post('/api/stress/run', async (req, reply) => {
    const request = parseBody(stressRequestSchema, req.body);
    if (!(await stressModelAvailable(db))) throw notReady('model:stress', request.scenario);
    const result = await runStressWaiting(db, request);
    if (result === null) throw notReady('model:stress', request.scenario);
    if (isStressError(result)) throw badRequest('invalid_stress_request', result.error);
    if (isPendingStress(result)) return reply.code(202).send(result);
    return result;
  });

  app.post('/api/stress/saved', async (req, reply) => {
    const auth = authOf(req);
    const input = parseBody(saveSchema, req.body);
    // Store the model's result with the scenario when it already has one (one call, no waiting), so
    // Home's "Saved stress result" shows it at once; otherwise result stays null until the model runs it.
    const result = (await stressModelAvailable(db)) ? await runStress(db, input.request) : null;
    const saved = await db.withOrg(auth.orgId, async (c) => {
      const row = await insertSaved(c, auth, { ...input, result });
      await audit(c, {
        orgId: auth.orgId, userId: auth.userId, action: 'stress.save', target: `stress_saved:${row.id}`, ip: req.ip,
        meta: { name: input.name, scenario: input.request.scenario },
      });
      return row;
    });
    return reply.code(201).send(saved);
  });

  app.get('/api/stress/saved', async (req) => {
    const { orgId } = authOf(req);
    return db.withOrg(orgId, (c) => listSaved(c, orgId, asOfOf(req)));
  });

  app.get('/api/stress/saved/:id', async (req) => {
    const { orgId } = authOf(req);
    const saved = await db.withOrg(orgId, (c) => getSaved(c, orgId, param(req, 'id')));
    if (!saved) throw notFound('stress_saved');
    return saved;
  });
}
