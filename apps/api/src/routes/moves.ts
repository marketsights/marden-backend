// Moves (price and yield changes over 1D/1W/1M/QTD/YTD) and event effects on debt value
// (docs/methods/moves-v0.1.md, event-impact-v0.1.md). Read models moves, instrument_moves, site_debt_effects.
import type { FastifyInstance } from 'fastify';
import type { InstrumentMoves, MoveRow, MovesPage, SiteDebtEffects } from '../contracts/moves.js';
import type { Db } from '../db.js';
import { asOfOf, list, queryOf, type Query } from '../lib/params.js';
import { INSTRUMENT, SITE, keyed, param, serveDoc } from './serve.js';

const lower = (values: string[]) => new Set(values.map((v) => v.toLowerCase()));
const hit = (wanted: Set<string>, value: string | null | undefined) => wanted.size === 0 || (value != null && wanted.has(value.toLowerCase()));

/** ?type=&site=&tenant=&endUser= (each a list; site matches the slug or the name). */
export function filterMoves(rows: MoveRow[], q: Query): MoveRow[] {
  const type = lower(list(q, 'type'));
  const site = lower(list(q, 'site'));
  const tenant = lower(list(q, 'tenant'));
  const endUser = lower(list(q, 'endUser'));
  return rows.filter((r) =>
    hit(type, r.type) && (site.size === 0 || hit(site, r.site?.slug) || hit(site, r.site?.name))
    && hit(tenant, r.tenant) && hit(endUser, r.endUser));
}

export function moveRoutes(app: FastifyInstance, db: Db): void {
  app.get('/api/moves', async (req) => {
    const doc = await serveDoc<MovesPage>(db, 'moves', 'all', asOfOf(req));
    return { ...doc, rows: filterMoves(doc.rows, queryOf(req)) };
  });

  app.get('/api/instruments/:id/moves', async (req) =>
    serveDoc<InstrumentMoves>(db, 'instrument_moves', keyed(INSTRUMENT, param(req, 'id')), asOfOf(req)));

  app.get('/api/sites/:slug/debt-effects', async (req) =>
    serveDoc<SiteDebtEffects>(db, 'site_debt_effects', keyed(SITE, param(req, 'slug')), asOfOf(req)));
}
