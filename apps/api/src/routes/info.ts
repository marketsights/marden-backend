// Sidebar search, source heartbeats (spec 12) and the liveness probe.
import type { FastifyInstance } from 'fastify';
import type { SearchResponse } from '../contracts/app.js';
import type { Db } from '../db.js';
import { badRequest } from '../errors.js';
import { checkHealth } from '../lib/health.js';
import { asOfOf, first, likePattern, queryOf } from '../lib/params.js';
import { search } from '../queries/search.js';
import { sourcesHealth } from '../queries/sources.js';

export function infoRoutes(app: FastifyInstance, db: Db): void {
  app.get('/api/search', async (req): Promise<SearchResponse> => {
    const q = first(queryOf(req), 'q') ?? '';
    if (q.length < 2 || q.length > 100) throw badRequest('q must be 2 to 100 characters');
    const results = await search(db, q, { pattern: likePattern(q), asOf: asOfOf(req), perKind: 8, limit: 30 });
    return { q, results };
  });

  app.get('/api/sources/health', async (req) => sourcesHealth(db, asOfOf(req)));

  // 200 when the database answers, 503 otherwise (a deploy whose database is unreachable never
  // takes traffic; src/lib/health.ts).
  app.get('/healthz', async (req, reply) => {
    const health = await checkHealth(db, { onError: (err) => req.log.warn({ err }, 'healthz: database unreachable') });
    return reply.code(health.ok ? 200 : 503).header('cache-control', 'no-store').send(health);
  });
}
