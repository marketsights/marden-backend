// Tables that GET /api/export/:table.xlsx can export: the same read model and filters as the
// screen's own endpoint, without paging.
import type {
  CompletionResponse, InstrumentDetail, InstrumentRow, LeaseMapPage, ObligationGraph, PayersPage, RiskList, SiteSummary,
} from '../contracts/types.js';
import type { Db } from '../db.js';
import { badRequest, notFound, notReady } from '../errors.js';
import { queryRows } from '../queries/rows.js';
import { watchedSiteSlugs, watchRows } from '../queries/watchlist.js';
import { filterCompletion, filterInstruments, filterObligations, filterSites, wantsWatchlist } from './doc-filters.js';
import { first, isoDate, type Query } from './params.js';
import type { RowViewDef } from './row-query.js';
import { DC_ROWS, PLANT_ROWS } from './row-views.js';
import { toWatchItem } from './watch-items.js';

export interface ExportCtx { db: Db; q: Query; orgId: string; asOf: string | null }
export interface ExportData { view: string; asOf: string | null; rows: Record<string, unknown>[] }
type Loader = (ctx: ExportCtx) => Promise<ExportData>;

const MAX_EXPORT_ROWS = 100_000;
const asRows = (rows: unknown[]): Record<string, unknown>[] => rows as Record<string, unknown>[];

async function doc<T>(ctx: ExportCtx, view: string, key: string): Promise<T> {
  const found = await ctx.db.readModel<T>(view, key, ctx.asOf);
  if (found === null) throw notReady(view, key);
  return found;
}

const rowView = (def: RowViewDef): Loader => async (ctx) => {
  const res = await queryRows<unknown>(ctx.db, def, ctx, { limit: MAX_EXPORT_ROWS, offset: 0 });
  if (res.available === 0) throw notReady(def.view, '*');
  return { view: def.view, asOf: res.asOf, rows: asRows(res.rows) };
};

export const EXPORT_TABLES: Readonly<Record<string, Loader>> = {
  'data-centers': rowView(DC_ROWS),
  'power-plants': rowView(PLANT_ROWS),
  completion: async (ctx) => {
    const d = await doc<CompletionResponse>(ctx, 'completion', 'all');
    const watched = wantsWatchlist(ctx.q) ? await watchedSiteSlugs(ctx.db, ctx.orgId) : null;
    return { view: 'completion', asOf: d.asOf, rows: asRows(filterCompletion(d.rows, ctx.q, watched)) };
  },
  sites: async (ctx) => ({ view: 'sites', asOf: ctx.asOf, rows: asRows(filterSites(await doc<SiteSummary[]>(ctx, 'sites', 'all'), ctx.q)) }),
  instruments: async (ctx) => ({
    view: 'instruments', asOf: ctx.asOf, rows: asRows(filterInstruments(await doc<InstrumentRow[]>(ctx, 'instruments', 'all'), ctx.q)),
  }),
  holders: async (ctx) => {
    const id = first(ctx.q, 'instrument');
    if (!id) throw badRequest('instrument is required for the holders export');
    return { view: 'instrument_detail', asOf: ctx.asOf, rows: asRows((await doc<InstrumentDetail>(ctx, 'instrument_detail', id)).holders.rows) };
  },
  'risk-list': async (ctx) => {
    const week = isoDate(ctx.q, 'week') ?? (await ctx.db.latestKey('risk_list', ctx.asOf));
    if (!week) throw notReady('risk_list', 'latest');
    return { view: 'risk_list', asOf: week, rows: asRows((await doc<RiskList>(ctx, 'risk_list', week)).rows) };
  },
  payers: async (ctx) => {
    const d = await doc<PayersPage>(ctx, 'payers', 'all');
    return { view: 'payers', asOf: d.asOf, rows: asRows(d.rows) };
  },
  leases: async (ctx) => {
    const d = await doc<LeaseMapPage>(ctx, 'leases', 'all');
    return { view: 'leases', asOf: d.asOf, rows: asRows(d.leases) };
  },
  obligations: async (ctx) => {
    const d = filterObligations(await doc<ObligationGraph>(ctx, 'obligations', 'all'), ctx.q);
    return { view: 'obligations', asOf: d.asOf, rows: asRows(d.edges) };
  },
  watchlist: async (ctx) => {
    const rows = await ctx.db.withOrg(ctx.orgId, (c) => watchRows(c, ctx.orgId, ctx.asOf));
    return { view: 'watchlist', asOf: ctx.asOf, rows: asRows(rows.map(toWatchItem)) };
  },
};

export function loaderFor(table: string): Loader {
  const loader = Object.hasOwn(EXPORT_TABLES, table) ? EXPORT_TABLES[table] : undefined;
  if (!loader) throw notFound('export_table');
  return loader;
}
