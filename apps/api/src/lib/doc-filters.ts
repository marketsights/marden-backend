// Server-side filters applied to single read-model documents (spec 03, 05, 06, 07, 14, 18).
// Filtering only: rows are kept or dropped, values are never recomputed here.
import type {
  ChainLink, CompletionRow, DcRecord, InstrumentRow, LinkRole, ObligationEdge, ObligationGraph, SiteSummary,
} from '../contracts/types.js';
import { badRequest } from '../errors.js';
import { first, flag, list, num, type Query } from './params.js';

const lowerSet = (values: string[]): Set<string> => new Set(values.map((v) => v.toLowerCase()));
const matches = (wanted: Set<string>, value: string | null | undefined): boolean =>
  wanted.size === 0 || (value != null && wanted.has(value.toLowerCase()));
const party = (chain: ChainLink[], role: LinkRole): string | undefined => chain.find((l) => l.role === role)?.party;

export const CUSHION_BANDS = ['<0', '0-3', '3-6', '6-12', '12+', 'none'] as const;

export function cushionBand(months: number | null | undefined): (typeof CUSHION_BANDS)[number] {
  if (months == null) return 'none';
  if (months < 0) return '<0';
  if (months < 3) return '0-3';
  if (months < 6) return '3-6';
  if (months < 12) return '6-12';
  return '12+';
}

/** Completion monitor: endUser, tenant, holder (lead lender or lender link), state, driver, cushion band, watchlist. */
export function filterCompletion(rows: CompletionRow[], q: Query, watched: Set<string> | null): CompletionRow[] {
  const endUser = lowerSet(list(q, 'endUser'));
  const tenant = lowerSet(list(q, 'tenant'));
  const state = lowerSet(list(q, 'state'));
  const driver = lowerSet(list(q, 'driver'));
  const bands = new Set(list(q, 'cushion'));
  for (const b of bands) {
    if (!(CUSHION_BANDS as readonly string[]).includes(b)) throw badRequest(`cushion must be one of ${CUSHION_BANDS.join(', ')}`);
  }
  const holder = first(q, 'holder')?.toLowerCase();
  return rows.filter((row) => {
    if (!matches(endUser, party(row.chain, 'end_user')) || !matches(tenant, party(row.chain, 'tenant'))) return false;
    if (!matches(state, row.state) || !matches(driver, row.driver)) return false;
    if (bands.size > 0 && !bands.has(cushionBand(row.projection?.cushionMonths?.value))) return false;
    if (holder) {
      const lenders = `${row.leadLenders} ${party(row.chain, 'lenders') ?? ''}`.toLowerCase();
      if (!lenders.includes(holder)) return false;
    }
    return watched === null || watched.has(row.slug);
  });
}

/** Sites: status (chain status), endUser, state. */
export function filterSites(rows: SiteSummary[], q: Query): SiteSummary[] {
  const status = lowerSet(list(q, 'status'));
  const endUser = lowerSet(list(q, 'endUser'));
  const state = lowerSet(list(q, 'state'));
  return rows.filter((r) => matches(status, r.status) && matches(endUser, r.endUser) && matches(state, r.state));
}

/** Instruments: type chip. */
export function filterInstruments(rows: InstrumentRow[], q: Query): InstrumentRow[] {
  const type = lowerSet(list(q, 'type'));
  return rows.filter((r) => matches(type, r.type));
}

/** Campus records tab: type chips (Permit, Zoning, Filing, ...). */
export function filterRecords(rows: DcRecord[], q: Query): DcRecord[] {
  const type = lowerSet(list(q, 'type'));
  return rows.filter((r) => matches(type, r.type));
}

const RANGE_MONTHS: Record<string, number | null> = { '3m': 3, '6m': 6, '1y': 12, all: null };

/** Instrument price history: range 3m | 6m | 1y | all, counted back from the last point. */
export function priceRange(points: { date: string; price: number }[], q: Query): { range: string; points: typeof points } {
  const range = (first(q, 'range') ?? 'all').toLowerCase();
  if (!(range in RANGE_MONTHS)) throw badRequest(`range must be one of ${Object.keys(RANGE_MONTHS).join(', ')}`);
  const months = RANGE_MONTHS[range] ?? null;
  const last = points.at(-1)?.date;
  if (months === null || !last) return { range, points };
  const from = new Date(`${last.slice(0, 10)}T00:00:00Z`);
  from.setUTCMonth(from.getUTCMonth() - months);
  const cutoff = from.toISOString().slice(0, 10);
  return { range, points: points.filter((p) => p.date.slice(0, 10) >= cutoff) };
}

/** Obligation map: type, status, minB (edge total, $B), company (either end). Nodes follow the kept edges. */
export function filterObligations(graph: ObligationGraph, q: Query): ObligationGraph {
  const type = lowerSet(list(q, 'type'));
  const status = lowerSet(list(q, 'status'));
  const company = lowerSet(list(q, 'company'));
  const minB = num(q, 'minB');
  const filtered = type.size + status.size + company.size > 0 || minB !== undefined;
  if (!filtered) return graph;
  const keep = (e: ObligationEdge): boolean =>
    matches(type, e.type) && matches(status, e.status) &&
    (company.size === 0 || company.has(e.from.toLowerCase()) || company.has(e.to.toLowerCase())) &&
    (minB === undefined || (e.totalB.value !== null && e.totalB.value >= minB));
  const edges = graph.edges.filter(keep);
  const ids = new Set(edges.flatMap((e) => [e.from, e.to]));
  return { ...graph, edges, nodes: graph.nodes.filter((n) => ids.has(n.id)) };
}

export const wantsWatchlist = (q: Query): boolean => flag(q, 'watchlist');
