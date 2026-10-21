// Postgres access: one pool, read-model reads and org-scoped transactions.
// Read models are kept one row per view/key/day; `asOf` picks the latest snapshot on or before that day.
import pg from 'pg';

// DATE columns stay ISO strings ('2026-10-19'); converting to JS Date would shift them by time zone.
pg.types.setTypeParser(pg.types.builtins.DATE, (v: string) => v);

/** A pool, a pooled client inside a transaction, or the Db itself. */
export interface Queryable {
  query<R extends pg.QueryResultRow = pg.QueryResultRow>(sql: string, params?: unknown[]): Promise<pg.QueryResult<R>>;
}

export interface ReadModelEntry<T = unknown> {
  key: string;
  asOf: string;
  doc: T;
}

export interface Db {
  pool: pg.Pool;
  query<R extends pg.QueryResultRow = pg.QueryResultRow>(sql: string, params?: unknown[]): Promise<pg.QueryResult<R>>;
  /** Latest doc for view/key with as_of <= asOf (or the latest of all when asOf is absent); null when not built. */
  readModel<T = unknown>(view: string, key: string, asOf?: string | null): Promise<T | null>;
  readModelEntry<T = unknown>(view: string, key: string, asOf?: string | null): Promise<ReadModelEntry<T> | null>;
  /** Row views (dc_row, plant_row, ...): the latest doc per key, as of the date. */
  readModelRows<T = unknown>(view: string, asOf?: string | null): Promise<T[]>;
  /** Highest key of a view (e.g. the latest risk_list week) as of the date. */
  latestKey(view: string, asOf?: string | null): Promise<string | null>;
  /** Run fn in a transaction with app.org_id set, so row-level security applies to org data. */
  withOrg<T>(orgId: string, fn: (client: Queryable) => Promise<T>): Promise<T>;
  tx<T>(fn: (client: Queryable) => Promise<T>): Promise<T>;
  close(): Promise<void>;
}

/** SQL for the latest row per key of one view ($1 = view, $2 = as-of date or null). */
export const LATEST_ROWS_SQL = `
  SELECT DISTINCT ON (key) key, as_of, doc
  FROM read_model
  WHERE view = $1 AND ($2::date IS NULL OR as_of <= $2::date)
  ORDER BY key DESC, as_of DESC`;

export function createDb(connectionString: string, max = 10): Db {
  const pool = new pg.Pool({ connectionString, max, application_name: 'marden-api' });
  pool.on('error', () => {
    // Idle client errors (server restart) are retried by the pool on the next checkout.
  });

  async function tx<T>(fn: (client: Queryable) => Promise<T>, orgId?: string): Promise<T> {
    const client = await pool.connect();
    try {
      await client.query('BEGIN');
      if (orgId) await client.query(`SELECT set_config('app.org_id', $1, true)`, [orgId]);
      const out = await fn(client);
      await client.query('COMMIT');
      return out;
    } catch (err) {
      await client.query('ROLLBACK').catch(() => undefined);
      throw err;
    } finally {
      client.release();
    }
  }

  async function readModelEntry<T>(view: string, key: string, asOf?: string | null): Promise<ReadModelEntry<T> | null> {
    const res = await pool.query<{ key: string; as_of: string; doc: T }>(
      `SELECT key, as_of, doc FROM read_model
       WHERE view = $1 AND key = $2 AND ($3::date IS NULL OR as_of <= $3::date)
       ORDER BY as_of DESC LIMIT 1`,
      [view, key, asOf ?? null],
    );
    const row = res.rows[0];
    return row ? { key: row.key, asOf: row.as_of, doc: row.doc } : null;
  }

  return {
    pool,
    query: (sql, params) => pool.query(sql, params),
    readModelEntry,
    async readModel<T>(view: string, key: string, asOf?: string | null) {
      return (await readModelEntry<T>(view, key, asOf))?.doc ?? null;
    },
    async readModelRows<T>(view: string, asOf?: string | null) {
      const res = await pool.query<{ doc: T }>(`SELECT doc FROM (${LATEST_ROWS_SQL}) r ORDER BY key`, [view, asOf ?? null]);
      return res.rows.map((r) => r.doc);
    },
    async latestKey(view: string, asOf?: string | null) {
      const res = await pool.query<{ key: string }>(
        `SELECT key FROM read_model WHERE view = $1 AND ($2::date IS NULL OR as_of <= $2::date)
         ORDER BY key DESC LIMIT 1`,
        [view, asOf ?? null],
      );
      return res.rows[0]?.key ?? null;
    },
    withOrg: (orgId, fn) => tx(fn, orgId),
    tx: (fn) => tx(fn),
    close: () => pool.end(),
  };
}
