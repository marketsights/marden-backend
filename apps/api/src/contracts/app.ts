// Response shapes for endpoints whose payload handoff/api/types.ts leaves open
// (me, account, sources health, search, saved stress, home, track record, GPU index). types.ts stays a
// verbatim copy of the handoff.
import type {
  ChainStatus, Evidence, GpuModelDetail, GpuPricePoint, GpuProviderPrice, LinkStatus, Sourced, StressRequest,
} from './types.js';

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
  savedAt: string;                                     // = createdAt; the name the web app's SavedStress reads
}

// ---------- Monitor area (specs 01, 12, 19): read models home, track_record, gpus ----------

/** A watchlist entry a home piece belongs to: "site:<slug>" or "instrument:<id>". */
export type WatchKey = string;

/**
 * One row of "What changed on your watchlist" (spec 01 Rules: cushion change, price change at or above the
 * user's threshold, link status change, new filing on a watched name). status: the current chain status
 * (null until the status model has run for the site).
 */
export interface HomeChange {
  kind: 'site' | 'instrument'; refId: string; name: string;
  type: 'cushion' | 'price' | 'status' | 'filing';
  label?: string;                                      // status: "End-user link"; filing: the form ("8-K")
  from: number | string | null;                        // cushion months, price, link status; null for a filing
  to: Sourced<number | string>;
  status: ChainStatus | null;
  record: Evidence;
}
export interface HomeCatalyst { when: string; what: string; refUrl?: string }

/**
 * GET /api/home source: the `home` read model (key 'all'). Every site's and instrument's pieces for the
 * week, each with the watchlist entries it belongs to; the API keeps the org's (lib/home-doc.ts).
 */
export interface HomeDoc {
  asOf: string;
  week: { start: string; end: string };
  under3: { watch: WatchKey[]; site?: string; name: string; debtB: Sourced<number>; cushionMonths: Sourced<number> }[];
  cushionKnown: WatchKey[];
  statusChanges: { watch: WatchKey[]; name: string; line: string; at: string }[];
  fundMarksHigh: { watch: WatchKey[]; name: string; gapPts: Sourced<number> }[];
  changes: (HomeChange & { watch: WatchKey[]; severity: number })[];
  comingUp: (HomeCatalyst & { watch: WatchKey[]; date: string; refUrl: string })[];
}

/** GET /api/home (spec 01): the org's cut of HomeDoc plus the caller's unread alerts. */
export interface HomeResponse {
  asOf: string;
  lastVisit: string | null;                            // previous session start; null until sessions are stored
  headline: {
    watchedDebtUnder3: { debtB: Sourced<number>; names: string[] };
    statusChangesThisWeek: { count: number; names: string[] };
    fundMarksHigh: { count: number; topName: string | null; gapPts: Sourced<number> | null };
  };
  unreadAlerts: { count: number; latestAt: string | null };
  watched: { sites: string[]; instruments: string[] };
  watchlistChanges: HomeChange[];
  comingUp: HomeCatalyst[];
}

/** GET /api/track-record (spec 12): the backtest's cases, the fixed rules and the models. */
export interface TrackRecordCase {
  name: string; instrumentId: string | null; flagged: string | null; moved: string | null;
  leadDays: number | null; backtestRunning: boolean;
}
export interface StatusRuleRow { link: 'End user' | 'Tenant' | 'Developer' | 'Lenders'; amberCode: string; amberWhen: string; redCode: string; redWhen: string }
export interface MaterialRule { code: string; rule: string; alert: string | null; severity: LinkStatus }
export interface ModelRow { model: string; output: string; version: string; builtFrom: string; cadence: string; methodUrl: string | null; lastRun: string }
export interface TrackRecord {
  asOf: string; published: number; total: number; cases: TrackRecordCase[];
  rules: StatusRuleRow[]; material: MaterialRule[]; statusMethod: { version: string; url: string };
  models: ModelRow[];
}

/** GET /api/gpus (spec 19 GPU index): index points, today's provider prices, value per unit by age. */
export interface GpuValueCurve { model: string; launch: Sourced<string>; newPrice: Sourced<number>; points: GpuModelDetail['valueByAgeYears'] }
export interface GpuIndexPage { asOf: string; index: GpuPricePoint[]; providers: GpuProviderPrice[]; valueByAge: GpuValueCurve[] }
