// HTTP errors with a stable JSON body: { error: <code>, ...details }.

export class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly details: Record<string, unknown> = {},
  ) {
    super(code);
  }
}

/** The read model for this view/key has not been built yet. Never filled with made-up data. */
export const notReady = (view: string, key: string): HttpError => new HttpError(503, 'not_ready', { view, key });

export const notFound = (what?: string): HttpError => new HttpError(404, 'not_found', what ? { what } : {});

export const unauthorized = (reason: string): HttpError => new HttpError(401, 'unauthorized', { reason });

export const forbidden = (reason: string): HttpError => new HttpError(403, 'forbidden', { reason });

/** The caller's org holds no live product, or the caller has no role on one (docs/access-control.md). */
export const noProduct = (): HttpError => new HttpError(403, 'no_product');

export const badRequest = (reason: string, issues?: unknown): HttpError =>
  new HttpError(400, 'bad_request', issues === undefined ? { reason } : { reason, issues });

export const conflict = (reason: string): HttpError => new HttpError(409, 'conflict', { reason });

export const unavailable = (reason: string): HttpError => new HttpError(503, 'unavailable', { reason });
