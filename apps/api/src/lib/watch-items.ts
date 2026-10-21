// Maps watchlist rows + their read models to WatchItem. Values come from the read models as built;
// anything the read model does not carry yet is null (n/d), never filled in.
import type { ChainStatus, Sourced, WatchItem } from '../contracts/types.js';
import type { WatchJoinRow } from '../queries/watchlist.js';

const BOND_KINDS = new Set(['bond', 'convert', 'abs', 'cmbs', 'muni']);

type Doc = Record<string, unknown> | null;

const field = <T>(doc: Doc, name: string): T | null => (doc && doc[name] !== undefined ? (doc[name] as T) : null);
const sourcedNumber = (doc: Doc, name: string): number | null => field<Sourced<number>>(doc, name)?.value ?? null;

/** WatchItem's nextCatalyst is typed string; until a read model carries it the value is null (n/d). */
export type WatchItemOut = Omit<WatchItem, 'status' | 'nextCatalyst'> & {
  status: ChainStatus | null;
  nextCatalyst: string | null;
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
  };
}

function instrumentItem(row: WatchJoinRow): WatchItemOut {
  const doc = row.inst_doc;
  const size = sourcedNumber(doc, 'sizeB');
  return {
    id: row.id,
    kind: 'instrument',
    refId: row.ref_id,
    name: field<string>(doc, 'name') ?? row.identity_name ?? row.ref_id,
    typeLabel: BOND_KINDS.has((row.instrument_kind ?? '').toLowerCase()) ? 'Bond' : 'Loan',
    sub: size === null ? 'n/d' : `$${size}B`,
    status: field<ChainStatus>(doc, 'chainStatus'),
    cushionMonths: sourcedNumber(row.inst_site_doc, 'cushionMonths'),
    price: field<Sourced<number>>(doc, 'mardenPrice'),
    change1w: field<Sourced<number>>(doc, 'change1w'),
    markGap: field<WatchItem['markGap']>(doc, 'markGap'),
    nextCatalyst: field<string>(doc, 'nextCatalyst'),
  };
}

export const toWatchItem = (row: WatchJoinRow): WatchItemOut => (row.kind === 'site' ? siteItem(row) : instrumentItem(row));
