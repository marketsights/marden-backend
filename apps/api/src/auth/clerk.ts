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

/** Session token from `Authorization: Bearer` or Clerk's same-site `__session` cookie. */
export function sessionToken(req: FastifyRequest): string | null {
  const header = req.headers.authorization;
  if (header?.startsWith('Bearer ')) return header.slice(7).trim() || null;
  const cookie = req.headers.cookie;
  if (!cookie) return null;
  for (const part of cookie.split(';')) {
    const [name, ...rest] = part.trim().split('=');
    if (name === '__session' && rest.length > 0) return decodeURIComponent(rest.join('=')) || null;
  }
  return null;
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
