// The view/key contract (docs/ARCHITECTURE.md "View and key table"): every GET route is either a
// read-model route, whose 503 names a documented view and the expected key, or an app-table route.
// Together the read-model routes and exports serve every documented view.
import { readFileSync } from 'node:fs';
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { ROUTE_RULES } from '../src/auth/permissions.js';
import type { Db } from '../src/db.js';
import { EXPORT_TABLES } from '../src/lib/export-tables.js';
import { makeApp, testDb } from './helpers.js';

const ARCHITECTURE = new URL('../../../docs/ARCHITECTURE.md', import.meta.url);

/** view -> key kind, parsed the same way as services/tests/test_readmodels.py. */
function documentedViews(): Map<string, string> {
  const text = readFileSync(ARCHITECTURE, 'utf8');
  const section = text.split('### View and key table')[1]?.split('\n## ')[0] ?? '';
  const views = new Map<string, string>();
  for (const line of section.split('\n')) {
    const cells = line.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
    if (cells.length < 3 || !cells[0]?.startsWith('`')) continue;
    const key = (cells[1] ?? '').replace(/`/g, '');
    const names = [...(cells[0] ?? '').matchAll(/`([^`]+)`/g)].map((m) => m[1] ?? '');
    const [head = '', ...rest] = names;
    if (head.includes('<tab>')) for (const tab of rest) views.set(head.replace('<tab>', tab), key);
    else for (const name of names) views.set(name, key);
  }
  return views;
}

const SITE = 'vw-site';
const INST = 'VW-INST';
const CAMPUS = 'vw-campus';
const PLANT = 'vw-plant';
const ENTITY = 'vw-entity';
const GPU = 'vw-gpu';

type Expect = [view: string, key: string];

/** Route pattern -> [concrete url, expected 503 view, key]. */
const READ_MODEL_ROUTES: Record<string, [string, ...Expect]> = {
  '/api/home': ['/api/home', 'home', 'all'],
  '/api/completion': ['/api/completion', 'completion', 'all'],
  '/api/sites': ['/api/sites', 'sites', 'all'],
  '/api/sites/:slug': [`/api/sites/${SITE}`, 'site_detail', SITE],
  '/api/sites/:slug/schedule': [`/api/sites/${SITE}/schedule`, 'site_schedule', SITE],
  ...Object.fromEntries(['evidence', 'power', 'opposition', 'events', 'debt', 'lease', 'buildings'].map(
    (tab) => [`/api/sites/:slug/${tab}`, [`/api/sites/${SITE}/${tab}`, `site_tab:${tab}`, SITE]],
  )),
  '/api/sites/:slug/project-file': [`/api/sites/${SITE}/project-file`, 'project_file', SITE],
  '/api/instruments': ['/api/instruments', 'instruments', 'all'],
  '/api/curve': ['/api/curve', 'curve', 'all'],
  '/api/instruments/:id': [`/api/instruments/${INST}`, 'instrument_detail', INST],
  '/api/instruments/:id/price-history': [`/api/instruments/${INST}/price-history`, 'instrument_detail', INST],
  '/api/instruments/:id/holders': [`/api/instruments/${INST}/holders`, 'instrument_detail', INST],
  '/api/instruments/:id/collateral': [`/api/instruments/${INST}/collateral`, 'gpu_collateral', INST],
  '/api/payers': ['/api/payers', 'payers', 'all'],
  '/api/payers/:slug': [`/api/payers/${ENTITY}`, 'payer', ENTITY],
  '/api/payers/:slug/capacity': [`/api/payers/${ENTITY}/capacity`, 'payer_capacity', ENTITY],
  '/api/risk-list': ['/api/risk-list?week=2026-10-19', 'risk_list', '2026-10-19'],
  '/api/track-record': ['/api/track-record', 'track_record', 'all'],
  '/api/data-centers': ['/api/data-centers', 'dc_row', '*'],
  '/api/data-centers/summary': ['/api/data-centers/summary', 'dc_row', '*'],
  '/api/data-centers/:id': [`/api/data-centers/${CAMPUS}`, 'dc_detail', CAMPUS],
  '/api/data-centers/:id/row': [`/api/data-centers/${CAMPUS}/row`, 'dc_row', CAMPUS],
  ...Object.fromEntries(['buildings', 'records', 'history', 'capacity'].map(
    (tab) => [`/api/data-centers/:id/${tab}`, [`/api/data-centers/${CAMPUS}/${tab}`, `dc_tab:${tab}`, CAMPUS]],
  )),
  '/api/power-plants': ['/api/power-plants', 'plant_row', '*'],
  '/api/power-plants/summary': ['/api/power-plants/summary', 'plant_row', '*'],
  '/api/power-plants/:id': [`/api/power-plants/${PLANT}`, 'plant_detail', PLANT],
  '/api/power-plants/:id/row': [`/api/power-plants/${PLANT}/row`, 'plant_row', PLANT],
  '/api/power-plants/:id/debt': [`/api/power-plants/${PLANT}/debt`, 'plant_debt', PLANT],
  '/api/obligations': ['/api/obligations', 'obligations', 'all'],
  '/api/obligations/:company': [`/api/obligations/${ENTITY}`, 'obligation_company', ENTITY],
  '/api/gpus': ['/api/gpus', 'gpus', 'all'],
  '/api/gpus/deployment': ['/api/gpus/deployment', 'gpu_deployment', 'all'],
  '/api/gpus/:model': [`/api/gpus/${GPU}`, 'gpu_model', GPU],
  '/api/leases': ['/api/leases', 'leases', 'all'],
  '/api/leases/:id': ['/api/leases/L1', 'leases', 'all'],
};

/** Export table -> expected 503 view and key (null: an app table, answered 200). */
const EXPORTS: Record<string, Expect | null> = {
  'data-centers': ['dc_row', '*'], 'power-plants': ['plant_row', '*'], completion: ['completion', 'all'],
  sites: ['sites', 'all'], site: ['site_schedule', SITE], 'data-center': ['dc_detail', CAMPUS], instruments: ['instruments', 'all'], holders: ['instrument_detail', INST],
  'risk-list': ['risk_list', 'latest'], payers: ['payers', 'all'], payer: ['payer', ENTITY], 'payer-exposed': ['payer_capacity', ENTITY],
  leases: ['leases', 'all'], 'lease-reconciliation': ['leases', 'all'],
  gpus: ['gpus', 'all'], 'gpu-models': ['gpus', 'all'], 'gpu-deployment': ['gpu_deployment', 'all'], 'gpu-model': ['gpu_model', 'H100'],
  obligations: ['obligations', 'all'], watchlist: null,
  'track-record': ['track_record', 'all'], models: ['track_record', 'all'], sources: null,
  stress: ['model:stress', 'power_delay'], // a run of the stress model (stress_run), not a read model
};

/** GET routes that read app tables, identity tables or the stream, not a read model. */
const APP_ROUTES = new Set([
  '/api/home/layout', '/api/stress/saved', '/api/stress/saved/:id', '/api/watchlist', '/api/watchlist/issuers/:id', '/api/alerts',
  '/api/alert-settings', '/api/saved-views', '/api/sources/health', '/api/account', '/api/search', '/api/stream', '/api/me',
  '/api/export/:table.xlsx', '/healthz',
  // alert email delivery: email_outbox / email_provider_state, and the signed unsubscribe link
  '/api/alert-delivery', '/api/email/unsubscribe',
  // staff document agents: app tables (agent_job, agent_upload, review_item), not read models
  '/api/documents', '/api/documents/:id', '/api/uploads/:id', '/api/agents/status', '/api/review', '/api/review/:id',
  // Data centers map layer: ISO/RTO boundaries straight from grid_region (HIFLD), not a read model
  '/api/grid/iso-rto',
]);

describe('view/key contract', () => {
  let db: Db;
  let app: FastifyInstance;
  const documented = documentedViews();

  beforeAll(async () => {
    db = testDb();
    app = await makeApp({ mode: 'dev', db });
    await db.query(`DELETE FROM read_model`);
    await db.query(`INSERT INTO site (slug, name) VALUES ($1, 'View test site')`, [SITE]);
    await db.query(`INSERT INTO instrument (id, name, kind, issuer_name) VALUES ($1, 'View test loan', 'term_loan', 'Issuer')`, [INST]);
    await db.query(`INSERT INTO campus (id, name) VALUES ($1, 'View test campus')`, [CAMPUS]);
    await db.query(`INSERT INTO power_plant (id, name) VALUES ($1, 'View test plant')`, [PLANT]);
    await db.query(`INSERT INTO entity (id, name, kind) VALUES ($1, 'View test payer', 'company')`, [ENTITY]);
  });
  afterAll(async () => {
    await db.query(`DELETE FROM entity WHERE id = $1`, [ENTITY]);
    await db.query(`DELETE FROM power_plant WHERE id = $1`, [PLANT]);
    await db.query(`DELETE FROM campus WHERE id = $1`, [CAMPUS]);
    await db.query(`DELETE FROM instrument WHERE id = $1`, [INST]);
    await db.query(`DELETE FROM site WHERE slug = $1`, [SITE]);
    await app.close();
    await db.close();
  });

  it('parses the documented table', () => {
    expect(documented.size).toBeGreaterThan(30);
    expect(documented.get('site_tab:debt')).toBe('site.slug');
    expect(documented.get('dc_tab:capacity')).toBe('campus.id');
  });

  it('classifies every GET route', () => {
    const gets = Object.keys(ROUTE_RULES).filter((k) => k.startsWith('GET ')).map((k) => k.slice(4));
    const unclassified = gets.filter((path) => !(path in READ_MODEL_ROUTES) && !APP_ROUTES.has(path));
    expect(unclassified).toEqual([]);
    expect(Object.keys(EXPORT_TABLES).sort()).toEqual(Object.keys(EXPORTS).sort());
  });

  it('names only documented views, with the expected keys, in every not_ready answer', async () => {
    const served = new Set<string>();
    for (const [route, [url, view, key]] of Object.entries(READ_MODEL_ROUTES)) {
      const res = await app.inject({ method: 'GET', url });
      expect({ route, status: res.statusCode, body: res.json() }).toEqual({ route, status: 503, body: { error: 'not_ready', view, key } });
      expect(documented.has(view), `${view} is not in the ARCHITECTURE table`).toBe(true);
      served.add(view);
    }
    for (const [table, expected] of Object.entries(EXPORTS)) {
      const qs = table === 'holders' ? `?instrument=${INST}` : table === 'payer' || table === 'payer-exposed' ? `?slug=${ENTITY}`
        : table === 'site' ? `?slug=${SITE}`
        : table === 'data-center' ? `?id=${CAMPUS}`
        : table === 'gpu-model' ? '?model=H100'
        : table === 'stress' ? '?scenario=power_delay&months=6' : '';
      const res = await app.inject({ method: 'GET', url: `/api/export/${table}.xlsx${qs}` });
      if (expected === null) {
        expect(res.statusCode).toBe(200);
        continue;
      }
      expect({ table, body: res.json() }).toEqual({ table, body: { error: 'not_ready', view: expected[0], key: expected[1] } });
      served.add(expected[0]);
    }
    expect([...documented.keys()].filter((v) => !served.has(v))).toEqual([]);
  });

  it('keyed routes answer 404 for ids that are not in their identity table', async () => {
    for (const [route, [url, , key]] of Object.entries(READ_MODEL_ROUTES)) {
      const isIdentityKey = [SITE, INST, CAMPUS, PLANT, ENTITY].includes(key);
      if (!isIdentityKey) continue;
      const res = await app.inject({ method: 'GET', url: url.replace(key, `${key}-missing`) });
      expect({ route, status: res.statusCode }).toEqual({ route, status: 404 });
    }
  });
});
