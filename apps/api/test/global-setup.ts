// Creates a throwaway schema on the local Postgres, applies the app migrations into it and hands the
// tests a DATABASE_URL whose search_path points there. The schema is dropped afterwards; the
// developer's own data in `public` is never touched.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import pg from 'pg';
import type { TestProject } from 'vitest/node';

declare module 'vitest' {
  export interface ProvidedContext {
    databaseUrl: string;
    testSchema: string;
  }
}

const BASE_URL = process.env.DATABASE_URL ?? 'postgres://marden:marden@localhost:5432/marden?sslmode=disable';
/** The migrations the API's tables come from, in order (core, per-product access, per-member alerts and
 * stream_event.user_id, Marden-owned org roles, saved views). */
const MIGRATIONS = ['20261021100000_core.sql', '20261021180000_product_access.sql', '20261021223000_alert_engine.sql',
  '20261021234100_marden_org_roles.sql', '20261021235500_saved_view.sql']
  .map((name) => fileURLToPath(new URL(`../../../db/migrations/${name}`, import.meta.url)));

export function withSearchPath(url: string, schema: string): string {
  const options = encodeURIComponent(`-c search_path=${schema},public`);
  return `${url}${url.includes('?') ? '&' : '?'}options=${options}`;
}

export async function setup(project: TestProject): Promise<() => Promise<void>> {
  const schema = `api_test_${process.pid}_${Date.now().toString(36)}`;
  const ups = MIGRATIONS.map((file) => readFileSync(file, 'utf8').split('-- migrate:down')[0] ?? '');
  const client = new pg.Client({ connectionString: BASE_URL });
  await client.connect();
  try {
    await client.query(`CREATE SCHEMA ${schema}`);
    await client.query(`SET search_path TO ${schema}, public`);
    for (const up of ups) await client.query(up);
  } finally {
    await client.end();
  }
  project.provide('databaseUrl', withSearchPath(BASE_URL, schema));
  project.provide('testSchema', schema);
  return async () => {
    const c = new pg.Client({ connectionString: BASE_URL });
    await c.connect();
    await c.query(`DROP SCHEMA IF EXISTS ${schema} CASCADE`);
    await c.end();
  };
}
