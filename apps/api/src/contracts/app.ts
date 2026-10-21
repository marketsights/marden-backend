// Response shapes for endpoints whose payload handoff/api/types.ts leaves open
// (me, account, sources health, search, saved stress). types.ts stays a verbatim copy of the handoff.
import type { StressRequest } from './types.js';

export type Role = 'owner' | 'admin' | 'member' | 'viewer';
export type Plan = 'trial' | 'design_partner' | 'paid';

export interface NotReadyBody { error: 'not_ready'; view: string; key: string }

export type ProductStatus = 'active' | 'suspended' | 'ended';

// GET /api/me (docs/access-control.md). products: the org's live products (active, within dates);
// role is the caller's role on it, null when none was assigned. [] = the org has no live product, and
// every data route answers 403 { error: 'no_product' }.
export interface MeProduct {
  id: string; name: string; plan: Plan; status: ProductStatus; role: Role | null;
  seats: number | null;                                // null = unlimited
  seatsUsed: number;
}
export interface MeResponse {
  user: { id: string; email: string; name: string | null };
  org: { id: string; name: string };
  orgRole: Role;                                       // membership.role: org administration only
  products: MeProduct[];
  permissions: string[];
  staff: boolean;
}

// GET /api/account, PATCH /api/account/members/:id(/products)
export interface MemberProductRole { productId: string; role: Role }
export interface TeamMember { userId: string; email: string; name: string | null; role: Role; products: MemberProductRole[] }
/** One of the org's products, live or not (POST/PATCH /api/admin/orgs/:orgId/products answer this too). */
export interface OrgProduct {
  id: string; name: string; plan: Plan; status: ProductStatus;
  seats: number | null; seatsUsed: number;
  startsOn: string; endsOn: string | null;             // UTC calendar days; endsOn inclusive, null = open-ended
  live: boolean;                                       // active and within dates today: grants access
}
export interface AccountResponse {
  org: { id: string; name: string; createdAt: string };
  team: TeamMember[];
  products: OrgProduct[];
  seats: { used: number; total: number | null };       // members of the org; per-product seats are in products
  security: { authProvider: 'clerk' | 'dev'; mfaEnabled: boolean | null };
}

// GET /api/sources/health
export type SourceStatus = 'ok' | 'delayed' | 'down';
export interface SourceHealth {
  id: string; name: string; kind: string; cadenceMinutes: number | null;
  blocked: boolean; blockedReason: string | null;
  lastOkAt: string | null; lastErrorAt: string | null; lastError: string | null;
  lastRun: { startedAt: string; finishedAt: string | null; ok: boolean | null; itemsSeen: number; itemsNew: number; error: string | null } | null;
  status: SourceStatus;
}
export interface SourcesHealthResponse { asOf: string; sources: SourceHealth[] }

// GET /api/search?q=
export type SearchKind = 'site' | 'instrument' | 'campus' | 'building' | 'plant' | 'entity' | 'permit';
export interface SearchResult { kind: SearchKind; id: string; label: string; sub: string | null; href: string | null; score: number }
export interface SearchResponse { q: string; results: SearchResult[] }

// POST/GET /api/stress/saved
export interface SavedStress {
  id: string; name: string; request: StressRequest | { scenario: 'contagion'; params: Record<string, number | string>; severity: 'base' | 'severe' };
  result: unknown;                                     // filled by the stress model's nightly run; null until then
  ranAt: string | null; createdAt: string; createdBy: string | null;
}
