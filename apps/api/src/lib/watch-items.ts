// Maps watchlist rows + their read models to WatchItem. Values come from the read models as built;
// anything the read model does not carry yet is null (n/d), never filled in.
import type { ChainStatus, Sourced, WatchItem } from '../contracts/types.js';
import type { ComingUpEntry, IssuerCandidateRow, WatchJoinRow } from '../queries/watchlist.js';
import { businessToday } from './business-day.js';

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
 * site, or the instrument's size) and `nextCatalystAt` (ISO date of the next catalyst: the
 * soonest step in the `home` read model's Coming up list or the mark_gap model's next fund
 * marks). `status` and `nextCatalyst` are null (n/d) until a read model has them.
 */
export type WatchItemOut = Omit<WatchItem, 'status' | 'nextCatalyst'> & {
  status: ChainStatus | null;
  nextCatalyst: string | null;
  siteSlug: string | null;
  cushion: Sourced<number> | null;
  debtB: Sourced<number> | null;
  nextCatalystAt: string | null;
};

/** The day a catalyst must fall on or after: the as-of day asked, else today in US Eastern. */
const fromDay = (asOf: string | null): string => asOf ?? businessToday();

/** "CoreWeave 9.00% Sr Notes 2031: next N-PORT marks public" on that name's own row reads "Next N-PORT marks public". */
function withoutName(what: string, name: string): string {
  const prefix = `${name}: `;
  if (!what.startsWith(prefix) || what.length === prefix.length) return what;
  const rest = what.slice(prefix.length);
  return rest.charAt(0).toUpperCase() + rest.slice(1);
}

/**
 * The name's next dated catalyst: the soonest of its scheduled steps (`home` read model Coming up,
 * which also carries the next public fund marks) and, for an instrument, the mark_gap model's next
 * fund marks date. Undated text from the record's own read model is the fallback. Nothing else is
 * filled in: no dated step and no text = null (n/d).
 */
function nextCatalyst(
  row: WatchJoinRow,
  name: string,
  asOf: string | null,
  own: { dated: ComingUpEntry | null; undated: string | null },
): { nextCatalyst: string | null; nextCatalystAt: string | null } {
  const day = fromDay(asOf);
  const dated = [...(row.coming ?? []), ...(own.dated ? [own.dated] : [])].filter(
    (c) => typeof c.date === 'string' && typeof c.what === 'string' && c.what !== '' && c.date.slice(0, 10) >= day,
  );
  // Soonest first; on the same day the shortest line (the read model writes one step under a short
  // headline and a long title, and the short one is what its Coming up list shows).
  const before = (a: ComingUpEntry, b: ComingUpEntry): boolean => {
    const [da, db] = [a.date.slice(0, 10), b.date.slice(0, 10)];
    return da < db || (da === db && a.what.length < b.what.length);
  };
  const next = dated.reduce<ComingUpEntry | null>((best, c) => (best === null || before(c, best) ? c : best), null);
  if (next) return { nextCatalyst: withoutName(next.what, name), nextCatalystAt: next.date.slice(0, 10) };
  return { nextCatalyst: own.undated, nextCatalystAt: null };
}

function siteItem(row: WatchJoinRow, asOf: string | null): WatchItemOut {
  const doc = row.site_doc;
  const endUser = field<string>(doc, 'endUser');
  const tenant = field<string>(doc, 'tenant');
  const name = field<string>(doc, 'name') ?? row.identity_name ?? row.ref_id;
  return {
    id: row.id,
    kind: 'site',
    refId: row.ref_id,
    name,
    typeLabel: 'Site',
    sub: endUser ? [endUser, tenant].filter(Boolean).join(' → ') : 'n/d',
    status: field<ChainStatus>(doc, 'status'),
    cushionMonths: sourcedNumber(doc, 'cushionMonths'),
    price: null,
    change1w: null,
    markGap: null,
    siteSlug: row.ref_id,
    cushion: sourced<number>(doc, 'cushionMonths'),
    debtB: sourced<number>(doc, 'debtB'),
    ...nextCatalyst(row, name, asOf, { dated: null, undated: field<string>(doc, 'nextCatalyst') }),
  };
}

function instrumentItem(row: WatchJoinRow, asOf: string | null): WatchItemOut {
  const doc = row.inst_doc;
  const size = sourcedNumber(doc, 'sizeB');
  // The mark_gap model dates the next public fund marks (N-PORT / BDC filings) for the instrument.
  const nextMarks = field<string>(doc, 'nextMarksPublic');
  const name = field<string>(doc, 'name') ?? row.identity_name ?? row.ref_id;
  const marks = nextMarks ? { date: nextMarks, what: 'Fund marks due', watch: [] } : null;
  return {
    id: row.id,
    kind: 'instrument',
    refId: row.ref_id,
    name,
    typeLabel: typeLabelOf(row.instrument_kind),
    sub: size === null ? 'n/d' : `$${size}B`,
    status: field<ChainStatus>(doc, 'chainStatus'),
    cushionMonths: sourcedNumber(row.inst_site_doc, 'cushionMonths'),
    price: sourced<number>(doc, 'mardenPrice'),
    change1w: sourced<number>(doc, 'change1w'),
    markGap: field<WatchItem['markGap']>(doc, 'markGap'),
    siteSlug: row.inst_site_slug,
    cushion: sourced<number>(row.inst_site_doc, 'cushionMonths'),
    debtB: sourced<number>(doc, 'sizeB'),
    ...nextCatalyst(row, name, asOf, { dated: marks, undated: field<string>(doc, 'nextCatalyst') }),
  };
}

/** One watchlist row as served; `asOf` (the day asked, else today) drops catalysts already past. */
export const toWatchItem = (row: WatchJoinRow, asOf: string | null = null): WatchItemOut =>
  row.kind === 'site' ? siteItem(row, asOf) : instrumentItem(row, asOf);

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
