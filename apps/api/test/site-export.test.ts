// GET /api/export/site.xlsx?slug=&tab=[&type=]: the Site detail header's Excel and the Evidence tab's
// Records Excel export the table on screen (spec 04, spec 00 "Exports"), from the tab's own read model.
import ExcelJS from 'exceljs';
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { evidenceRows, SITE_EXPORT_TABS } from '../src/lib/site-export.js';
import { clearReadModels, makeApp, putReadModel, testDb } from './helpers.js';

const AS_OF = '2026-10-15';
const SLUG = 'xl-site';
const VIEWS = ['site_schedule', 'site_tab:evidence', 'site_tab:power', 'project_file'];

const rec = (date: string, type: string, text: string, tag: 'F' | 'R' | 'M' = 'F') => ({
  date, type, text, tag, sourceName: tag === 'F' ? 'NMED' : 'Press', sourceUrl: `https://example.org/${date}`,
});

const evidence = {
  records: [
    rec('2026-10-13', 'Docket', 'Letter to FERC: pipeline in service Feb 1, 2027'),
    rec('2026-09-24', 'Press', 'Force majeure notice', 'R'),
    rec('2026-09-21', 'Imagery', 'Sentinel-2 L2A, 21 Sep 2026', 'R'),
    rec('2026-07-14', 'Permit', 'Right-of-way reconsideration denied'),
  ],
  imagery: [{ month: '2026-09', url: 'https://example.org/img.png', caption: 'Sentinel-2', changeScore: 0.08 }],
};

const schedule = {
  projection: null,
  milestones: [
    {
      key: 'gas_or_grid', label: 'Gas supply / grid interconnection in service', status: 'Slipped',
      events: [{ date: '2026-08-17', kind: 'slipped' }], projected: { p10: '2027-01', p50: '2027-02', p90: '2027-06' },
      latestEvidence: rec('2026-10-13', 'Docket', 'Feb 1 2027 per letter to FERC'),
    },
    { key: 'rent_start', label: 'Rent start', status: 'Projected', events: [] },
  ],
  cushionHistory: [], imagery: [], documents: [],
};

describe('site tab export', () => {
  let db: Db;
  let app: FastifyInstance;

  beforeAll(async () => {
    db = testDb();
    app = await makeApp({ mode: 'dev', db });
    await clearReadModels(db, VIEWS);
    await db.query(`INSERT INTO site (slug, name) VALUES ($1, 'Export test site') ON CONFLICT DO NOTHING`, [SLUG]);
    await putReadModel(db, { view: 'site_tab:evidence', key: SLUG, asOf: AS_OF }, evidence);
    await putReadModel(db, { view: 'site_schedule', key: SLUG, asOf: AS_OF }, schedule);
  });
  afterAll(async () => {
    await clearReadModels(db, VIEWS);
    await db.query(`DELETE FROM site WHERE slug = $1`, [SLUG]);
    await app.close();
    await db.close();
  });

  async function sheet(url: string) {
    const res = await app.inject({ method: 'GET', url });
    expect(res.statusCode, res.body).toBe(200);
    expect(res.headers['content-disposition']).toContain(`marden-site-`);
    const wb = new ExcelJS.Workbook();
    await wb.xlsx.load(res.rawPayload as unknown as ArrayBuffer);
    const data = wb.getWorksheet('Data')!;
    const header = (data.getRow(1).values as unknown[]).slice(1) as string[];
    const rows = Array.from({ length: data.rowCount - 1 }, (_, i) => {
      const values = (data.getRow(i + 2).values as unknown[]).slice(1);
      return Object.fromEntries(header.map((h, j) => [h, values[j]]));
    });
    return { rows, sources: wb.getWorksheet('Sources')!.getSheetValues().slice(2) as unknown[][] };
  }

  it('exports the evidence records shown: images stay in the satellite card under All', async () => {
    const { rows, sources } = await sheet(`/api/export/site.xlsx?slug=${SLUG}&tab=evidence`);
    expect(rows.map((r) => r.type)).toEqual(['Docket', 'Press', 'Permit']);
    expect(rows[0]).toMatchObject({ date: '2026-10-13', text: 'Letter to FERC: pipeline in service Feb 1, 2027', tag: 'F', sourceName: 'NMED' });
    expect(sources.some((l) => l[4] === 'F' && l[5] === 'NMED' && l[6] === 'https://example.org/2026-10-13')).toBe(true);
  });

  it('exports one chip’s records with ?type=', async () => {
    expect((await sheet(`/api/export/site.xlsx?slug=${SLUG}&tab=evidence&type=permits`)).rows.map((r) => r.text)).toEqual([
      'Right-of-way reconsideration denied',
    ]);
    expect((await sheet(`/api/export/site.xlsx?slug=${SLUG}&tab=evidence&type=imagery`)).rows.map((r) => r.type)).toEqual(['Imagery']);
    const bad = await app.inject({ method: 'GET', url: `/api/export/site.xlsx?slug=${SLUG}&tab=evidence&type=everything` });
    expect(bad.statusCode).toBe(400);
  });

  it('exports the milestones on the Schedule tab (the default) with their latest evidence as a source', async () => {
    const { rows, sources } = await sheet(`/api/export/site.xlsx?slug=${SLUG}`);
    expect(rows.map((r) => [r.milestone, r.status, r.p50 || ''])).toEqual([
      ['Gas supply / grid interconnection in service', 'Slipped', '2027-02'],
      ['Rent start', 'Projected', ''], // no projection: empty cell, never a made-up date
    ]);
    expect(rows[0]?.latestEvidence).toBe('2026-10-13 · Feb 1 2027 per letter to FERC');
    expect(sources.some((l) => l[2] === 'latestEvidence' && l[4] === 'F')).toBe(true);
  });

  it('answers 400 for an unknown tab or no slug, 404 for an unknown site and 503 for a tab not built yet', async () => {
    const get = (qs: string) => app.inject({ method: 'GET', url: `/api/export/site.xlsx${qs}` });
    expect((await get(`?slug=${SLUG}&tab=nope`)).statusCode).toBe(400);
    expect((await get('')).statusCode).toBe(400);
    expect((await get('?slug=no-such-site&tab=power')).statusCode).toBe(404);
    const power = await get(`?slug=${SLUG}&tab=power`);
    expect(power.statusCode).toBe(503);
    expect(power.json()).toEqual({ error: 'not_ready', view: 'site_tab:power', key: SLUG });
  });

  it('covers every Site detail tab', () => {
    expect(Object.keys(SITE_EXPORT_TABS).sort()).toEqual(
      ['buildings', 'construction', 'debt', 'events', 'evidence', 'lease', 'opposition', 'parties', 'power', 'schedule', 'structure'],
    );
    expect(evidenceRows({ records: evidence.records, imagery: [] }, undefined)).toHaveLength(4); // no image card: every record
  });
});
