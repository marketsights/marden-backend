// Runs the spec 23 checks on every request: verify the session (401), resolve org, org role and live
// product roles (403), check the route's permission and the granting product's plan (403; no live
// product role at all: 403 no_product). Unmapped routes stop the server from starting.
import type { FastifyInstance, FastifyRequest } from 'fastify';
import type { Db } from '../db.js';
import { forbidden, noProduct, unauthorized } from '../errors.js';
import { DEV_IDENTITY, resolveAccess, type AuthContext } from './access.js';
import { createActivityRecorder } from './activity.js';
import { sessionToken, type TokenVerifier } from './clerk.js';
import { decide, type Denial } from './grants.js';
import { routeKey, ruleFor, unmappedRoutes } from './permissions.js';
import type { ProductRegistry } from './products/index.js';

declare module 'fastify' {
  interface FastifyRequest {
    auth: AuthContext | null;
  }
  interface FastifyInstance {
    registeredRoutes: Set<string>;
  }
}

export interface AuthOptions {
  db: Db;
  devMode: boolean;
  verify: TokenVerifier | null;
  products: ProductRegistry;
}

function refusal(reason: Denial) {
  if (reason === 'no_product') return noProduct();
  return forbidden(reason === 'role' ? 'role_lacks_permission' : 'plan_lacks_permission');
}

/** The caller's context on a route that is not public. */
export function authOf(req: FastifyRequest): AuthContext {
  if (!req.auth) throw unauthorized('signed_out');
  return req.auth;
}

/**
 * Collects every route as it is registered and refuses to start if one has no rule. Must be called
 * before any plugin or route is registered.
 */
export function trackRoutes(app: FastifyInstance): void {
  const registered = new Set<string>();
  app.decorate('registeredRoutes', registered);

  app.addHook('onRoute', (route) => {
    const methods = Array.isArray(route.method) ? route.method : [route.method];
    for (const method of methods) registered.add(routeKey(method, route.url));
  });

  app.addHook('onReady', (done) => {
    const missing = unmappedRoutes(registered);
    done(missing.length > 0
      ? new Error(`Refusing to start: routes without a permission rule (default deny): ${missing.join(', ')}`)
      : undefined);
  });
}

/** The per-request checks. Registered after CORS and security headers so a 401/403 still carries them. */
export function registerAuth(app: FastifyInstance, opts: AuthOptions): void {
  app.decorateRequest('auth', null);
  const activity = createActivityRecorder(opts.db);
  app.addHook('onRequest', async (req) => {
    const url = req.routeOptions.url;
    if (!url) return; // no such route: the not-found handler answers 404
    const rule = ruleFor(req.method, url);
    if (!rule) throw forbidden('no_permission_rule');
    if (rule.scope === 'public') return;
    const identity = opts.devMode ? DEV_IDENTITY : await identify(req, opts.verify);
    const ctx = await resolveAccess(opts.db, identity, opts.devMode);
    // Last active (spec 13): any request by a member counts, allowed or not; never fails the request.
    await activity.touch(ctx.orgId, ctx.userId).catch((err: unknown) => req.log.warn({ err }, 'last_active_at not recorded'));
    const decision = decide(rule, ctx, opts.products);
    if (!decision.ok) throw refusal(decision.reason);
    req.auth = ctx;
    req.log = req.log.child({ userId: ctx.userId, orgId: ctx.orgId });
  });
}

async function identify(req: FastifyRequest, verify: TokenVerifier | null) {
  if (!verify) throw unauthorized('auth_not_configured');
  const token = sessionToken(req);
  if (!token) throw unauthorized('missing_token');
  return verify(token);
}
