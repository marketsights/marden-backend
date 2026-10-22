// Environment for the API. Fails closed: production refuses dev auth and needs Clerk keys (spec 23).
import { z } from 'zod';

export type AuthMode = 'dev' | 'clerk';

export interface ClerkConfig {
  secretKey: string | null;
  publishableKey: string | null;
  jwtKey: string | null;
  issuer: string | null; // derived from the publishable key: https://<frontend api>
  webhookSecret: string | null;
}

export interface AppConfig {
  nodeEnv: string;
  databaseUrl: string;
  /** Direct endpoint for session-level work (the stream's LISTEN); equals databaseUrl when unset. */
  databaseUrlUnpooled: string;
  authMode: AuthMode;
  clerk: ClerkConfig;
  webOrigins: string[];
  rateLimitPerMinute: number;
  heartbeatMs: number;
  recheckMs: number;
  logLevel: string | false;
  /**
   * Which X-Forwarded-For entry is the client (rate-limit key, logs): false = the socket peer, a number
   * n = n proxy hops are trusted (Railway: 1, its edge appends the client address), true = every hop
   * (the left-most entry, which a client can set itself).
   */
  trustProxy: boolean | number;
  /** How long SIGTERM waits for in-flight requests before the process exits anyway (ms). */
  shutdownTimeoutMs: number;
  /**
   * Verifies the one-click unsubscribe links in alert emails (the worker signs them with the same value).
   * null when ALERT_LINK_SECRET is unset or shorter than 32 characters: the links then answer 503.
   */
  alertLinkSecret: string | null;
  /**
   * MARDEN_SHOW_UNLICENSED_PRICES (default true), the same switch the Python services read: false leaves
   * every fact row from an 'unlicensed-commercial' source out of the API's own fact reads (lib/licensing.ts).
   */
  showUnlicensedPrices: boolean;
  host: string;
  port: number;
}

const envSchema = z.object({
  NODE_ENV: z.string().default('development'),
  DATABASE_URL: z.string().min(1, 'DATABASE_URL is required'),
  // Neon's direct (non-pooler) endpoint for the stream's LISTEN connection; PgBouncer's
  // transaction mode drops LISTEN. Falls back to DATABASE_URL (local Postgres has no pooler).
  DATABASE_URL_UNPOOLED: z.string().optional(),
  MARDEN_AUTH_MODE: z.enum(['dev', 'clerk']).default('clerk'),
  CLERK_SECRET_KEY: z.string().optional(),
  CLERK_PUBLISHABLE_KEY: z.string().optional(),
  CLERK_JWT_KEY: z.string().optional(),
  CLERK_WEBHOOK_SIGNING_SECRET: z.string().optional(),
  WEB_ORIGIN: z.string().default('http://localhost:3000'),
  RATE_LIMIT_PER_MINUTE: z.coerce.number().int().positive().default(600),
  STREAM_HEARTBEAT_MS: z.coerce.number().int().positive().default(25_000),
  STREAM_RECHECK_MS: z.coerce.number().int().positive().default(600_000),
  LOG_LEVEL: z.string().default('info'),
  TRUST_PROXY: z.string().regex(/^(true|false|[0-9]{1,2})$/, 'must be true, false or a hop count').default('false'),
  SHUTDOWN_TIMEOUT_MS: z.coerce.number().int().positive().default(20_000),
  ALERT_LINK_SECRET: z.string().optional(),
  MARDEN_SHOW_UNLICENSED_PRICES: z.string().optional(),
  HOST: z.string().default('0.0.0.0'),
  PORT: z.coerce.number().int().positive().default(4000),
});

function parseTrustProxy(v: string): boolean | number {
  if (v === 'true') return true;
  if (v === 'false') return false;
  const hops = Number(v);
  return hops === 0 ? false : hops;
}

const blank = (v: string | undefined): string | null => (v && v.trim() !== '' ? v.trim() : null);

/** ALERT_LINK_SECRET when long enough to sign links (services/marden/alerts/settings.py MIN_SECRET_LENGTH). */
const linkSecret = (v: string | undefined): string | null => {
  const s = blank(v);
  return s !== null && s.length >= 32 ? s : null;
};

/** As services/marden/core/config.py: true/1/yes or false/0/no (any case); unset or blank = true; else an error. */
const BOOLS: Readonly<Record<string, boolean>> = { true: true, '1': true, yes: true, false: false, '0': false, no: false };
export function parseShowUnlicensed(v: string | undefined): boolean {
  const s = blank(v);
  if (s === null) return true;
  const value = BOOLS[s.toLowerCase()];
  if (value === undefined) throw new Error('MARDEN_SHOW_UNLICENSED_PRICES must be true or false');
  return value;
}

/** Clerk publishable keys are `pk_(test|live)_` + base64("<frontend api>$"). */
export function issuerFromPublishableKey(key: string | null): string | null {
  if (!key) return null;
  const match = /^pk_(test|live)_(.+)$/.exec(key);
  if (!match?.[2]) throw new Error('CLERK_PUBLISHABLE_KEY is not a Clerk publishable key');
  const decoded = Buffer.from(match[2], 'base64').toString('utf8');
  if (!decoded.endsWith('$') || decoded.length < 2) throw new Error('CLERK_PUBLISHABLE_KEY does not encode a frontend API');
  return `https://${decoded.slice(0, -1)}`;
}

/** NODE_ENV values where dev auth (one local owner + staff user, no token) may run. */
const DEV_AUTH_NODE_ENVS = new Set(['development', 'test']);
/** Set by the host on every deployed service (Railway, Render; staging and production). */
const DEPLOY_MARKERS = ['RAILWAY_ENVIRONMENT', 'RAILWAY_ENVIRONMENT_NAME', 'RAILWAY_ENVIRONMENT_ID', 'RAILWAY_PROJECT_ID', 'RENDER', 'RENDER_SERVICE_ID'];

/**
 * Dev auth only on a developer machine or in tests: refused unless NODE_ENV is development or test,
 * and refused on any deployed (Railway) environment whatever NODE_ENV says. Called by loadConfig and
 * again by buildApp, so a hand-built config cannot skip it.
 */
export function assertDevAuthAllowed(nodeEnv: string, env: NodeJS.ProcessEnv = process.env): void {
  if (!DEV_AUTH_NODE_ENVS.has(nodeEnv)) {
    throw new Error(`MARDEN_AUTH_MODE=dev is refused when NODE_ENV=${nodeEnv || '(empty)'}; only development or test`);
  }
  const marker = DEPLOY_MARKERS.find((name) => blank(env[name]) !== null);
  if (marker) throw new Error(`MARDEN_AUTH_MODE=dev is refused on a deployed environment (${marker} is set)`);
}

function assertAuthConfig(env: NodeJS.ProcessEnv, nodeEnv: string, mode: AuthMode, clerk: ClerkConfig): void {
  const production = nodeEnv === 'production';
  if (mode === 'dev') {
    assertDevAuthAllowed(nodeEnv, env);
    return;
  }
  if (production && (!clerk.secretKey || !clerk.publishableKey)) {
    throw new Error('CLERK_SECRET_KEY and CLERK_PUBLISHABLE_KEY are required when NODE_ENV=production');
  }
  if (!clerk.secretKey && !clerk.jwtKey) {
    throw new Error('Clerk auth needs CLERK_SECRET_KEY or CLERK_JWT_KEY (or MARDEN_AUTH_MODE=dev for local work)');
  }
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): AppConfig {
  const parsed = envSchema.safeParse(env);
  if (!parsed.success) {
    throw new Error(`Invalid environment: ${parsed.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`).join('; ')}`);
  }
  const e = parsed.data;
  const publishableKey = blank(e.CLERK_PUBLISHABLE_KEY);
  const clerk: ClerkConfig = {
    secretKey: blank(e.CLERK_SECRET_KEY),
    publishableKey,
    jwtKey: blank(e.CLERK_JWT_KEY)?.replace(/\\n/g, '\n') ?? null,
    issuer: issuerFromPublishableKey(publishableKey),
    webhookSecret: blank(e.CLERK_WEBHOOK_SIGNING_SECRET),
  };
  assertAuthConfig(env, e.NODE_ENV, e.MARDEN_AUTH_MODE, clerk);
  return {
    nodeEnv: e.NODE_ENV,
    databaseUrl: e.DATABASE_URL,
    databaseUrlUnpooled: blank(e.DATABASE_URL_UNPOOLED) ?? e.DATABASE_URL,
    authMode: e.MARDEN_AUTH_MODE,
    clerk,
    webOrigins: e.WEB_ORIGIN.split(',').map((o) => o.trim()).filter(Boolean),
    rateLimitPerMinute: e.RATE_LIMIT_PER_MINUTE,
    heartbeatMs: e.STREAM_HEARTBEAT_MS,
    recheckMs: e.STREAM_RECHECK_MS,
    logLevel: e.LOG_LEVEL === 'silent' ? false : e.LOG_LEVEL,
    trustProxy: parseTrustProxy(e.TRUST_PROXY),
    shutdownTimeoutMs: e.SHUTDOWN_TIMEOUT_MS,
    alertLinkSecret: linkSecret(e.ALERT_LINK_SECRET),
    showUnlicensedPrices: parseShowUnlicensed(e.MARDEN_SHOW_UNLICENSED_PRICES),
    host: e.HOST,
    port: e.PORT,
  };
}
