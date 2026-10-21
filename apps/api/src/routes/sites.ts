// Sites (spec 05), Site detail and its tabs (spec 04, 16), project file (spec 26).
import type { FastifyInstance } from 'fastify';
import type { SiteSummary } from '../contracts/types.js';
import type { Db } from '../db.js';
import { filterSites } from '../lib/doc-filters.js';
import { asOfOf, queryOf } from '../lib/params.js';
import { keyed, param, serveDoc, SITE } from './serve.js';

export const SITE_TABS = ['evidence', 'power', 'opposition', 'events', 'debt', 'lease', 'buildings'] as const;

export function siteRoutes(app: FastifyInstance, db: Db): void {
  app.get('/api/sites', async (req) => {
    const rows = await serveDoc<SiteSummary[]>(db, 'sites', 'all', asOfOf(req));
    return filterSites(rows, queryOf(req));
  });

  app.get('/api/sites/:slug', async (req) => serveDoc(db, 'site_detail', keyed(SITE, param(req, 'slug')), asOfOf(req)));

  app.get('/api/sites/:slug/schedule', async (req) =>
    serveDoc(db, 'site_schedule', keyed(SITE, param(req, 'slug')), asOfOf(req)));

  for (const tab of SITE_TABS) {
    app.get(`/api/sites/:slug/${tab}`, async (req) => serveDoc(db, `site_tab:${tab}`, keyed(SITE, param(req, 'slug')), asOfOf(req)));
  }

  app.get('/api/sites/:slug/project-file', async (req) =>
    serveDoc(db, 'project_file', keyed(SITE, param(req, 'slug')), asOfOf(req)));
}
