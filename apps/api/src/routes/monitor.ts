// Home (spec 01), Completion monitor (spec 03), Risk list (spec 10), Track record (spec 12).
import type { FastifyInstance } from 'fastify';
import type { HomeDoc, HomeResponse } from '../contracts/app.js';
import type { CompletionResponse, RiskList } from '../contracts/types.js';
import type { Db } from '../db.js';
import { notReady } from '../errors.js';
import { authOf } from '../auth/plugin.js';
import { filterCompletion, wantsWatchlist } from '../lib/doc-filters.js';
import { cutHome } from '../lib/home-doc.js';
import { asOfOf, isoDate, queryOf } from '../lib/params.js';
import { unreadAlerts } from '../queries/alerts.js';
import { watchedRefs } from '../queries/home.js';
import { getAlertSettings } from '../queries/prefs.js';
import { watchedSiteSlugs } from '../queries/watchlist.js';
import { serveDoc } from './serve.js';

export function monitorRoutes(app: FastifyInstance, db: Db): void {
  // The home read model holds every name's pieces; the API keeps the org's watched names, applies the
  // caller's price threshold and adds the caller's unread alerts (lib/home-doc.ts).
  app.get('/api/home', async (req): Promise<HomeResponse> => {
    const auth = authOf(req);
    const asOf = asOfOf(req);
    const doc = await serveDoc<HomeDoc>(db, 'home', 'all', asOf);
    const [watched, alerts, settings] = await Promise.all([
      watchedRefs(db, auth.orgId),
      unreadAlerts(db, auth.orgId, asOf),
      getAlertSettings(db, { orgId: auth.orgId, userId: auth.userId }),
    ]);
    return cutHome(doc, watched, { priceThresholdPts: settings.priceThresholdPts, unreadAlerts: alerts, lastVisit: null });
  });

  app.get('/api/completion', async (req) => {
    const q = queryOf(req);
    const doc = await serveDoc<CompletionResponse>(db, 'completion', 'all', asOfOf(req));
    const watched = wantsWatchlist(q) ? await watchedSiteSlugs(db, authOf(req).orgId) : null;
    return { ...doc, rows: filterCompletion(doc.rows, q, watched) };
  });

  // Published lists are frozen per week: ?week=YYYY-MM-DD (week start); default = latest published.
  app.get('/api/risk-list', async (req) => {
    const asOf = asOfOf(req);
    const week = isoDate(queryOf(req), 'week') ?? (await db.latestKey('risk_list', asOf));
    if (!week) throw notReady('risk_list', 'latest');
    return serveDoc<RiskList>(db, 'risk_list', week, asOf);
  });

  app.get('/api/track-record', async (req) => serveDoc(db, 'track_record', 'all', asOfOf(req)));
}
