// Builds the Fastify app: security headers, CORS, request ids, JSON logs, auth + permission matrix,
// per-user rate limits, every /api route and the SSE stream.
import { randomUUID } from 'node:crypto';
import cors from '@fastify/cors';
import helmet from '@fastify/helmet';
import multipart from '@fastify/multipart';
import rateLimit from '@fastify/rate-limit';
import Fastify, { type FastifyError, type FastifyInstance } from 'fastify';
import { currentHolder, ensureDevIdentity } from './auth/access.js';
import { createClerkAdmin, type ClerkAdmin } from './auth/clerk-admin.js';
import { createClerkVerifier } from './auth/clerk.js';
import { decide } from './auth/grants.js';
import { ROUTE_RULES } from './auth/permissions.js';
import { registerAuth, trackRoutes } from './auth/plugin.js';
import { PRODUCTS, type ProductRegistry } from './auth/products/index.js';
import { assertDevAuthAllowed, type AppConfig } from './config.js';
import { createDb, type Db } from './db.js';
import { HttpError } from './errors.js';
import { hiddenSources } from './lib/licensing.js';
import { loggerOptions } from './lib/log-redact.js';
import { asOfOf } from './lib/params.js';
import { accountRoutes } from './routes/account.js';
import { agentRoutes, uploadMaxBytes } from './routes/agents.js';
import { alertDeliveryRoutes } from './routes/alert-delivery.js';
import { dataCenterRoutes, powerPlantRoutes } from './routes/data-centers.js';
import { exportRoutes } from './routes/export.js';
import { infoRoutes } from './routes/info.js';
import { instrumentRoutes } from './routes/instruments.js';
import { marketRoutes } from './routes/market.js';
import { monitorRoutes } from './routes/monitor.js';
import { moveRoutes } from './routes/moves.js';
import { prefsRoutes } from './routes/prefs.js';
import { savedViewRoutes } from './routes/saved-views.js';
import { productAccessRoutes } from './routes/product-access.js';
import { siteRoutes } from './routes/sites.js';
import { stressRoutes } from './routes/stress.js';
import { watchlistRoutes } from './routes/watchlist.js';
import { webhookRoutes } from './routes/webhooks.js';
import { StreamHub, streamRoutes } from './stream.js';

declare module 'fastify' {
  interface FastifyInstance {
    db: Db;
    streams: StreamHub;
    /** Source names the API's own fact reads leave out (MARDEN_SHOW_UNLICENSED_PRICES; lib/licensing.ts). */
    hiddenSources: readonly string[];
  }
}

export interface AppDeps {
  config: AppConfig;
  db?: Db;
  clerkAdmin?: ClerkAdmin | null;
  /** Product matrices (default PRODUCTS); tests pass more products. */
  products?: ProductRegistry;
  /** Extra routes (tests use this to prove an unmapped route stops startup). */
  extraRoutes?: (app: FastifyInstance) => void;
}

const REQUEST_ID = /^[A-Za-z0-9._:-]{1,64}$/;

/** A hop count as the function Fastify itself builds from a number (trust the n nearest proxies). */
function trustProxyOption(v: boolean | number): boolean | ((address: string, hop: number) => boolean) {
  return typeof v === 'number' ? (_address, hop) => hop < v : v;
}
const STREAM_RULE = ROUTE_RULES['GET /api/stream']!;

function errorBody(err: FastifyError | HttpError): { status: number; body: Record<string, unknown> } {
  if (err instanceof HttpError) return { status: err.status, body: { error: err.code, ...err.details } };
  const status = err.statusCode ?? 500;
  if (status >= 500) return { status: 500, body: { error: 'internal' } };
  if (status === 429) return { status, body: { error: 'rate_limited' } };
  return { status, body: { error: status === 404 ? 'not_found' : 'bad_request', reason: err.message } };
}

interface RouteDeps {
  db: Db; clerkAdmin: ClerkAdmin | null; devMode: boolean; hub: StreamHub; webhookSecret: string | null; products: ProductRegistry;
  alertLinkSecret: string | null;
}

function registerRoutes(app: FastifyInstance, r: RouteDeps): void {
  const { db } = r;
  monitorRoutes(app, db);
  siteRoutes(app, db);
  instrumentRoutes(app, db);
  moveRoutes(app, db);
  dataCenterRoutes(app, db);
  powerPlantRoutes(app, db);
  marketRoutes(app, db);
  watchlistRoutes(app, db);
  prefsRoutes(app, db);
  alertDeliveryRoutes(app, { db, linkSecret: r.alertLinkSecret });
  savedViewRoutes(app, db);
  stressRoutes(app, db);
  accountRoutes(app, { db, clerkAdmin: r.clerkAdmin, devMode: r.devMode, products: r.products });
  productAccessRoutes(app, { db, products: r.products });
  infoRoutes(app, db);
  exportRoutes(app, db);
  agentRoutes(app, db);
  webhookRoutes(app, { db, secret: r.webhookSecret, streams: r.hub });
  streamRoutes(app, r.hub);
}

async function registerHttpPlugins(app: FastifyInstance, config: AppConfig): Promise<void> {
  await app.register(helmet, {
    contentSecurityPolicy: { directives: { defaultSrc: ["'none'"], frameAncestors: ["'none'"] } },
    crossOriginResourcePolicy: { policy: 'same-site' },
  });
  await app.register(cors, {
    origin: config.webOrigins,
    credentials: true,
    methods: ['GET', 'PUT', 'POST', 'PATCH', 'DELETE'],
    allowedHeaders: ['authorization', 'content-type', 'last-event-id', 'x-request-id'],
    exposedHeaders: ['x-request-id', 'content-disposition'],
    maxAge: 600,
  });
  // Multipart only for staff document uploads (POST /api/documents); one file, size-limited.
  await app.register(multipart, { limits: { fileSize: uploadMaxBytes(), files: 1, fields: 12 } });
  await app.register(rateLimit, {
    global: true,
    hook: 'preHandler',
    max: config.rateLimitPerMinute,
    timeWindow: '1 minute',
    keyGenerator: (req) => (req.auth ? `user:${req.auth.userId}` : `ip:${req.ip}`),
  });
}

export async function buildApp(deps: AppDeps): Promise<FastifyInstance> {
  const { config } = deps;
  const devMode = config.authMode === 'dev';
  if (devMode) {
    // Re-checked here (not only in loadConfig): the process environment wins over the config object.
    assertDevAuthAllowed(config.nodeEnv);
    if (process.env.NODE_ENV !== undefined) assertDevAuthAllowed(process.env.NODE_ENV);
  }
  const db = deps.db ?? createDb(config.databaseUrl);
  const products = deps.products ?? PRODUCTS;
  const app = Fastify({
    // Request URLs are logged with secret query values masked (?token= on unsubscribe links).
    logger: config.logLevel === false ? false : loggerOptions(config.logLevel),
    genReqId: (req) => {
      const incoming = req.headers['x-request-id'];
      return typeof incoming === 'string' && REQUEST_ID.test(incoming) ? incoming : randomUUID();
    },
    exposeHeadRoutes: false,
    trustProxy: trustProxyOption(config.trustProxy),
  });
  app.decorate('db', db);
  app.decorate('hiddenSources', hiddenSources(config.showUnlicensedPrices));

  // Route tracking first so it sees every route registered below (including plugin routes).
  trackRoutes(app);
  app.setErrorHandler<FastifyError | HttpError>(async (err, req, reply) => {
    const { status, body } = errorBody(err);
    if (status >= 500 && !(err instanceof HttpError)) req.log.error({ err }, 'request failed');
    return reply.code(status).send(body);
  });
  app.setNotFoundHandler(async (_req, reply) => reply.code(404).send({ error: 'not_found' }));
  app.addHook('onSend', async (req, reply) => {
    reply.header('x-request-id', req.id);
  });
  // Every GET accepts ?asOf=YYYY-MM-DD; a malformed date is a 400 on any of them.
  app.addHook('preValidation', (req, _reply, done) => {
    try {
      if (req.method === 'GET') asOfOf(req);
      done();
    } catch (err) {
      done(err as Error);
    }
  });

  await registerHttpPlugins(app, config);
  registerAuth(app, {
    db,
    devMode,
    verify: devMode ? null : createClerkVerifier(config.clerk, config.webOrigins),
    products,
  });

  const hub = new StreamHub(db, {
    connectionString: config.databaseUrlUnpooled, heartbeatMs: config.heartbeatMs, recheckMs: config.recheckMs,
    log: app.log,
    allowed: async (who) => {
      const holder = await currentHolder(db, who);
      return holder !== null && decide(STREAM_RULE, holder, products).ok;
    },
  });
  app.decorate('streams', hub);
  const clerkAdmin = deps.clerkAdmin === undefined ? createClerkAdmin(config.clerk.secretKey) : deps.clerkAdmin;

  registerRoutes(app, {
    db, clerkAdmin, devMode, hub, webhookSecret: config.clerk.webhookSecret, products,
    alertLinkSecret: config.alertLinkSecret ?? null,
  });
  deps.extraRoutes?.(app);

  app.addHook('onReady', async () => {
    if (devMode) await ensureDevIdentity(db);
    await hub.start();
  });
  app.addHook('preClose', async () => {
    await hub.stop();
  });
  app.addHook('onClose', async () => {
    if (!deps.db) await db.close();
  });
  return app;
}
