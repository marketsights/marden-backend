// Stress runs and saved scenarios. Runs execute in the stress model (Python), exposed to the API as the
// SQL function stress_run(request jsonb) -> jsonb; until that function exists the API answers 503.
import type { SavedStress } from '../contracts/app.js';
import type { Db, Queryable } from '../db.js';
import { badRequest } from '../errors.js';
import { dayEnd } from '../lib/business-day.js';
import { stressRequestSchema, type StressRunRequest } from '../lib/defaults.js';
import { first, type Query } from '../lib/params.js';

export const STRESS_FUNCTION = 'stress_run(jsonb)';

/** True when stress_run(jsonb) exists in the connection's own schema (public in production; a test's
 *  throwaway schema in the API tests, so a stand-in there never reaches the real function in public). */
export async function stressModelAvailable(db: Db): Promise<boolean> {
  const res = await db.query<{ fn: string | null }>(
    `SELECT to_regprocedure(quote_ident(current_schema()) || '.' || $1)::text AS fn`, [STRESS_FUNCTION]);
  return res.rows[0]?.fn != null;
}

export async function runStress(db: Db, request: StressRunRequest): Promise<unknown> {
  const res = await db.query<{ result: unknown }>(`SELECT stress_run($1::jsonb) AS result`, [JSON.stringify(request)]);
  return res.rows[0]?.result ?? null;
}

/** stress_run's answer for a custom run the model has queued but not finished ({status, jobId, subject}). */
export function isPendingStress(result: unknown): result is { status: 'queued' | 'running'; jobId: unknown } {
  const status = (result as { status?: unknown } | null)?.status;
  return status === 'queued' || status === 'running';
}

export function isStressError(result: unknown): result is { status: 'error'; error: string } {
  return (result as { status?: unknown } | null)?.status === 'error';
}

const POLL_INTERVAL_MS = 250;
/** How long POST /api/stress/run waits for a queued custom run (MARDEN_STRESS_WAIT_MS, default 20 s). */
export function stressWaitMs(): number {
  const v = Number(process.env.MARDEN_STRESS_WAIT_MS);
  return Number.isFinite(v) && v >= 0 ? v : 20_000;
}

/**
 * The stress_run contract (db/migrations/20261021210000_stress_run.sql): a saved scenario answers at once;
 * a custom run answers {status: 'queued' | 'running', jobId} while the stress model's worker computes it,
 * and the same call returns the result once it is stored. Calls again every 250 ms until done or the wait
 * runs out; the last answer (possibly still pending) is returned.
 */
export async function runStressWaiting(db: Db, request: StressRunRequest, waitMs = stressWaitMs()): Promise<unknown> {
  const deadline = Date.now() + waitMs;
  let result = await runStress(db, request);
  while (isPendingStress(result) && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, Math.min(POLL_INTERVAL_MS, Math.max(deadline - Date.now(), 0))));
    result = await runStress(db, request);
  }
  return result;
}

const RESERVED = new Set(['scenario', 'severity', 'asOf']);

/**
 * The run an export URL names, in the screen's own URL form (/stress?scenario=&severity=&<param>=):
 * every other query parameter is a scenario parameter, numbers parsed. 400 when it is not a valid run.
 */
export function stressRequestFromQuery(q: Query): StressRunRequest {
  const params: Record<string, string | number> = {};
  for (const key of Object.keys(q).sort()) {
    const value = first(q, key);
    if (RESERVED.has(key) || value === undefined) continue;
    params[key] = /^-?\d+(?:\.\d+)?$/.test(value) ? Number(value) : value;
  }
  const parsed = stressRequestSchema.safeParse({ scenario: first(q, 'scenario'), params, severity: first(q, 'severity') ?? 'base' });
  if (!parsed.success) {
    throw badRequest('invalid_stress_request', parsed.error.issues.map((i) => ({ path: i.path.join('.'), message: i.message })));
  }
  return parsed.data;
}

interface SavedRow {
  id: string; name: string; request: SavedStress['request']; result: unknown;
  ran_at: Date | null; created_at: Date; user_id: string | null;
}

const toSaved = (r: SavedRow): SavedStress => ({
  id: r.id,
  name: r.name,
  request: r.request,
  result: r.result,
  ranAt: r.ran_at ? r.ran_at.toISOString() : null,
  createdAt: r.created_at.toISOString(),
  createdBy: r.user_id,
  savedAt: r.created_at.toISOString(),
});

const COLUMNS = `id::text, name, request, result, ran_at, created_at, user_id`;

/** A finished stress result (not queued, not an error): what a saved scenario may store at once. */
export function isFinishedStress(result: unknown): boolean {
  return typeof result === 'object' && result !== null && !isPendingStress(result) && !isStressError(result);
}

/** `result`: the model's finished answer for the request when it had one at save time (else null until it runs). */
export async function insertSaved(
  c: Queryable,
  who: { orgId: string; userId: string },
  input: { name: string; request: StressRunRequest; result?: unknown },
): Promise<SavedStress> {
  const result = isFinishedStress(input.result) ? JSON.stringify(input.result) : null;
  const res = await c.query<SavedRow>(
    `INSERT INTO stress_saved (org_id, user_id, name, request, result, ran_at)
     VALUES ($1, $2, $3, $4, $5::jsonb, CASE WHEN $5::jsonb IS NULL THEN NULL ELSE now() END) RETURNING ${COLUMNS}`,
    [who.orgId, who.userId, input.name, JSON.stringify(input.request), result],
  );
  const row = res.rows[0];
  if (!row) throw new Error('insert returned no row');
  return toSaved(row);
}

export async function listSaved(c: Queryable, orgId: string, asOf: string | null): Promise<SavedStress[]> {
  const res = await c.query<SavedRow>(
    `SELECT ${COLUMNS} FROM stress_saved WHERE org_id = $1 AND ($2::date IS NULL OR created_at < ${dayEnd('$2')})
     ORDER BY created_at DESC, id`,
    [orgId, asOf],
  );
  return res.rows.map(toSaved);
}

/** null for unknown ids and for another org's scenarios (-> 404). */
export async function getSaved(c: Queryable, orgId: string, id: string): Promise<SavedStress | null> {
  const res = await c.query<SavedRow>(`SELECT ${COLUMNS} FROM stress_saved WHERE org_id = $1 AND id::text = $2`, [orgId, id]);
  return res.rows[0] ? toSaved(res.rows[0]) : null;
}
