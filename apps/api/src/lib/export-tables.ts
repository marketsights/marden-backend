// Tables that GET /api/export/:table.xlsx can export: the same read model and filters as the
// screen's own endpoint, without paging.
import type {
  CompletionResponse, InstrumentDetail, InstrumentRow, LeaseMapPage, ObligationGraph, Payer, PayerCapacity, PayersPage, RiskList,
  SiteSummary,
} from '../contracts/types.js';
import type { Db } from '../db.js';
import { badRequest, notFound, notReady } from '../errors.js';
import { queryRows } from '../queries/rows.js';
import { runStress, stressModelAvailable, stressRequestFromQuery, stressResultRows } from '../queries/stress.js';
import { watchedSiteSlugs, watchRows } from '../queries/watchlist.js';
import { filterCompletion, filterInstruments, filterObligations, filterSites, wantsWatchlist } from './doc-filters.js';
import { completionExportRow, sitesExportRow } from './list-export.js';
import { first, isoDate, type Query } from './params.js';
import { filterPayers, payerIndexRow, payerSiteRows } from './payer-filters.js';
import type { RowViewDef } from './row-query.js';
import { DC_ROWS, PLANT_ROWS } from './row-views.js';
import { siteExport } from './site-export.js';
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
    return { view: 'completion', asOf: d.asOf, rows: filterCompletion(d.rows, ctx.q, watched).map(completionExportRow) };
  },
  sites: async (ctx) => ({
    view: 'sites', asOf: ctx.asOf, rows: filterSites(await doc<SiteSummary[]>(ctx, 'sites', 'all'), ctx.q).map(sitesExportRow),
  }),
  // One Site detail tab's table (?slug=&tab=[&type=], spec 04): the header Excel and the Evidence Records Excel.
  site: (ctx) => siteExport(ctx),
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
  // The index's columns for the filtered rows (?kind=&risk=), as on screen (spec 24).
  payers: async (ctx) => {
    const d = await doc<PayersPage>(ctx, 'payers', 'all');
    return { view: 'payers', asOf: d.asOf, rows: filterPayers(d.rows, ctx.q).map(payerIndexRow) };
  },
  // One payer's sites table (?slug=, spec 08), sourced per site from the sites document when built.
  payer: async (ctx) => {
    const slug = first(ctx.q, 'slug');
    if (!slug) throw badRequest('slug is required for the payer export');
    const payer = await doc<Payer>(ctx, 'payer', slug);
    const sites = await ctx.db.readModel<SiteSummary[]>('sites', 'all', ctx.asOf);
    return { view: 'payer', asOf: ctx.asOf, rows: payerSiteRows(payer, sites) };
  },
  // The instruments that depend on one payer (?slug=, spec 24 "Capacity to pay").
  'payer-exposed': async (ctx) => {
    const slug = first(ctx.q, 'slug');
    if (!slug) throw badRequest('slug is required for the payer-exposed export');
    const cap = await doc<PayerCapacity>(ctx, 'payer_capacity', slug);
    return { view: 'payer_capacity', asOf: ctx.asOf, rows: asRows(cap.exposed) };
  },
  leases: async (ctx) => {
    const d = await doc<LeaseMapPage>(ctx, 'leases', 'all');
    return { view: 'leases', asOf: d.asOf, rows: asRows(d.leases) };
  },
  obligations: async (ctx) => {
    const d = filterObligations(await doc<ObligationGraph>(ctx, 'obligations', 'all'), ctx.q);
    return { view: 'obligations', asOf: d.asOf, rows: asRows(d.edges) };
  },
  // ?scenario=&severity=&<param>= as on /stress: the run's debt-hit rows (StressResult.rows, or
  // ContagionResult.instruments), straight from the stress model; 503 until the model is deployed.
  stress: async (ctx) => {
    const request = stressRequestFromQuery(ctx.q);
    if (!(await stressModelAvailable(ctx.db))) throw notReady('model:stress', request.scenario);
    const rows = stressResultRows(await runStress(ctx.db, request));
    if (rows === null) throw notReady('model:stress', request.scenario);
    return { view: 'model:stress', asOf: null, rows };
  },
  watchlist: async (ctx) => {
    // ?kind=site|instrument: the Watchlist tab on screen (All / Sites / Instruments).
    const kind = first(ctx.q, 'kind');
    if (kind !== undefined && kind !== 'site' && kind !== 'instrument') throw badRequest('kind must be site or instrument');
    const rows = await ctx.db.withOrg(ctx.orgId, (c) => watchRows(c, ctx.orgId, ctx.asOf));
    const items = rows.map(toWatchItem).filter((item) => kind === undefined || item.kind === kind);
    return { view: 'watchlist', asOf: ctx.asOf, rows: asRows(items) };
  },
};

export function loaderFor(table: string): Loader {
  const loader = Object.hasOwn(EXPORT_TABLES, table) ? EXPORT_TABLES[table] : undefined;
  if (!loader) throw notFound('export_table');
  return loader;
}
