// Response shapes for endpoints whose payload handoff/api/types.ts leaves open
// (me, account, sources health, search, saved stress). types.ts stays a verbatim copy of the handoff.
import type { StressRequest } from './types.js';

export type Role = 'owner' | 'admin' | 'member' | 'viewer';
export type Plan = 'trial' | 'design_partner' | 'paid';

export interface NotReadyBody { error: 'not_ready'; view: string; key: string }

// GET /api/me
export interface MeResponse {
  user: { id: string; email: string; name: string | null };
  org: { id: string; name: string; plan: Plan };
  role: Role;
  staff: boolean;
  permissions: string[];
}

// GET /api/account
export interface TeamMember { userId: string; email: string; name: string | null; role: Role }
export interface AccountResponse {
  org: { id: string; name: string; plan: Plan; createdAt: string };
  team: TeamMember[];
  seats: { used: number; total: number | null };       // seat count is not stored yet: null = n/d
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
