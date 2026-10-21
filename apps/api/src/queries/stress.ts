// Stress runs and saved scenarios. Runs execute in the stress model (Python), exposed to the API as the
// SQL function stress_run(request jsonb) -> jsonb; until that function exists the API answers 503.
import type { SavedStress } from '../contracts/app.js';
import type { Db, Queryable } from '../db.js';
import type { StressRunRequest } from '../lib/defaults.js';

export const STRESS_FUNCTION = 'stress_run(jsonb)';

export async function stressModelAvailable(db: Db): Promise<boolean> {
  const res = await db.query<{ fn: string | null }>(`SELECT to_regprocedure($1)::text AS fn`, [STRESS_FUNCTION]);
  return res.rows[0]?.fn != null;
}

export async function runStress(db: Db, request: StressRunRequest): Promise<unknown> {
  const res = await db.query<{ result: unknown }>(`SELECT stress_run($1::jsonb) AS result`, [JSON.stringify(request)]);
  return res.rows[0]?.result ?? null;
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
});

const COLUMNS = `id::text, name, request, result, ran_at, created_at, user_id`;

export async function insertSaved(
  c: Queryable,
  who: { orgId: string; userId: string },
  input: { name: string; request: StressRunRequest },
): Promise<SavedStress> {
  const res = await c.query<SavedRow>(
    `INSERT INTO stress_saved (org_id, user_id, name, request) VALUES ($1, $2, $3, $4) RETURNING ${COLUMNS}`,
    [who.orgId, who.userId, input.name, JSON.stringify(input.request)],
  );
  const row = res.rows[0];
  if (!row) throw new Error('insert returned no row');
  return toSaved(row);
}

export async function listSaved(c: Queryable, orgId: string, asOf: string | null): Promise<SavedStress[]> {
  const res = await c.query<SavedRow>(
    `SELECT ${COLUMNS} FROM stress_saved WHERE org_id = $1 AND ($2::date IS NULL OR created_at < ($2::date + 1))
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
