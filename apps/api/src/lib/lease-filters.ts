// Lease map (spec 25): URL filters on the lease-map document and the rows its Excel export carries.
// The page reads the document whole and applies the same filters in the browser, so the export holds
// exactly the rows on screen. Filtering and projection only: values are copied as served.
import type { LeaseRecord } from '../contracts/types.js';
import { badRequest } from '../errors.js';
import { first, flag, list, type Query } from './params.js';

/** Query keys the lease map and its export accept (front end: src/lib/endpoints/filters.ts LEASE_FILTERS). */
export const LEASE_FILTER_KEYS = ['lessee', 'kind', 'status', 'attribution', 'band', 'trigger', 'q'] as const;

/** Downgrade bands the lessee can carry (spec 24); 'n/d' = the capacity model has no band for it yet. */
export const LEASE_BANDS = ['High', 'Watch', 'Low', 'n/d'] as const;

const lowerSet = (values: string[]): Set<string> => new Set(values.map((v) => v.toLowerCase()));
const matches = (wanted: Set<string>, value: string | null | undefined): boolean =>
  wanted.size === 0 || (value != null && wanted.has(value.toLowerCase()));

/**
 * Status filter key: the status up to its first comma ("Signed, not commenced" → "Signed"), since
 * filter values are comma lists (front end: components/leases/lease-model.ts statusKey).
 */
export const statusKey = (status: string): string => status.split(',')[0]?.trim() ?? status;

/** The band a lease shows: its lessee's model band, or 'n/d' while the model has none. */
export const leaseBand = (lease: LeaseRecord): string => lease.lesseeBand?.value ?? 'n/d';

/** Words the free-text filter looks in: lessee, landlord, site and the lease id. */
const haystack = (lease: LeaseRecord): string =>
  [lease.lessee, lease.lessor?.value, lease.siteSlug, lease.campusId, lease.id].filter(Boolean).join(' ').toLowerCase();

/**
 * lessee, kind, status (its key), attribution and band match one of a comma list (any case); trigger=1 keeps
 * trigger leases only; q matches text in the lessee, landlord, site or id.
 */
export function filterLeases(leases: readonly LeaseRecord[], q: Query): LeaseRecord[] {
  const lessee = lowerSet(list(q, 'lessee'));
  const kind = lowerSet(list(q, 'kind'));
  const status = lowerSet(list(q, 'status'));
  const attribution = lowerSet(list(q, 'attribution'));
  const band = lowerSet(list(q, 'band'));
  for (const b of band) {
    if (!LEASE_BANDS.some((known) => known.toLowerCase() === b)) throw badRequest(`band must be one of ${LEASE_BANDS.join(', ')}`);
  }
  const trigger = flag(q, 'trigger');
  const text = first(q, 'q')?.toLowerCase();
  return leases.filter(
    (l) =>
      matches(lessee, l.lessee) &&
      matches(kind, l.kind) &&
      matches(status, statusKey(l.status)) &&
      matches(attribution, l.attribution) &&
      matches(band, leaseBand(l)) &&
      (!trigger || l.triggerLease) &&
      (!text || haystack(l).includes(text)),
  );
}

/** Dependent debt with a disclosed amount, summed ($B); null when no amount is disclosed. */
export function debtExposedB(lease: LeaseRecord): number | null {
  const known = lease.dependentDebt.map((d) => d.amountB?.value).filter((v): v is number => typeof v === 'number');
  return known.length === 0 ? null : Math.round(known.reduce((a, b) => a + b, 0) * 1e6) / 1e6;
}

/** One export row per lease: the ranked table's columns, Sourced values kept for the sources sheet. */
export function leaseExportRow(lease: LeaseRecord): Record<string, unknown> {
  return {
    id: lease.id,
    lessee: lease.lessee,
    lessor: lease.lessor,
    siteSlug: lease.siteSlug,
    campusId: lease.campusId,
    kind: lease.kind,
    status: lease.status,
    attribution: lease.attribution,
    confidence: lease.confidence,
    totalB: lease.totalB,
    annualB: lease.annualB,
    commencement: lease.commencement,
    termYears: lease.termYears,
    turnsAdded: lease.turnsAdded,
    headroomBeforeTurns: lease.headroomBeforeTurns,
    triggerLease: lease.triggerLease,
    lesseeBand: lease.lesseeBand,
    priorityScore: lease.priorityScore,
    debtExposedB: debtExposedB(lease),
    dependentDebt: lease.dependentDebt.map((d) => d.name).join(', '),
    holders: lease.dependentDebt.reduce((n, d) => n + (d.holdersCount ?? 0), 0),
    evidence: lease.evidence,
  };
}
