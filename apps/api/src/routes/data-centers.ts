// Data centers (spec 14, 27) and On-site power (spec 15, 20).
import type { FastifyInstance } from 'fastify';
import type { DcRecord } from '../contracts/types.js';
import type { Db } from '../db.js';
import { notReady } from '../errors.js';
import { filterRecords } from '../lib/doc-filters.js';
import { asOfOf, queryOf } from '../lib/params.js';
import { DC_ROWS, PLANT_ROWS } from '../lib/row-views.js';
import { dataCenterSummary, powerPlantSummary } from '../queries/row-summaries.js';
import { listRows, rowScope } from './row-list.js';
import { CAMPUS, keyed, param, PLANT, serveDoc } from './serve.js';

export const CAMPUS_TABS = ['buildings', 'records', 'history', 'capacity'] as const;

export function dataCenterRoutes(app: FastifyInstance, db: Db): void {
  app.get('/api/data-centers', async (req, reply) => listRows(db, DC_ROWS, req, reply));

  app.get('/api/data-centers/summary', async (req) => {
    const { available, ...summary } = await dataCenterSummary(db, rowScope(req));
    if (available === 0) throw notReady(DC_ROWS.view, '*');
    return summary;
  });

  app.get('/api/data-centers/:id', async (req) => serveDoc(db, 'dc_detail', keyed(CAMPUS, param(req, 'id')), asOfOf(req)));

  for (const tab of CAMPUS_TABS) {
    app.get(`/api/data-centers/:id/${tab}`, async (req) => {
      const doc = await serveDoc<unknown>(db, `dc_tab:${tab}`, keyed(CAMPUS, param(req, 'id')), asOfOf(req));
      return tab === 'records' && Array.isArray(doc) ? filterRecords(doc as DcRecord[], queryOf(req)) : doc;
    });
  }
}

export function powerPlantRoutes(app: FastifyInstance, db: Db): void {
  app.get('/api/power-plants', async (req, reply) => listRows(db, PLANT_ROWS, req, reply));

  app.get('/api/power-plants/summary', async (req) => {
    const { available, ...summary } = await powerPlantSummary(db, rowScope(req));
    if (available === 0) throw notReady(PLANT_ROWS.view, '*');
    return summary;
  });

  app.get('/api/power-plants/:id', async (req) => serveDoc(db, 'plant_detail', keyed(PLANT, param(req, 'id')), asOfOf(req)));

  app.get('/api/power-plants/:id/debt', async (req) => serveDoc(db, 'plant_debt', keyed(PLANT, param(req, 'id')), asOfOf(req)));
}
