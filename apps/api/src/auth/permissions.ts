// The single access matrix (spec 23). Every registered route maps to one permission, a scope and the
// plans allowed; the server refuses to start when a route has no entry (default deny).
import type { Plan, Role } from '../contracts/app.js';

export const ROLES: readonly Role[] = ['owner', 'admin', 'member', 'viewer'];
export const PLANS: readonly Plan[] = ['trial', 'design_partner', 'paid'];

const READ = [
  'home:read', 'completion:read', 'sites:read', 'instruments:read', 'payers:read', 'risk:read',
  'track_record:read', 'sources:read', 'data_centers:read', 'power:read', 'obligations:read',
  'gpus:read', 'leases:read', 'search:read', 'stream:read', 'watchlist:read', 'alerts:read',
  'stress:read', 'prefs:read', 'account:read', 'me:read',
] as const;
const USE = ['watchlist:write', 'alerts:write', 'stress:run', 'stress:save', 'export:xlsx'] as const;
const ADMIN = ['members:invite', 'members:manage'] as const;
const OWNER = ['billing:manage', 'org:delete'] as const;
const STAFF = ['review:approve', 'ops:read'] as const;
/** No session: liveness probe, CORS preflight, signature-verified Clerk webhook. */
export const PUBLIC_PERMISSIONS = ['public:health', 'public:preflight', 'webhook:clerk'] as const;

export type Permission =
  | (typeof READ)[number] | 'prefs:write' | (typeof USE)[number] | (typeof ADMIN)[number]
  | (typeof OWNER)[number] | (typeof STAFF)[number] | (typeof PUBLIC_PERMISSIONS)[number];

/** What each Marden role may do. Viewer: read only plus their own preferences (layout, alert settings). */
export const ROLE_PERMISSIONS: Readonly<Record<Role, readonly Permission[]>> = {
  viewer: [...READ, 'prefs:write'],
  member: [...READ, 'prefs:write', ...USE],
  admin: [...READ, 'prefs:write', ...USE, ...ADMIN],
  owner: [...READ, 'prefs:write', ...USE, ...ADMIN, ...OWNER],
};
/** Staff (app_user.is_staff, a @marden.ai address and MFA) add these on top of their org role. */
export const STAFF_PERMISSIONS: readonly Permission[] = [...READ, ...STAFF];

/**
 * global: market data, same for every org. org: org-owned rows (watchlists, alerts, settings, saved
 * stress, team); handlers filter by the caller's org and answer 404 for another org's ids.
 * self: the caller's own identity. public: no session (signature-verified webhook, preflight, health).
 */
export type Scope = 'global' | 'org' | 'self' | 'public';

export interface RouteRule { permission: Permission; scope: Scope; plans: readonly Plan[] }

const ALL = PLANS;
const g = (permission: Permission, plans: readonly Plan[] = ALL): RouteRule => ({ permission, scope: 'global', plans });
const o = (permission: Permission, plans: readonly Plan[] = ALL): RouteRule => ({ permission, scope: 'org', plans });
const pub = (permission: Permission): RouteRule => ({ permission, scope: 'public', plans: [] });

const SITE_TABS = ['evidence', 'power', 'opposition', 'events', 'debt', 'lease'] as const;

/** Key = "METHOD /path" exactly as registered with Fastify. */
export const ROUTE_RULES: Readonly<Record<string, RouteRule>> = {
  'GET /api/home': g('home:read'),
  'GET /api/home/layout': o('prefs:read'),
  'PUT /api/home/layout': o('prefs:write'),
  'GET /api/completion': g('completion:read'),
  'GET /api/sites': g('sites:read'),
  'GET /api/sites/:slug': g('sites:read'),
  'GET /api/sites/:slug/schedule': g('sites:read'),
  ...Object.fromEntries(SITE_TABS.map((tab) => [`GET /api/sites/:slug/${tab}`, g('sites:read')])),
  'GET /api/sites/:slug/buildings': g('sites:read'),
  'GET /api/sites/:slug/project-file': g('sites:read'),
  'GET /api/instruments': g('instruments:read'),
  'GET /api/curve': g('instruments:read'),
  'GET /api/instruments/:id': g('instruments:read'),
  'GET /api/instruments/:id/price-history': g('instruments:read'),
  'GET /api/instruments/:id/holders': g('instruments:read'),
  'GET /api/instruments/:id/collateral': g('instruments:read'),
  'GET /api/payers': g('payers:read'),
  'GET /api/payers/:slug': g('payers:read'),
  'GET /api/payers/:slug/capacity': g('payers:read'),
  'POST /api/stress/run': g('stress:run'),
  'POST /api/stress/saved': o('stress:save'),
  'GET /api/stress/saved': o('stress:read'),
  'GET /api/stress/saved/:id': o('stress:read'),
  'GET /api/risk-list': g('risk:read'),
  'GET /api/watchlist': o('watchlist:read'),
  'POST /api/watchlist/items': o('watchlist:write'),
  'DELETE /api/watchlist/items/:id': o('watchlist:write'),
  'GET /api/alerts': o('alerts:read'),
  'POST /api/alerts/read': o('alerts:write'),
  'GET /api/alert-settings': o('prefs:read'),
  'PUT /api/alert-settings': o('prefs:write'),
  'GET /api/track-record': g('track_record:read'),
  'GET /api/sources/health': g('sources:read'),
  'GET /api/account': o('account:read'),
  'POST /api/account/invites': o('members:invite'),
  'PATCH /api/account/members/:id': o('members:manage'),
  'DELETE /api/account/members/:id': o('members:manage'),
  // Bulk export is a paid feature: trials can browse but not download tables.
  'GET /api/export/:table.xlsx': o('export:xlsx', ['design_partner', 'paid']),
  'GET /api/search': g('search:read'),
  'GET /api/data-centers': g('data_centers:read'),
  'GET /api/data-centers/summary': g('data_centers:read'),
  'GET /api/data-centers/:id': g('data_centers:read'),
  'GET /api/data-centers/:id/buildings': g('data_centers:read'),
  'GET /api/data-centers/:id/records': g('data_centers:read'),
  'GET /api/data-centers/:id/history': g('data_centers:read'),
  'GET /api/data-centers/:id/capacity': g('data_centers:read'),
  'GET /api/power-plants': g('power:read'),
  'GET /api/power-plants/summary': g('power:read'),
  'GET /api/power-plants/:id': g('power:read'),
  'GET /api/power-plants/:id/debt': g('power:read'),
  'GET /api/obligations': g('obligations:read'),
  'GET /api/obligations/:company': g('obligations:read'),
  'GET /api/gpus': g('gpus:read'),
  'GET /api/gpus/deployment': g('gpus:read'),
  'GET /api/gpus/:model': g('gpus:read'),
  'GET /api/leases': g('leases:read'),
  'GET /api/leases/:id': g('leases:read'),
  'GET /api/stream': o('stream:read'),
  'GET /api/me': { permission: 'me:read', scope: 'self', plans: ALL },
  'POST /api/webhooks/clerk': pub('webhook:clerk'),
  'GET /healthz': pub('public:health'),
  'OPTIONS *': pub('public:preflight'),
};

export const routeKey = (method: string, url: string): string => `${method.toUpperCase()} ${url}`;

export function ruleFor(method: string, url: string): RouteRule | undefined {
  return ROUTE_RULES[routeKey(method, url)];
}

/** Clerk organization role -> Marden role. Anything else maps to no access. */
export function roleFromClerk(clerkRole: string | null | undefined): Role | null {
  switch (clerkRole) {
    case 'org:owner': return 'owner';
    case 'org:admin': return 'admin';
    case 'org:member': return 'member';
    case 'org:viewer': return 'viewer';
    default: return null;
  }
}

export const clerkRoleFor = (role: Role): string => `org:${role}`;

export function permissionsFor(role: Role, staff: boolean): Permission[] {
  const set = new Set<Permission>(ROLE_PERMISSIONS[role]);
  if (staff) for (const p of STAFF_PERMISSIONS) set.add(p);
  return [...set].sort();
}

export type Decision = { ok: true } | { ok: false; reason: 'role' | 'plan' };

/** Role must hold the permission; the org's plan must be allowed (staff are not plan-limited). */
export function decide(rule: RouteRule, who: { role: Role; staff: boolean; plan: Plan }): Decision {
  if (!permissionsFor(who.role, who.staff).includes(rule.permission)) return { ok: false, reason: 'role' };
  if (!who.staff && !rule.plans.includes(who.plan)) return { ok: false, reason: 'plan' };
  return { ok: true };
}

/** Routes registered with Fastify that have no rule. The app refuses to start when this is non-empty. */
export function unmappedRoutes(registered: Iterable<string>): string[] {
  return [...registered].filter((key) => !(key in ROUTE_RULES));
}
