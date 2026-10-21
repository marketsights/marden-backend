// Alert email delivery (spec 13; docs/methods/alerts-v0.1.md).
//   GET  /api/alert-delivery       the caller's email delivery state: configured or not, recent emails
//   GET  /api/email/unsubscribe    public: the page an email's unsubscribe link opens (asks to confirm;
//                                  a GET never changes anything, so mail scanners that follow links cannot)
//   POST /api/email/unsubscribe    public: switches the token's emails off; the RFC 8058 one-click POST
//                                  (List-Unsubscribe-Post) and the page's button both land here
// The token (?token=) is signed by the worker with ALERT_LINK_SECRET. Without that secret the links answer
// 503 and say so; nothing pretends to have unsubscribed.
import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';
import { authOf } from '../auth/plugin.js';
import type { Db } from '../db.js';
import { SCOPE_LABEL, verifyUnsubscribe, type UnsubscribePayload } from '../lib/unsubscribe-token.js';
import { alertDelivery, applyUnsubscribe } from '../queries/alert-delivery.js';

const PAGE_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'";
const MANAGE = '/account?tab=alerts';

const escape = (s: string): string =>
  s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');

function page(title: string, body: string): string {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">`
    + `<meta name="robots" content="noindex"><title>${escape(title)} · Marden</title></head>`
    + `<body style="margin:0;padding:40px 16px;background:#f9fafb;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#101828">`
    + `<main style="max-width:480px;margin:0 auto;background:#fff;border:1px solid #e4e7ec;border-radius:8px;padding:24px">`
    + `<div style="font-size:18px;font-weight:700;margin-bottom:12px">Marden</div><h1 style="font-size:20px;margin:0 0 12px">${escape(title)}</h1>`
    + `${body}<p style="font-size:14px;color:#475467;margin-top:20px"><a href="${MANAGE}" style="color:#1849a9">Manage alert email</a> `
    + `(sign in, Account &gt; Alerts). In-app alerts on your watchlist are not affected.</p></main></body></html>`;
}

const p = (text: string): string => `<p style="font-size:15px;line-height:1.5">${escape(text)}</p>`;

function send(reply: FastifyReply, status: number, title: string, body: string): FastifyReply {
  return reply.code(status).header('content-security-policy', PAGE_CSP).header('cache-control', 'no-store')
    .header('referrer-policy', 'no-referrer').type('text/html; charset=utf-8').send(page(title, body));
}

type Checked = { ok: true; payload: UnsubscribePayload } | { ok: false; status: number; title: string; text: string };

function check(req: FastifyRequest, secret: string | null): Checked {
  if (!secret) {
    req.log.warn('unsubscribe link used but ALERT_LINK_SECRET is not set on the API');
    return { ok: false, status: 503, title: 'Unsubscribe is not available right now',
      text: 'Unsubscribe links are not configured on this server yet, so nothing was changed. Sign in to switch alert email off.' };
  }
  const raw = (req.query as Record<string, unknown> | undefined)?.token;
  const payload = typeof raw === 'string' && raw.length <= 1024 ? verifyUnsubscribe(secret, raw) : null;
  if (!payload) {
    return { ok: false, status: 400, title: 'This link is not valid',
      text: 'The unsubscribe link is incomplete or was changed, so nothing was changed. Sign in to manage alert email.' };
  }
  return { ok: true, payload };
}

export function alertDeliveryRoutes(app: FastifyInstance, deps: { db: Db; linkSecret: string | null }): void {
  const { db, linkSecret } = deps;

  app.get('/api/alert-delivery', async (req) => {
    const auth = authOf(req);
    return db.withOrg(auth.orgId, (c) => alertDelivery(c, auth));
  });

  // Mail clients send the one-click POST as a form ("List-Unsubscribe=One-Click"): accept and ignore the body.
  void app.register((scope, _opts, registered) => {
    scope.addContentTypeParser('application/x-www-form-urlencoded', { parseAs: 'string', bodyLimit: 4096 }, (_req, body, done) => {
      done(null, body);
    });
    scope.addContentTypeParser('text/plain', { parseAs: 'string', bodyLimit: 4096 }, (_req, body, done) => {
      done(null, body);
    });

    scope.get('/api/email/unsubscribe', async (req, reply) => {
      const checked = check(req, linkSecret);
      if (!checked.ok) return send(reply, checked.status, checked.title, p(checked.text));
      const label = SCOPE_LABEL[checked.payload.s];
      return send(reply, 200, `Unsubscribe from ${label}?`,
        p(`Marden will stop sending you ${label} for this firm.`)
        + `<form method="post"><button type="submit" style="font-size:15px;padding:8px 16px;border-radius:6px;`
        + `border:1px solid #1849a9;background:#1849a9;color:#fff;cursor:pointer">Unsubscribe</button></form>`);
    });

    scope.post('/api/email/unsubscribe', async (req, reply) => {
      const checked = check(req, linkSecret);
      if (!checked.ok) return send(reply, checked.status, checked.title, p(checked.text));
      const { o: orgId, u: userId, s: scope } = checked.payload;
      const outcome = await db.withOrg(orgId, (c) => applyUnsubscribe(c, { orgId, userId, scope, ip: req.ip }));
      req.log.info({ orgId, userId, scope, outcome }, 'alert email unsubscribe');
      const label = SCOPE_LABEL[scope];
      if (!outcome.ok) {
        return send(reply, 200, 'Nothing to unsubscribe', p('This link is for a firm membership that no longer exists, so no Marden email goes to it.'));
      }
      return send(reply, 200, `Unsubscribed from ${label}`, p(outcome.changed
        ? `Done: Marden will not send you ${label} for this firm.`
        : `You were already unsubscribed from ${label} for this firm.`));
    });
    registered();
  });
}
