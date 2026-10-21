// A minimal SSE client over a real socket for the stream tests.
import http from 'node:http';

export interface Sse { chunks: () => string; close: () => void; status: number; headers: http.IncomingHttpHeaders }

export function connect(port: number, lastEventId?: string, headers: Record<string, string> = {}): Promise<Sse> {
  return new Promise((resolve, reject) => {
    let buf = '';
    const req = http.get(
      { host: '127.0.0.1', port, path: '/api/stream', headers: { ...headers, ...(lastEventId ? { 'last-event-id': lastEventId } : {}) } },
      (res) => {
        res.setEncoding('utf8');
        res.on('data', (d: string) => (buf += d));
        resolve({ chunks: () => buf, close: () => req.destroy(), status: res.statusCode ?? 0, headers: res.headers });
      },
    );
    req.on('error', reject);
  });
}

export async function until(check: () => boolean, ms = 5000): Promise<void> {
  const start = Date.now();
  while (!check()) {
    if (Date.now() - start > ms) throw new Error('timed out waiting for stream data');
    await new Promise((r) => setTimeout(r, 25));
  }
}

/** The `id:` / `data:` events in a stream transcript. */
export const events = (text: string): { id: string; data: unknown }[] =>
  text.split('\n\n').flatMap((block) => {
    const id = /^id: (\d+)$/m.exec(block)?.[1];
    const data = /^data: (.*)$/m.exec(block)?.[1];
    return id && data ? [{ id, data: JSON.parse(data) as unknown }] : [];
  });
