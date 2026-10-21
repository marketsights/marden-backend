// End to end across the two services: the Python core writes stream events with
// marden.core.events.emit (stream_event row -> trigger -> NOTIFY marden_stream), and GET /api/stream
// delivers them to a connected client, scoped to its org. Python runs as a real subprocess (uv) on
// the same throwaway schema as the API tests. Set MARDEN_SKIP_PYTHON_E2E=1 only on a machine
// without the Python toolchain (scripts/dev-setup.sh installs it).
import { execFile } from 'node:child_process';
import type { AddressInfo } from 'node:net';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { dbUrl, makeApp, testDb } from './helpers.js';
import { connect, events, until } from './sse-client.js';

const run = promisify(execFile);
const SERVICES = fileURLToPath(new URL('../../../services', import.meta.url));
const SKIP = process.env.MARDEN_SKIP_PYTHON_E2E === '1';

// Emits three events: one for org `dev` (the dev-mode user), one for everyone, one for another org.
const EMIT = `
import json
from marden.core.events import emit
at = "2026-10-21T12:00:00+00:00"
ids = [
    emit({"type": "pipeline", "source": "py-e2e-dev", "status": "down", "at": at}, "dev"),
    emit({"type": "campus", "campusId": "py-e2e-campus"}),
    emit({"type": "pipeline", "source": "py-e2e-other", "status": "ok", "at": at}, "py-e2e-other-org"),
]
print(json.dumps([str(i) for i in ids]))
`;

async function pythonEmit(): Promise<string[]> {
  const { stdout } = await run('uv', ['run', '--frozen', '--project', SERVICES, 'python', '-c', EMIT], {
    cwd: SERVICES,
    env: { ...process.env, DATABASE_URL: dbUrl() },
    timeout: 60_000,
  });
  const last = stdout.trim().split('\n').at(-1) ?? '[]';
  return JSON.parse(last) as string[];
}

describe.skipIf(SKIP)('stream: Python emit -> API SSE', () => {
  let db: Db;
  let app: FastifyInstance;
  let port: number;

  beforeAll(async () => {
    db = testDb();
    app = await makeApp({ mode: 'dev', db });
    await app.listen({ host: '127.0.0.1', port: 0 });
    port = (app.server.address() as AddressInfo).port;
  });
  afterAll(async () => {
    await app.close();
    await db.query(`DELETE FROM stream_event WHERE payload ->> 'source' LIKE 'py-e2e-%' OR payload ->> 'campusId' LIKE 'py-e2e-%'`);
    await db.close();
  });

  it('delivers events written by marden.core.events.emit to the connected client of that org', async () => {
    const sse = await connect(port);
    await until(() => sse.chunks().includes(': connected'));
    const [devId, globalId, otherId] = await pythonEmit();
    expect(devId && globalId && otherId).toBeTruthy();

    await until(() => events(sse.chunks()).length >= 2, 10_000);
    const got = events(sse.chunks());
    expect(got.map((e) => e.id)).toEqual([devId, globalId]);
    expect(got[0]?.data).toEqual({ type: 'pipeline', source: 'py-e2e-dev', status: 'down', at: '2026-10-21T12:00:00+00:00' });
    expect(got[1]?.data).toEqual({ type: 'campus', campusId: 'py-e2e-campus' });

    // The rows are the ones Python wrote, in this schema; the other org's row was not delivered.
    const rows = await db.query<{ id: string; org_id: string | null }>(
      `SELECT id::text, org_id FROM stream_event WHERE id = ANY($1::bigint[]) ORDER BY id`, [[devId, globalId, otherId]],
    );
    expect(rows.rows).toEqual([{ id: devId, org_id: 'dev' }, { id: globalId, org_id: null }, { id: otherId, org_id: 'py-e2e-other-org' }]);
    expect(sse.chunks()).not.toContain('py-e2e-other');

    // A client reconnecting with Last-Event-ID gets the Python events it missed.
    sse.close();
    const again = await connect(port, String(BigInt(devId!) - 1n));
    await until(() => events(again.chunks()).length >= 2, 10_000);
    expect(events(again.chunks()).map((e) => e.id)).toEqual([devId, globalId]);
    again.close();
  });
});
