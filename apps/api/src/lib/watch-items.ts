// Maps watchlist rows + their read models to WatchItem. Values come from the read models as built;
// anything the read model does not carry yet is null (n/d), never filled in.
import type { ChainStatus, Sourced, WatchItem } from '../contracts/types.js';
import type { IssuerCandidateRow, WatchJoinRow } from '../queries/watchlist.js';

const BOND_KINDS = new Set(['bond', 'convert', 'abs', 'cmbs', 'muni']);

type Doc = Record<string, unknown> | null;

const field = <T>(doc: Doc, name: string): T | null => (doc && doc[name] !== undefined ? (doc[name] as T) : null);
const sourced = <T>(doc: Doc, name: string): Sourced<T> | null => field<Sourced<T>>(doc, name);
const sourcedNumber = (doc: Doc, name: string): number | null => sourced<number>(doc, name)?.value ?? null;

const typeLabelOf = (instrumentKind: string | null): WatchItem['typeLabel'] =>
  BOND_KINDS.has((instrumentKind ?? '').toLowerCase()) ? 'Bond' : 'Loan';

/**
 * WatchItem plus what the screen needs that the contract lacks (additive; spec 11 Layout 2–3):
 * `siteSlug` (the site whose status and cushion the row shows, so stream events find the row),
 * `cushion` (the cushion model output with its tag and method version), `debtB` (debt on the
 * site, or the instrument's size) and `nextCatalystAt` (ISO date of the next catalyst, when a
 * model output dates it). `status` and `nextCatalyst` are null (n/d) until a read model has them.
 */
export type WatchItemOut = Omit<WatchItem, 'status' | 'nextCatalyst'> & {
  status: ChainStatus | null;
  nextCatalyst: string | null;
  siteSlug: string | null;
  cushion: Sourced<number> | null;
  debtB: Sourced<number> | null;
  nextCatalystAt: string | null;
};

function siteItem(row: WatchJoinRow): WatchItemOut {
  const doc = row.site_doc;
  const endUser = field<string>(doc, 'endUser');
  const tenant = field<string>(doc, 'tenant');
  return {
    id: row.id,
    kind: 'site',
    refId: row.ref_id,
    name: field<string>(doc, 'name') ?? row.identity_name ?? row.ref_id,
    typeLabel: 'Site',
    sub: endUser ? [endUser, tenant].filter(Boolean).join(' → ') : 'n/d',
    status: field<ChainStatus>(doc, 'status'),
    cushionMonths: sourcedNumber(doc, 'cushionMonths'),
    price: null,
    change1w: null,
    markGap: null,
    nextCatalyst: field<string>(doc, 'nextCatalyst'),
    siteSlug: row.ref_id,
    cushion: sourced<number>(doc, 'cushionMonths'),
    debtB: sourced<number>(doc, 'debtB'),
    nextCatalystAt: null,
  };
}

function instrumentItem(row: WatchJoinRow): WatchItemOut {
  const doc = row.inst_doc;
  const size = sourcedNumber(doc, 'sizeB');
  // The mark_gap model dates the next public fund marks (N-PORT / BDC filings) for the instrument.
  const nextMarks = field<string>(doc, 'nextMarksPublic');
  return {
    id: row.id,
    kind: 'instrument',
    refId: row.ref_id,
    name: field<string>(doc, 'name') ?? row.identity_name ?? row.ref_id,
    typeLabel: typeLabelOf(row.instrument_kind),
    sub: size === null ? 'n/d' : `$${size}B`,
    status: field<ChainStatus>(doc, 'chainStatus'),
    cushionMonths: sourcedNumber(row.inst_site_doc, 'cushionMonths'),
    price: sourced<number>(doc, 'mardenPrice'),
    change1w: sourced<number>(doc, 'change1w'),
    markGap: field<WatchItem['markGap']>(doc, 'markGap'),
    nextCatalyst: field<string>(doc, 'nextCatalyst') ?? (nextMarks ? 'Fund marks due' : null),
    siteSlug: row.inst_site_slug,
    cushion: sourced<number>(row.inst_site_doc, 'cushionMonths'),
    debtB: sourced<number>(doc, 'sizeB'),
    nextCatalystAt: nextMarks,
  };
}

export const toWatchItem = (row: WatchJoinRow): WatchItemOut => (row.kind === 'site' ? siteItem(row) : instrumentItem(row));

/** One name that adding an issuer would add; `watchId` set when the org already watches it. */
export interface IssuerCandidate {
  kind: 'site' | 'instrument';
  refId: string;
  name: string;
  typeLabel: WatchItem['typeLabel'];
  /** CUSIP for instruments that have one, else null. */
  cusip: string | null;
  watchId: string | null;
}

export const toIssuerCandidate = (row: IssuerCandidateRow): IssuerCandidate => ({
  kind: row.kind,
  refId: row.ref_id,
  name: row.name,
  typeLabel: row.kind === 'site' ? 'Site' : typeLabelOf(row.instrument_kind),
  cusip: row.cusip,
  watchId: row.watch_id,
});
