// Clerk session token verification (spec 23 step 1): signature, expiry, not-before, issuer and
// authorized party. Networkless with CLERK_JWT_KEY; otherwise JWKS fetched with CLERK_SECRET_KEY.
import { verifyToken } from '@clerk/backend';
import type { FastifyRequest } from 'fastify';
import type { ClerkConfig } from '../config.js';
import { unauthorized } from '../errors.js';

export interface Identity {
  userId: string;
  orgId: string | null;      // active Clerk organization id = org.id
  sessionId: string | null;
  mfa: boolean;              // a second factor was verified in this session (fva claim)
}

export type TokenVerifier = (token: string) => Promise<Identity>;

/**
 * Methods a cookie-only session may use. The web app calls the API same-origin (Next.js rewrites
 * /api/*), so Clerk's `__session` cookie reaches it; that is how EventSource (/api/stream) and Excel
 * links (/api/export/*.xlsx) authenticate, since neither can set a header. Both are reads. Writes
 * must carry the bearer, so a cross-site form cannot ride the cookie (CSRF).
 */
const COOKIE_METHODS = new Set(['GET', 'HEAD']);

/** Value of the first non-empty cookie with this name. */
function cookieValue(header: string | undefined, name: string): string | null {
  if (!header) return null;
  for (const part of header.split(';')) {
    const eq = part.indexOf('=');
    if (eq === -1 || part.slice(0, eq).trim() !== name) continue;
    const raw = part.slice(eq + 1).trim();
    let value = raw;
    try {
      value = decodeURIComponent(raw);
    } catch {
      // not URI-encoded: use as is
    }
    if (value) return value;
  }
  return null;
}

/**
 * Session token from `Authorization: Bearer` or, on GET/HEAD, Clerk's same-site `__session` cookie.
 * Either way it is verified the same way (createClerkVerifier). A bearer decides when present: a bad
 * bearer is a 401 even when a valid cookie rides along, never a silent fallback to the cookie.
 */
export function sessionToken(req: FastifyRequest): string | null {
  const header = req.headers.authorization;
  if (header !== undefined && /^Bearer\s/i.test(header)) return header.replace(/^Bearer\s+/i, '').trim() || null;
  if (!COOKIE_METHODS.has(req.method.toUpperCase())) return null;
  return cookieValue(req.headers.cookie, '__session');
}

interface SessionClaims {
  sub?: unknown; sid?: unknown; iss?: unknown; sts?: unknown; fva?: unknown;
  v?: unknown; o?: { id?: unknown } | undefined; org_id?: unknown;
}

const str = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null);

export function identityFromClaims(claims: SessionClaims, issuer: string | null): Identity {
  const userId = str(claims.sub);
  if (!userId) throw unauthorized('token_without_subject');
  if (issuer && claims.iss !== issuer) throw unauthorized('wrong_issuer');
  if (claims.sts === 'pending') throw unauthorized('session_pending');
  const orgId = claims.v === 2 ? str(claims.o?.id) : str(claims.org_id);
  const fva = Array.isArray(claims.fva) ? (claims.fva as unknown[]) : [];
  const secondFactorAge = typeof fva[1] === 'number' ? fva[1] : -1;
  return { userId, orgId, sessionId: str(claims.sid), mfa: secondFactorAge >= 0 };
}

export function createClerkVerifier(clerk: ClerkConfig, authorizedParties: string[]): TokenVerifier {
  return async (token: string) => {
    // verifyToken throws on a bad signature, expiry, nbf, header type or authorized party.
    const claims = await verifyToken(token, {
      ...(clerk.jwtKey ? { jwtKey: clerk.jwtKey } : {}),
      ...(clerk.secretKey ? { secretKey: clerk.secretKey } : {}),
      authorizedParties,
    }).catch(() => null);
    if (!claims) throw unauthorized('invalid_token');
    return identityFromClaims(claims, clerk.issuer);
  };
}
