// Filtered, sorted, paged reads of row read models (one doc per campus / plant).
import type { Db } from '../db.js';
import { asOfDay, type Query } from '../lib/params.js';
import { buildOrder, buildWhere, latestRowsSql, SqlParams, type RowViewDef } from '../lib/row-query.js';

export interface RowScope {
  q: Query;
  orgId: string;
  asOf: string | null;
  /** Source names left out of fact reads (FastifyInstance.hiddenSources: the unlicensed-price switch). */
  hidden: readonly string[];
}

export interface FilteredRows {
  params: SqlParams;
  /** `WITH latest AS (...), filtered AS (...)` over the view, ready for a SELECT. */
  cte: string;
}

/** The filtered row set as CTEs; callers append their SELECT and pass params.values. */
export function filteredRows(def: RowViewDef, scope: RowScope): FilteredRows {
  const params = new SqlParams();
  const viewPh = params.add(def.view);
  const asOfPh = params.add(scope.asOf);
  const where = buildWhere(def, scope.q, params, {
    orgId: scope.orgId, day: asOfDay(scope.asOf), asOf: scope.asOf, hidden: scope.hidden,
  });
  const cte = `WITH latest AS (${latestRowsSql(viewPh, asOfPh)}),
    filtered AS (SELECT r.key, r.as_of, r.doc FROM latest r WHERE ${where})`;
  return { params, cte };
}

export interface RowsPage<T> {
  /** Rows in the view at all (0 = the read model has not been built). */
  available: number;
  asOf: string | null;
  total: number;
  rows: T[];
}

export async function queryRows<T>(
  db: Db,
  def: RowViewDef,
  scope: RowScope,
  window: { limit: number; offset: number },
): Promise<RowsPage<T>> {
  const { params, cte } = filteredRows(def, scope);
  const head = await db.query<{ available: number; as_of: string | null; total: number }>(
    `${cte} SELECT (SELECT count(*) FROM latest)::int AS available,
       (SELECT max(as_of) FROM latest) AS as_of, (SELECT count(*) FROM filtered)::int AS total`,
    [...params.values],
  );
  const { available, as_of: asOf, total } = head.rows[0] ?? { available: 0, as_of: null, total: 0 };
  if (available === 0 || total === 0) return { available, asOf, total, rows: [] };
  const order = buildOrder(def, scope.q);
  const limitPh = params.add(window.limit);
  const offsetPh = params.add(window.offset);
  const companion = def.companion;
  const join = companion
    ? `LEFT JOIN (${latestRowsSql(params.add(companion.view), params.add(scope.asOf))}) c ON c.key = r.key`
    : '';
  const body = await db.query<{ doc: T; companion?: unknown }>(
    `${cte} SELECT r.doc${companion ? ', c.doc AS companion' : ''} FROM filtered r ${join}
      ORDER BY ${order} LIMIT ${limitPh} OFFSET ${offsetPh}`,
    params.values,
  );
  const rows = body.rows.map((r) => (companion ? (companion.merge(r.doc as never, (r.companion ?? null) as never) as T) : r.doc));
  return { available, asOf, total, rows };
}

/** One row doc of a row view (the `/row` refresh), with its companion joined like a page row. */
export async function rowDoc<T>(db: Db, def: RowViewDef, row: { doc: T; key: string; asOf: string | null }): Promise<T> {
  if (!def.companion) return row.doc;
  const other = await db.readModel(def.companion.view, row.key, row.asOf);
  return def.companion.merge(row.doc as never, (other ?? null) as never) as T;
}

/** Does the subject exist in its identity table? Separates "unknown id" (404) from "not built" (503). */
export async function subjectExists(db: Db, table: string, column: string, id: string): Promise<boolean> {
  const res = await db.query(`SELECT 1 FROM ${table} WHERE ${column} = $1`, [id]);
  return (res.rowCount ?? 0) > 0;
}
