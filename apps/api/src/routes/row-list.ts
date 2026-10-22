// Shared list handler for row read models: filters and sort in SQL, 50 rows a page, or every
// filtered row as GeoJSON for the map (?format=geojson).
import type { FastifyReply, FastifyRequest } from 'fastify';
import { authOf } from '../auth/plugin.js';
import type { Db } from '../db.js';
import { badRequest, notReady } from '../errors.js';
import { toFeatureCollection } from '../lib/geojson.js';
import { asOfOf, first, page, queryOf } from '../lib/params.js';
import type { RowViewDef } from '../lib/row-query.js';
import { queryRows, type RowScope } from '../queries/rows.js';

/** Upper bound for one map response (spec 14: 5,000 points render without lag). */
export const GEOJSON_MAX_ROWS = 10_000;

export const rowScope = (req: FastifyRequest): RowScope => ({
  q: queryOf(req),
  orgId: authOf(req).orgId,
  asOf: asOfOf(req),
  hidden: req.server.hiddenSources,
});

export async function listRows(db: Db, def: RowViewDef, req: FastifyRequest, reply: FastifyReply): Promise<unknown> {
  const scope = rowScope(req);
  const format = first(scope.q, 'format') ?? 'json';
  if (format !== 'json' && format !== 'geojson') throw badRequest('format must be json or geojson');
  const p = page(scope.q);
  const window = format === 'geojson'
    ? { limit: GEOJSON_MAX_ROWS, offset: 0 }
    : { limit: def.pageSize, offset: (p - 1) * def.pageSize };
  const res = await queryRows(db, def, scope, window);
  if (res.available === 0) throw notReady(def.view, '*');
  if (format === 'geojson') {
    reply.type('application/geo+json');
    return toFeatureCollection(res.rows, { asOf: res.asOf, total: res.total });
  }
  return { asOf: res.asOf, total: res.total, page: p, pageSize: def.pageSize, rows: res.rows };
}
