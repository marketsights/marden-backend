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
  authMode: AuthMode;
  clerk: ClerkConfig;
  webOrigins: string[];
  rateLimitPerMinute: number;
  heartbeatMs: number;
  recheckMs: number;
  logLevel: string | false;
  host: string;
  port: number;
}

const envSchema = z.object({
  NODE_ENV: z.string().default('development'),
  DATABASE_URL: z.string().min(1, 'DATABASE_URL is required'),
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
  HOST: z.string().default('0.0.0.0'),
  PORT: z.coerce.number().int().positive().default(4000),
});

const blank = (v: string | undefined): string | null => (v && v.trim() !== '' ? v.trim() : null);

/** Clerk publishable keys are `pk_(test|live)_` + base64("<frontend api>$"). */
export function issuerFromPublishableKey(key: string | null): string | null {
  if (!key) return null;
  const match = /^pk_(test|live)_(.+)$/.exec(key);
  if (!match?.[2]) throw new Error('CLERK_PUBLISHABLE_KEY is not a Clerk publishable key');
  const decoded = Buffer.from(match[2], 'base64').toString('utf8');
  if (!decoded.endsWith('$') || decoded.length < 2) throw new Error('CLERK_PUBLISHABLE_KEY does not encode a frontend API');
  return `https://${decoded.slice(0, -1)}`;
}

function assertAuthConfig(nodeEnv: string, mode: AuthMode, clerk: ClerkConfig): void {
  const production = nodeEnv === 'production';
  if (mode === 'dev') {
    if (production) throw new Error('MARDEN_AUTH_MODE=dev is refused when NODE_ENV=production');
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
  assertAuthConfig(e.NODE_ENV, e.MARDEN_AUTH_MODE, clerk);
  return {
    nodeEnv: e.NODE_ENV,
    databaseUrl: e.DATABASE_URL,
    authMode: e.MARDEN_AUTH_MODE,
    clerk,
    webOrigins: e.WEB_ORIGIN.split(',').map((o) => o.trim()).filter(Boolean),
    rateLimitPerMinute: e.RATE_LIMIT_PER_MINUTE,
    heartbeatMs: e.STREAM_HEARTBEAT_MS,
    recheckMs: e.STREAM_RECHECK_MS,
    logLevel: e.LOG_LEVEL === 'silent' ? false : e.LOG_LEVEL,
    host: e.HOST,
    port: e.PORT,
  };
}
