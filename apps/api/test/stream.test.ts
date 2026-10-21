// GET /api/stream over a real socket: a stream_event inserted after connect is delivered (NOTIFY ->
// row -> client), other orgs' events are not, and a reconnect with Last-Event-ID replays what was missed.
import type { AddressInfo } from 'node:net';
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { makeApp, testDb } from './helpers.js';
import { connect, events, until } from './sse-client.js';

describe('GET /api/stream', () => {
  let db: Db;
  let app: FastifyInstance;
  let port: number;

  beforeAll(async () => {
    db = testDb();
    app = await makeApp({ mode: 'dev', db, env: { STREAM_HEARTBEAT_MS: '200' } });
    await app.listen({ host: '127.0.0.1', port: 0 });
    port = (app.server.address() as AddressInfo).port;
  });
  afterAll(async () => {
    await app.close();
    await db.query(`DELETE FROM stream_event WHERE payload ->> 'source' LIKE 'stream-test%'`);
    await db.close();
  });

  const insert = async (source: string, orgId: string | null = null): Promise<string> => {
    const res = await db.query<{ id: string }>(
      `INSERT INTO stream_event (org_id, payload) VALUES ($1, $2) RETURNING id::text`,
      [orgId, JSON.stringify({ type: 'pipeline', source, status: 'ok', at: '2026-10-21T00:00:00Z' })],
    );
    return res.rows[0]!.id;
  };

  it('delivers events inserted after connect, scoped to the org, with heartbeats', async () => {
    const sse = await connect(port);
    expect(sse.status).toBe(200);
    expect(sse.headers['content-type']).toContain('text/event-stream');
    await until(() => sse.chunks().includes(': connected'));
    const other = await insert('stream-test-other-org', 'some-other-org');
    const id = await insert('stream-test-live', 'dev');
    const global = await insert('stream-test-global');
    await until(() => events(sse.chunks()).length >= 2);
    const got = events(sse.chunks());
    expect(got.map((e) => e.id)).toEqual([id, global]);
    expect(got.map((e) => e.id)).not.toContain(other);
    expect(got[0]?.data).toEqual({ type: 'pipeline', source: 'stream-test-live', status: 'ok', at: '2026-10-21T00:00:00Z' });
    await until(() => sse.chunks().includes(': heartbeat'));
    sse.close();
    await until(() => app.streams.size === 0);
  });

  it('replays rows after Last-Event-ID on reconnect, then continues live', async () => {
    const first = await insert('stream-test-replay-1', 'dev');
    const missed = await insert('stream-test-replay-2', 'dev');
    const missedGlobal = await insert('stream-test-replay-3');
    await insert('stream-test-replay-other', 'some-other-org');
    const sse = await connect(port, first);
    await until(() => events(sse.chunks()).length >= 2);
    expect(events(sse.chunks()).map((e) => e.id)).toEqual([missed, missedGlobal]);
    const live = await insert('stream-test-replay-live', 'dev');
    await until(() => events(sse.chunks()).length >= 3);
    expect(events(sse.chunks()).map((e) => e.id)).toEqual([missed, missedGlobal, live]);
    sse.close();
  });

  it('closes streams when the session or user is revoked', async () => {
    const sse = await connect(port);
    await until(() => app.streams.size === 1);
    app.streams.closeUser('dev-user');
    await until(() => app.streams.size === 0);
    sse.close();
  });
});
