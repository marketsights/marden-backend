// Listen on HOST:PORT. The image sets HOST=:: (IPv6 and IPv4), which Railway's private network needs
// (web -> api.railway.internal). A host whose kernel has no IPv6 refuses `::` with EAFNOSUPPORT; then
// the API listens on 0.0.0.0 (IPv4 only) and says so, instead of failing to start.
import type { FastifyInstance } from 'fastify';

export interface ListenTarget {
  host: string;
  port: number;
}

type Listen = (target: ListenTarget) => Promise<unknown>;

export const IPV4_ANY = '0.0.0.0';

/** Returns the host actually bound. `listen` is app.listen by default; tests pass their own. */
export async function listenWithFallback(app: FastifyInstance, target: ListenTarget, listen?: Listen): Promise<string> {
  const bind: Listen = listen ?? ((t) => app.listen(t));
  try {
    await bind(target);
    return target.host;
  } catch (err) {
    if (target.host !== '::' || (err as NodeJS.ErrnoException).code !== 'EAFNOSUPPORT') throw err;
    app.log.warn({ host: target.host }, `IPv6 is not available on this host; listening on ${IPV4_ANY} instead`);
    await bind({ host: IPV4_ANY, port: target.port });
    return IPV4_ANY;
  }
}
