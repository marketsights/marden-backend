// Request logging without secrets. Fastify logs every request's URL ("incoming request"), query string
// included, and some URLs carry a credential: the alert-email unsubscribe link (?token=, a signed
// capability that switches a member's email off) and anything a client sends as ?key= / ?secret=.
// The request serializer masks those values; headers are not logged at all (and the logger also redacts
// authorization, cookie and svix-signature should a header ever be logged).
import type { FastifyRequest } from 'fastify';

export const MASK = '***';

/** Query parameter names whose values are credentials (case-insensitive). */
const SECRET_PARAM = /^(token|access[_-]?token|id[_-]?token|api[_-]?key|apikey|key|secret|client[_-]?secret|password|passwd|signature|sig|session|__session|code)$/i;

/** The URL with every secret query value replaced by ***; the path and other parameters unchanged. */
export function redactUrl(url: string): string {
  const q = url.indexOf('?');
  if (q === -1) return url;
  const hash = url.indexOf('#', q);
  const query = url.slice(q + 1, hash === -1 ? undefined : hash);
  const masked = query
    .split('&')
    .map((pair) => {
      const eq = pair.indexOf('=');
      if (eq === -1) return pair;
      let name = pair.slice(0, eq);
      try {
        name = decodeURIComponent(name.replace(/\+/g, ' '));
      } catch {
        // not URI-encoded: test as is
      }
      return SECRET_PARAM.test(name.trim()) ? `${pair.slice(0, eq)}=${MASK}` : pair;
    })
    .join('&');
  return `${url.slice(0, q)}?${masked}${hash === -1 ? '' : url.slice(hash)}`;
}

// A type alias, not an interface: Fastify's serializer type has an index signature, which an interface
// does not satisfy.
export type LoggedRequest = {
  method: string;
  url: string;
  host: string | undefined;
  remoteAddress: string | undefined;
  remotePort: number | undefined;
};

/** Fastify's default request serializer, with the URL redacted. */
export function serializeRequest(req: FastifyRequest): LoggedRequest {
  return {
    method: req.method,
    url: redactUrl(req.url),
    host: req.host,
    remoteAddress: req.ip,
    remotePort: req.socket?.remotePort,
  };
}

/** Logger options for the app: level, the redacting request serializer and header redaction. */
export function loggerOptions(level: string) {
  return {
    level,
    serializers: { req: serializeRequest },
    redact: ['req.headers.authorization', 'req.headers.cookie', 'req.headers["svix-signature"]'],
  };
}
