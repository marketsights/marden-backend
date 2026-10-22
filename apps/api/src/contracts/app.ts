// Response shapes for endpoints whose payload handoff/api/types.ts leaves open
// (me, account, sources health, search, saved stress, home, track record, GPU index). types.ts stays a
// verbatim copy of the handoff.
import type * as GeoJSON from 'geojson';
import type {
  ChainStatus, DataCenterSummary, Evidence, GpuModelDetail, GpuPricePoint, GpuProviderPrice, LinkStatus, RiskList, Sourced, StressRequest,
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
export interface TeamMember {
  userId: string; email: string; name: string | null; role: Role; products: MemberProductRole[];
  lastActiveAt: string | null;                         // last request in this org (membership.last_active_at); null = n/d
}
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
  security: {
    authProvider: 'clerk' | 'dev';
    mfaEnabled: boolean | null;                        // the caller's own second factor (Clerk); null when the API cannot ask Clerk
    sso: OrgSso | null;                                // the org's Clerk enterprise connection; null when the API cannot ask Clerk
  };
}
/** Single sign-on for the org (spec 13, 23): its Clerk enterprise connections (SAML or OIDC). */
export interface OrgSso { connected: boolean; name: string | null; provider: string | null }

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

// GET / POST /api/saved-views, DELETE /api/saved-views/:id (spec 03 "Saved views"; specs 14, 15)
export type SavedViewScreen = 'completion' | 'data-centers' | 'power';
export interface SavedView {
  id: string;
  screen: string;                                      // a SavedViewScreen; the stored column is text
  name: string;
  query: string;                                       // the screen's URL query without '?', e.g. 'tenant=Oracle&sort=-debt'
  createdAt: string;
}

// ---------- Data centers (spec 14) ----------

/**
 * GET /api/data-centers/summary as served: types.ts DataCenterSummary plus the counts the KPI strip and
 * footer show ("30 of 72 buildings built", "facility MW known for 10 of 16 campuses (62%)").
 * coverage[].known: campuses with the field on record; 'buildings' = campuses with a building record.
 */
export interface DataCenterSummaryResponse extends Omit<DataCenterSummary, 'coverage'> {
  buildingsBuilt: number;
  coverage: { field: string; pct: number; known: number }[];
}

/** One ISO/RTO service area (HIFLD "Independent System Operators", tag F), simplified for the map. */
export interface IsoBoundaryProps {
  id: string; label: string; name: string;
  labelPoint: [number, number] | null;                 // [lon, lat] inside the largest part: where the name is drawn
  source: Sourced<string>;
}
/** GET /api/grid/iso-rto: the US ISO/RTO boundaries layer of the Data centers map (spec 14 Map view). */
export interface IsoBoundaries {
  type: 'FeatureCollection';
  features: { type: 'Feature'; id: string; geometry: GeoJSON.Polygon | GeoJSON.MultiPolygon; properties: IsoBoundaryProps }[];
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
  lastVisit: string | null;                            // previous session start (membership.last_visit_at); null until seen
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

/**
 * GET /api/risk-list?week= (spec 10): the week's published list as frozen (types.ts RiskList) plus every
 * week published so far (Mondays, oldest first) for the week picker. ?week= takes any day of its week.
 */
export interface RiskListPage extends RiskList { published: string[] }

/** GET /api/track-record (spec 12): the backtest's cases, the fixed rules and the models. */
export interface TrackRecordCase {
  name: string; instrumentId: string | null; flagged: string | null; moved: string | null;
  leadDays: number | null; backtestRunning: boolean;
  flagBasis: 'snapshot' | 'reconstructed' | null;      // a flag from a live snapshot, or a point-in-time rerun
}
export interface StatusRuleRow { link: 'End user' | 'Tenant' | 'Developer' | 'Lenders'; amberCode: string; amberWhen: string; redCode: string; redWhen: string }
export interface MaterialRule { code: string; rule: string; alert: string | null; severity: LinkStatus }
export interface ModelRow { model: string; output: string; version: string; builtFrom: string; cadence: string; methodUrl: string | null; lastRun: string }
export interface TrackRecord {
  asOf: string; published: number; total: number; cases: TrackRecordCase[];
  rules: StatusRuleRow[]; material: MaterialRule[]; statusMethod: { version: string; url: string };
  backtest: { methodVersion: string; methodUrl: string | null; flagBasis: string | null; asOf: string } | null;
  models: ModelRow[];
}

/** GET /api/gpus (spec 19 GPU index): index points, today's provider prices, value per unit by age. */
export interface GpuValueCurve { model: string; launch: Sourced<string>; newPrice: Sourced<number>; points: GpuModelDetail['valueByAgeYears'] }
export interface GpuIndexPage { asOf: string; index: GpuPricePoint[]; providers: GpuProviderPrice[]; valueByAge: GpuValueCurve[] }

// GET /api/alert-delivery (spec 13 Alert delivery): whether alert email can go out and what happened to
// the caller's recent alert emails (email_outbox, written by the worker). `email` is what the worker last
// saw of its email configuration; configured false = no email is sent, rows end 'not_configured'.
export type AlertEmailKind = 'instant' | 'daily_digest' | 'weekly_digest';
export type AlertEmailStatus = 'queued' | 'sent' | 'failed' | 'not_configured' | 'cancelled';
export interface AlertEmail {
  id: string; kind: AlertEmailKind; subject: string; status: AlertEmailStatus; alerts: number;
  createdAt: string; sentAt: string | null; attempts: number;
  error: string | null;                                // why it failed or was cancelled (no secrets)
}
export interface AlertDelivery {
  email: {
    configured: boolean; provider: 'resend' | 'postmark' | null;
    checkedAt: string | null;                          // null = the worker has not checked yet
    reason: string | null;                             // staff only: the missing variable names (never values)
  };
  to: string;                                          // the address alert email goes to (the member's email)
  counts: Record<AlertEmailStatus, number>;            // the caller's alert emails of the last 30 days
  recent: AlertEmail[];                                // newest first, at most 10
}
