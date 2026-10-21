// Obligation map (spec 18), GPU index and deployment (spec 19), lease map (spec 25).
import type { FastifyInstance } from 'fastify';
import type { LeaseMapPage, ObligationGraph } from '../contracts/types.js';
import type { Db } from '../db.js';
import { badRequest, notFound } from '../errors.js';
import { filterObligations } from '../lib/doc-filters.js';
import { asOfOf, first, queryOf } from '../lib/params.js';
import { ENTITY, keyed, param, serveDoc } from './serve.js';

export function marketRoutes(app: FastifyInstance, db: Db): void {
  app.get('/api/obligations', async (req) => {
    const q = queryOf(req);
    const format = first(q, 'format') ?? 'graph';
    if (format !== 'graph' && format !== 'table') throw badRequest('format must be graph or table');
    const graph = filterObligations(await serveDoc<ObligationGraph>(db, 'obligations', 'all', asOfOf(req)), q);
    return format === 'table' ? { asOf: graph.asOf, kpis: graph.kpis, edges: graph.edges } : graph;
  });

  app.get('/api/obligations/:company', async (req) =>
    serveDoc(db, 'obligation_company', keyed(ENTITY, param(req, 'company')), asOfOf(req)));

  app.get('/api/gpus', async (req) => serveDoc(db, 'gpus', 'all', asOfOf(req)));

  app.get('/api/gpus/deployment', async (req) => serveDoc(db, 'gpu_deployment', 'all', asOfOf(req)));

  app.get('/api/gpus/:model', async (req) => serveDoc(db, 'gpu_model', param(req, 'model'), asOfOf(req)));

  app.get('/api/leases', async (req) => serveDoc(db, 'leases', 'all', asOfOf(req)));

  // One lease from the same lease-map document, so the side panel matches the map.
  app.get('/api/leases/:id', async (req) => {
    const id = param(req, 'id');
    const page = await serveDoc<LeaseMapPage>(db, 'leases', 'all', asOfOf(req));
    const lease = page.leases.find((l) => l.id === id);
    if (!lease) throw notFound('lease');
    return lease;
  });
}
