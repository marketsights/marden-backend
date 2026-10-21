// Graceful stop on SIGTERM/SIGINT (Railway sends SIGTERM, then SIGKILL after the service's draining
// time): stop accepting connections, close SSE clients (they reconnect to the new deploy and replay
// from Last-Event-ID), let in-flight requests finish, close the pool, exit 0. If that takes longer
// than the timeout the process exits 1 anyway, before the platform has to kill it.
//
// Keep-alive: server.close() waits for every open connection, and Fastify closes only the connections
// that are idle at that moment. A connection whose request finishes during the drain (Railway's edge
// and Next's /api rewrite both keep connections alive) would hold the close open until the timeout, so
// idle connections are swept every IDLE_SWEEP_MS until the close completes.
import type { FastifyInstance } from 'fastify';

/** How often a draining server closes connections that went idle after their last response. */
export const IDLE_SWEEP_MS = 200;

export interface ShutdownOptions {
  timeoutMs: number;
  /** process.exit by default; tests pass a recorder. */
  exit?: (code: number) => void;
}

/** Returns the handler that installShutdown registers (exported for tests). */
export function shutdownHandler(app: FastifyInstance, opts: ShutdownOptions): (signal: string) => Promise<void> {
  const exit = opts.exit ?? ((code: number) => process.exit(code));
  let started = false;
  return async (signal: string) => {
    if (started) return;
    started = true;
    app.log.info({ signal, timeoutMs: opts.timeoutMs }, 'shutting down: draining requests');
    const timer = setTimeout(() => {
      app.log.error({ timeoutMs: opts.timeoutMs }, 'shutdown timed out; exiting');
      exit(1);
    }, opts.timeoutMs);
    timer.unref();
    const sweep = setInterval(() => app.server.closeIdleConnections(), IDLE_SWEEP_MS);
    sweep.unref();
    try {
      await app.close();
      clearInterval(sweep);
      clearTimeout(timer);
      app.log.info('shutdown complete');
      exit(0);
    } catch (err) {
      clearInterval(sweep);
      clearTimeout(timer);
      app.log.error({ err }, 'shutdown failed');
      exit(1);
    }
  };
}

export function installShutdown(app: FastifyInstance, opts: ShutdownOptions): void {
  const handler = shutdownHandler(app, opts);
  for (const signal of ['SIGTERM', 'SIGINT'] as const) {
    process.once(signal, () => void handler(signal));
  }
}
