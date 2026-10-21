// Graceful stop on SIGTERM/SIGINT (Railway sends SIGTERM, then SIGKILL after the service's draining
// time): stop accepting connections, close SSE clients (they reconnect to the new deploy and replay
// from Last-Event-ID), let in-flight requests finish, close the pool, exit 0. If that takes longer
// than the timeout the process exits 1 anyway, before the platform has to kill it.
import type { FastifyInstance } from 'fastify';

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
    try {
      await app.close();
      clearTimeout(timer);
      app.log.info('shutdown complete');
      exit(0);
    } catch (err) {
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
