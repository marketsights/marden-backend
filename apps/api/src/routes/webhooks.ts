// POST /api/webhooks/clerk: svix-signed Clerk events keep org / app_user / membership current.
// 400 on a bad or missing signature, 503 while CLERK_WEBHOOK_SIGNING_SECRET is unset, 204 otherwise.
import type { FastifyInstance } from 'fastify';
import { Webhook } from 'svix';
import type { Db } from '../db.js';
import { badRequest, unavailable } from '../errors.js';
import { applyClerkEvent, eventTime, type ClerkEvent } from '../queries/clerk-sync.js';

export interface StreamCloser {
  closeSession(sessionId: string): void;
  closeUser(userId: string): void;
}

const header = (v: string | string[] | undefined): string => (Array.isArray(v) ? (v[0] ?? '') : (v ?? ''));

function parseEvent(raw: unknown): ClerkEvent {
  const evt = raw as Partial<ClerkEvent> | null;
  if (!evt || typeof evt.type !== 'string' || !evt.data || typeof evt.data !== 'object') throw badRequest('not_a_clerk_event');
  return { type: evt.type, data: evt.data, ...(typeof evt.timestamp === 'number' ? { timestamp: evt.timestamp } : {}) };
}

export function webhookRoutes(app: FastifyInstance, deps: { db: Db; secret: string | null; streams: StreamCloser }): void {
  const verifier = deps.secret ? new Webhook(deps.secret) : null;

  // Signature verification needs the exact bytes, so this scope keeps JSON bodies as strings.
  void app.register((scope, _opts, registered) => {
    scope.removeContentTypeParser('application/json');
    scope.addContentTypeParser('application/json', { parseAs: 'string', bodyLimit: 1024 * 1024 }, (_req, body, done) => {
      done(null, body);
    });

    scope.post('/api/webhooks/clerk', async (req, reply) => {
      if (!verifier) throw unavailable('webhook_secret_not_configured');
      const headers = {
        'svix-id': header(req.headers['svix-id']),
        'svix-timestamp': header(req.headers['svix-timestamp']),
        'svix-signature': header(req.headers['svix-signature']),
      };
      const raw = typeof req.body === 'string' ? req.body : '';
      try {
        verifier.verify(raw, headers); // throws on a bad, missing or expired signature
      } catch {
        throw badRequest('bad_signature');
      }
      let payload: unknown;
      try {
        payload = JSON.parse(raw);
      } catch {
        throw badRequest('invalid_json');
      }
      const evt = parseEvent(payload);
      const msg = { id: headers['svix-id'], at: eventTime(evt, Number(headers['svix-timestamp'])) };
      const result = await deps.db.tx((c) => applyClerkEvent(c, evt, msg));
      req.log.info({ type: evt.type, svixId: msg.id, outcome: result.outcome }, 'clerk webhook');
      if (result.sessionId) deps.streams.closeSession(result.sessionId);
      if (result.userId) deps.streams.closeUser(result.userId);
      return reply.code(204).send();
    });
    registered();
  });
}
