// GET /api/export/site.xlsx?slug=&tab=[&type=]: the table on the Site detail tab the user is looking at
// (spec 04 header "Excel", spec 00 "Exports": exactly the rows shown, with a sources sheet). Each tab
// reads the same read model as its own endpoint (GET /api/sites/:slug/<tab>, /schedule, /project-file).
import type { Db } from '../db.js';
import { badRequest, notFound, notReady } from '../errors.js';
import { subjectExists } from '../queries/rows.js';
import { first, type Query } from './params.js';

type Json = Record<string, unknown>;
type Rows = Json[];

const list = (v: unknown): Json[] => (Array.isArray(v) ? (v as Json[]) : []);
const obj = (v: unknown): Json => (typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Json) : {});

/** Evidence tab chips (web app components/sites/detail/evidence-groups.ts): record type words per chip. */
export const EVIDENCE_GROUPS: Readonly<Record<string, readonly string[]>> = {
  permits: ['permit', 'zoning', 'air permit', 'building permit'],
  dockets: ['docket', 'filing', 'utility', 'court'],
  press: ['press', 'rating'],
  county: ['county', 'county vote'],
  imagery: ['imagery'],
};

const groupOf = (type: unknown): string | undefined => {
  const word = String(type ?? '').trim().toLowerCase();
  return Object.keys(EVIDENCE_GROUPS).find((g) => EVIDENCE_GROUPS[g].includes(word));
};

/** The records table as the Evidence tab shows it: one chip's records; under All, the monthly images
 * sit in the satellite card above the table, so Imagery records are left out when images exist. */
export function evidenceRows(doc: Json, type: string | undefined): Rows {
  const records = list(doc.records);
  if (type !== undefined) {
    if (!(type in EVIDENCE_GROUPS)) throw badRequest(`type must be one of ${Object.keys(EVIDENCE_GROUPS).join(', ')}`);
    return records.filter((r) => groupOf(r.type) === type);
  }
  return list(doc.imagery).length > 0 ? records.filter((r) => groupOf(r.type) !== 'imagery') : records;
}

interface TabExport {
  view: string;
  rows: (doc: Json, q: Query) => Rows;
}

const milestoneRows = (doc: Json): Rows =>
  list(doc.milestones).map((m) => {
    const p = obj(m.projected);
    return {
      key: m.key, milestone: m.label, status: m.status, p10: p.p10 ?? null, p50: p.p50 ?? null, p90: p.p90 ?? null,
      events: list(m.events).map((e) => `${String(e.kind)} ${String(e.date)}`),
      latestEvidence: m.latestEvidence ?? null,
    };
  });

/** Site detail tab -> the read model it shows and the rows of its table. */
export const SITE_EXPORT_TABS: Readonly<Record<string, TabExport>> = {
  schedule: { view: 'site_schedule', rows: milestoneRows },
  evidence: { view: 'site_tab:evidence', rows: (doc, q) => evidenceRows(doc, first(q, 'type')) },
  power: { view: 'site_tab:power', rows: (doc) => list(doc.history) },
  buildings: {
    view: 'site_tab:buildings',
    rows: (doc) => list(doc.buildings).map(({ footprint: _footprint, ...rest }) => rest),
  },
  opposition: { view: 'site_tab:opposition', rows: (doc) => list(doc.items) },
  events: { view: 'site_tab:events', rows: (doc) => list(doc.events) },
  debt: { view: 'site_tab:debt', rows: (doc) => list(doc.instruments) },
  lease: { view: 'site_tab:lease', rows: (doc) => list(doc.terms) },
  parties: { view: 'project_file', rows: (doc) => list(doc.parties) },
  structure: { view: 'project_file', rows: (doc) => list(obj(doc.structure).tranches) },
  construction: {
    view: 'project_file',
    rows: (doc) => {
      const c = obj(doc.construction);
      return [
        ...list(c.liens).map((l) => ({ table: "Mechanic's liens", ...l })),
        ...list(c.equipment).map((e) => ({ table: 'Equipment', ...e })),
      ];
    },
  },
};

export interface SiteExportCtx { db: Db; q: Query; asOf: string | null }

export async function siteExport(ctx: SiteExportCtx): Promise<{ view: string; asOf: string | null; rows: Rows }> {
  const slug = first(ctx.q, 'slug');
  if (!slug) throw badRequest('slug is required');
  const tab = first(ctx.q, 'tab') ?? 'schedule';
  const spec = Object.hasOwn(SITE_EXPORT_TABS, tab) ? SITE_EXPORT_TABS[tab] : undefined;
  if (!spec) throw badRequest(`tab must be one of ${Object.keys(SITE_EXPORT_TABS).join(', ')}`);
  const doc = await ctx.db.readModel<Json>(spec.view, slug, ctx.asOf);
  if (doc === null) {
    if (!(await subjectExists(ctx.db, 'site', 'slug', slug))) throw notFound('site');
    throw notReady(spec.view, slug);
  }
  return { view: spec.view, asOf: ctx.asOf, rows: spec.rows(doc, ctx.q) };
}
