// Request logs carry no credential: the unsubscribe link's ?token= (a signed capability) and other
// secret-looking query values are masked in the logged URL; headers are never logged.
import { Writable } from 'node:stream';
import Fastify from 'fastify';
import { describe, expect, it } from 'vitest';
import { loggerOptions, MASK, redactUrl } from '../src/lib/log-redact.js';

describe('redactUrl', () => {
  it('masks secret query values and keeps the path and every other parameter', () => {
    expect(redactUrl('/api/email/unsubscribe?token=abc.def')).toBe(`/api/email/unsubscribe?token=${MASK}`);
    expect(redactUrl('/api/x?asOf=2026-10-20&api_key=k1&tab=debt&Signature=s')).toBe(
      `/api/x?asOf=2026-10-20&api_key=${MASK}&tab=debt&Signature=${MASK}`,
    );
    expect(redactUrl('/api/x?%74oken=abc')).toBe(`/api/x?%74oken=${MASK}`); // encoded name
    expect(redactUrl('/api/x?token=a&token=b#frag')).toBe(`/api/x?token=${MASK}&token=${MASK}#frag`);
  });

  it('leaves URLs without secrets alone', () => {
    for (const url of ['/healthz', '/api/sites?asOf=2026-10-20&q=abilene', '/api/x?flag', '/api/x?tokens=1', '/api/x?']) {
      expect(redactUrl(url)).toBe(url);
    }
  });
});

describe('the API logger', () => {
  it('logs the request URL with the unsubscribe token masked and no header values', async () => {
    const lines: string[] = [];
    const stream = new Writable({ write(chunk, _enc, done) { lines.push(String(chunk)); done(); } });
    const app = Fastify({ logger: { ...loggerOptions('info'), stream } });
    app.get('/api/email/unsubscribe', (_req, reply) => reply.send({ ok: true }));
    try {
      const res = await app.inject({
        method: 'GET',
        url: '/api/email/unsubscribe?token=SIGNED.CAPABILITY.TOKEN',
        headers: { authorization: 'Bearer sekrit-bearer', cookie: '__session=sekrit-cookie' },
      });
      expect(res.statusCode).toBe(200);
    } finally {
      await app.close();
    }
    const log = lines.join('');
    expect(log).toContain(`"url":"/api/email/unsubscribe?token=${MASK}"`);
    for (const secret of ['SIGNED.CAPABILITY.TOKEN', 'sekrit-bearer', 'sekrit-cookie']) expect(log).not.toContain(secret);
  });
});
