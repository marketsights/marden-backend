// GET /api/stream: Server-Sent Events from stream_event. One shared LISTEN marden_stream connection;
// each NOTIFY carries a row id, the row is loaded and pushed to clients of its org (org_id null = all).
// Reconnecting clients send Last-Event-ID and get the rows after it (at most 500) before live events.
import type { ServerResponse } from 'node:http';
import type { FastifyBaseLogger, FastifyInstance } from 'fastify';
import pg from 'pg';
import { authOf } from './auth/plugin.js';
import type { Db } from './db.js';
import { isMember } from './queries/account.js';

export const STREAM_CHANNEL = 'marden_stream';
export const REPLAY_LIMIT = 500;
const MAX_BUFFERED_BYTES = 1024 * 1024;

interface StreamRow { id: string; org_id: string | null; payload: unknown }

// Ids are selected as text (bigint does not fit a JS number). ORDER BY must name the table's bigint
// column (stream_event.id): a bare `id` would bind to the text output column and sort '100' before
// '99', so catch-up would deliver 100 first and the client would then drop 99 as already seen.
/** Rows written while the listener was down, oldest first. */
export const CATCH_UP_SQL =
  `SELECT id::text, org_id, payload FROM stream_event WHERE id > $1 ORDER BY stream_event.id LIMIT ${REPLAY_LIMIT}`;
/** The newest REPLAY_LIMIT rows after a client's Last-Event-ID that its org may see, oldest first. */
export const REPLAY_SQL = `SELECT * FROM (
     SELECT id::text, org_id, payload, count(*) OVER ()::int AS total FROM stream_event
     WHERE id > $1 AND (org_id IS NULL OR org_id = $2) ORDER BY stream_event.id DESC LIMIT ${REPLAY_LIMIT}) t
   ORDER BY id::bigint`;

interface Client {
  orgId: string;
  userId: string;
  sessionId: string | null;
  staff: boolean;
  res: ServerResponse;
  lastId: bigint;
  pending: StreamRow[] | null; // live rows that arrived while the replay was running
}

export interface StreamOptions {
  connectionString: string; heartbeatMs: number; recheckMs: number; log: FastifyBaseLogger;
  /** Re-check of a connected client (membership and a live product role that grants the stream); default: membership only. */
  allowed?: (who: { orgId: string; userId: string; staff: boolean }) => Promise<boolean>;
}

export class StreamHub {
  private readonly clients = new Set<Client>();
  private listener: pg.Client | null = null;
  private timers: NodeJS.Timeout[] = [];
  private chain: Promise<void> = Promise.resolve();
  private lastSeen = 0n;
  private stopped = false;
  private retryMs = 1000;

  constructor(private readonly db: Db, private readonly opts: StreamOptions) {}

  get size(): number {
    return this.clients.size;
  }

  async start(): Promise<void> {
    this.stopped = false;
    const res = await this.db.query<{ max: string | null }>(`SELECT max(id)::text AS max FROM stream_event`);
    this.lastSeen = BigInt(res.rows[0]?.max ?? '0');
    await this.listen();
    this.timers.push(setInterval(() => this.heartbeat(), this.opts.heartbeatMs).unref());
    this.timers.push(setInterval(() => void this.recheck(), this.opts.recheckMs).unref());
  }

  async stop(): Promise<void> {
    this.stopped = true;
    for (const t of this.timers) clearInterval(t);
    this.timers = [];
    for (const client of [...this.clients]) this.drop(client);
    const listener = this.listener;
    this.listener = null;
    await listener?.end().catch(() => undefined);
  }

  private async listen(): Promise<void> {
    const client = new pg.Client({ connectionString: this.opts.connectionString, application_name: 'marden-api-stream' });
    client.on('notification', (msg) => {
      if (msg.channel === STREAM_CHANNEL && msg.payload && /^\d+$/.test(msg.payload)) this.enqueue(BigInt(msg.payload));
    });
    client.on('error', (err) => this.reconnect(client, err));
    client.on('end', () => this.reconnect(client, null));
    await client.connect();
    await client.query(`LISTEN ${STREAM_CHANNEL}`);
    this.listener = client;
    this.retryMs = 1000;
  }

  private reconnect(client: pg.Client, err: Error | null): void {
    if (this.stopped || this.listener !== client) return;
    this.listener = null;
    this.opts.log.warn({ err }, 'stream listener lost; reconnecting');
    const delay = this.retryMs;
    this.retryMs = Math.min(this.retryMs * 2, 30_000);
    setTimeout(() => {
      this.listen()
        .then(() => this.catchUp())
        .catch((e: unknown) => {
          this.opts.log.error({ err: e }, 'stream listener reconnect failed');
          if (this.listener === null && !this.stopped) this.reconnect(client, e as Error);
        });
    }, delay).unref();
  }

  /** Rows written while the listener was down. */
  private async catchUp(): Promise<void> {
    const res = await this.db.query<StreamRow>(CATCH_UP_SQL, [this.lastSeen.toString()]);
    for (const row of res.rows) this.enqueue(BigInt(row.id));
  }

  /** Deliveries run one at a time so clients see rows in notification order. */
  private enqueue(id: bigint): void {
    this.chain = this.chain.then(() => this.deliver(id)).catch((err: unknown) => {
      this.opts.log.error({ err, id: id.toString() }, 'stream delivery failed');
    });
  }

  private async deliver(id: bigint): Promise<void> {
    if (id > this.lastSeen) this.lastSeen = id;
    if (this.clients.size === 0) return;
    const res = await this.db.query<StreamRow>(`SELECT id::text, org_id, payload FROM stream_event WHERE id = $1`, [id.toString()]);
    const row = res.rows[0];
    if (!row) return;
    for (const client of this.clients) {
      if (row.org_id !== null && row.org_id !== client.orgId) continue;
      if (client.pending) client.pending.push(row);
      else this.send(client, row);
    }
  }

  private send(client: Client, row: StreamRow): void {
    const id = BigInt(row.id);
    if (id <= client.lastId) return;
    client.lastId = id;
    this.write(client, `id: ${row.id}\ndata: ${JSON.stringify(row.payload)}\n\n`);
  }

  private write(client: Client, chunk: string): void {
    if (client.res.writableEnded || client.res.destroyed) return this.drop(client);
    client.res.write(chunk);
    if (client.res.writableLength > MAX_BUFFERED_BYTES) this.drop(client); // a client that stopped reading
  }

  private heartbeat(): void {
    for (const client of this.clients) this.write(client, `: heartbeat ${new Date().toISOString()}\n\n`);
  }

  /** Spec 23: re-check access at least every 10 minutes; a removed member's (or lapsed product's) stream closes. */
  private async recheck(): Promise<void> {
    for (const client of [...this.clients]) {
      const check = this.opts.allowed ? this.opts.allowed(client) : isMember(this.db, client.orgId, client.userId);
      const ok = await check.catch(() => true);
      if (!ok) this.drop(client);
    }
  }

  drop(client: Client): void {
    if (!this.clients.delete(client)) return;
    if (!client.res.writableEnded) client.res.end();
  }

  closeSession(sessionId: string): void {
    for (const c of [...this.clients]) if (c.sessionId === sessionId) this.drop(c);
  }

  closeUser(userId: string): void {
    for (const c of [...this.clients]) if (c.userId === userId) this.drop(c);
  }

  async attach(
    who: { orgId: string; userId: string; sessionId: string | null; staff?: boolean }, res: ServerResponse, lastEventId: bigint | null,
  ): Promise<Client> {
    const { orgId, userId, sessionId } = who;
    const client: Client = { orgId, userId, sessionId, staff: who.staff ?? false, res, lastId: 0n, pending: lastEventId === null ? null : [] };
    this.clients.add(client);
    res.on('close', () => this.drop(client));
    res.write(`retry: 5000\n: connected\n\n`);
    if (lastEventId !== null) await this.replay(client, lastEventId);
    return client;
  }

  private async replay(client: Client, after: bigint): Promise<void> {
    try {
      const res = await this.db.query<StreamRow & { total: number }>(REPLAY_SQL, [after.toString(), client.orgId]);
      // More missed than we replay: tell the client to refetch everything, then send the newest rows.
      if ((res.rows[0]?.total ?? 0) > REPLAY_LIMIT) this.write(client, `event: reset\ndata: {"reason":"replay_truncated"}\n\n`);
      client.lastId = after;
      for (const row of res.rows) this.send(client, row);
    } finally {
      const pending = client.pending ?? [];
      client.pending = null;
      for (const row of pending) this.send(client, row);
    }
  }
}

function lastEventIdOf(header: string | string[] | undefined, query: unknown): bigint | null {
  const raw = (Array.isArray(header) ? header[0] : header) ?? (query as { lastEventId?: string } | undefined)?.lastEventId;
  return raw && /^\d{1,19}$/.test(raw) ? BigInt(raw) : null;
}

export function streamRoutes(app: FastifyInstance, hub: StreamHub): void {
  app.get('/api/stream', async (req, reply) => {
    const auth = authOf(req);
    reply.hijack();
    reply.raw.writeHead(200, {
      ...(reply.getHeaders() as Record<string, string>),
      'content-type': 'text/event-stream; charset=utf-8',
      'cache-control': 'no-cache, no-transform',
      connection: 'keep-alive',
      'x-accel-buffering': 'no',
      'x-request-id': req.id,
    });
    await hub.attach(auth, reply.raw, lastEventIdOf(req.headers['last-event-id'], req.query));
  });
}
