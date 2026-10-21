// GET /healthz: liveness plus database reachability, for Railway's deploy healthcheck and uptime
// monitors. Public (no session), so it says nothing beyond up/down and the deployed commit.
import type { Queryable } from '../db.js';

export interface Health {
  ok: boolean;
  db: 'ok' | 'unreachable';
  /** Short git sha of the running build (Railway sets RAILWAY_GIT_COMMIT_SHA), else null. */
  commit: string | null;
}

/** Default wait for `SELECT 1`: a Neon compute waking from scale-to-zero answers within a few seconds. */
export const HEALTH_DB_TIMEOUT_MS = 5_000;

function withTimeout<T>(work: Promise<T>, ms: number): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  const timeout = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => reject(new Error('timeout')), ms);
  });
  return Promise.race([work, timeout]).finally(() => clearTimeout(timer));
}

export interface HealthOptions {
  env?: NodeJS.ProcessEnv;
  timeoutMs?: number;
  /** Receives the failure (host, auth or timeout) for the server log; it never reaches the response. */
  onError?: (err: unknown) => void;
}

export async function checkHealth(db: Queryable, opts: HealthOptions = {}): Promise<Health> {
  const env = opts.env ?? process.env;
  const sha = env.RAILWAY_GIT_COMMIT_SHA?.trim();
  const commit = sha ? sha.slice(0, 12) : null;
  try {
    await withTimeout(db.query('SELECT 1'), opts.timeoutMs ?? HEALTH_DB_TIMEOUT_MS);
    return { ok: true, db: 'ok', commit };
  } catch (err) {
    opts.onError?.(err);
    return { ok: false, db: 'unreachable', commit };
  }
}
