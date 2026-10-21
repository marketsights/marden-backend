// The stream's catch-up and replay queries return rows in numeric id order across a digit boundary
// (99 -> 100). Ids are selected as text, so a bare `ORDER BY id` would sort them as strings: catch-up
// would deliver 100 before 99, and the client, which drops ids it has already passed, would lose 99.
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { CATCH_UP_SQL, REPLAY_LIMIT, REPLAY_SQL } from '../src/stream.js';
import { testDb } from './helpers.js';

const SOURCE = 'stream-test-order';

describe('stream row order', () => {
  let db: Db;
  beforeAll(() => {
    db = testDb();
  });
  afterAll(async () => {
    await db.query(`DELETE FROM stream_event WHERE payload ->> 'source' = $1`, [SOURCE]);
    await db.close();
  });

  /** Moves the id sequence so the next `count` ids start `before` below a power of ten, and inserts them. */
  async function insertAcrossBoundary(before: number, count: number): Promise<{ boundary: bigint; ids: bigint[] }> {
    const max = await db.query<{ max: string | null }>(`SELECT max(id)::text AS max FROM stream_event`);
    const seq = await db.query<{ last: string }>(`SELECT last_value::text AS last FROM stream_event_id_seq`);
    const floor = BigInt(Math.max(Number(max.rows[0]?.max ?? 0), Number(seq.rows[0]?.last ?? 0))) + BigInt(before) + 1n;
    let boundary = 10n;
    while (boundary <= floor) boundary *= 10n;
    await db.query(`SELECT setval('stream_event_id_seq', $1::bigint)`, [(boundary - BigInt(before) - 1n).toString()]);
    const res = await db.query<{ id: string }>(
      `INSERT INTO stream_event (org_id, payload)
       SELECT NULL, jsonb_build_object('type', 'pipeline', 'source', $1::text, 'n', n) FROM generate_series(1, $2::int) n
       RETURNING id::text`,
      [SOURCE, count],
    );
    return { boundary, ids: res.rows.map((r) => BigInt(r.id)).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0)) };
  }

  it('catches up oldest first: 99 before 100', async () => {
    const { boundary, ids } = await insertAcrossBoundary(2, 4);
    expect(ids[0]).toBe(boundary - 2n);
    const res = await db.query<{ id: string }>(CATCH_UP_SQL, [(ids[0]! - 1n).toString()]);
    const got = res.rows.map((r) => BigInt(r.id));
    expect(got.slice(0, 4)).toEqual(ids);
  });

  it('replays the newest rows by number, oldest first, when more were missed than it replays', async () => {
    const { boundary, ids } = await insertAcrossBoundary(3, REPLAY_LIMIT + 2);
    expect(ids[0]).toBe(boundary - 3n);
    const res = await db.query<{ id: string; total: number }>(REPLAY_SQL, [(ids[0]! - 1n).toString(), 'dev']);
    expect(res.rows[0]?.total).toBe(REPLAY_LIMIT + 2);
    expect(res.rows.map((r) => BigInt(r.id))).toEqual(ids.slice(2));
  });
});
