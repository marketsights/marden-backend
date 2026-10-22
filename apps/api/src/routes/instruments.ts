// Instruments and curve (spec 06), Instrument detail (spec 07), GPU collateral (spec 19),
// Payer (spec 08) and payer capacity (spec 24).
import type { FastifyInstance } from 'fastify';
import type { GpuCollateral, InstrumentDetail, InstrumentRow, PayersPage } from '../contracts/types.js';
import type { Db } from '../db.js';
import { notFound } from '../errors.js';
import { filterInstruments, priceRange } from '../lib/doc-filters.js';
import { asOfOf, queryOf } from '../lib/params.js';
import { filterPayers } from '../lib/payer-filters.js';
import { ENTITY, INSTRUMENT, keyed, param, serveDoc } from './serve.js';

export function instrumentRoutes(app: FastifyInstance, db: Db): void {
  app.get('/api/instruments', async (req) => {
    const rows = await serveDoc<InstrumentRow[]>(db, 'instruments', 'all', asOfOf(req));
    return filterInstruments(rows, queryOf(req));
  });

  app.get('/api/curve', async (req) => serveDoc(db, 'curve', 'all', asOfOf(req)));

  // hasCollateral: whether GET /api/instruments/:id/collateral has a document for the same day, so
  // Instrument detail asks for GPU collateral only when there is some (no 404 for every other loan).
  app.get('/api/instruments/:id', async (req) => {
    const id = param(req, 'id');
    const asOf = asOfOf(req);
    const doc = await serveDoc<InstrumentDetail>(db, 'instrument_detail', keyed(INSTRUMENT, id), asOf);
    const hasCollateral = (await db.readModel('gpu_collateral', id, asOf)) !== null;
    return { ...doc, hasCollateral };
  });

  // Price series and holders are read from the same instrument_detail document as the page,
  // so the chart, KPI strip and holders table agree (spec 07 acceptance).
  app.get('/api/instruments/:id/price-history', async (req) => {
    const id = param(req, 'id');
    const doc = await serveDoc<InstrumentDetail>(db, 'instrument_detail', keyed(INSTRUMENT, id), asOfOf(req));
    const { range, points } = priceRange(doc.priceHistory, queryOf(req));
    return { instrumentId: id, range, points, fundMarks: doc.fundMarks };
  });

  app.get('/api/instruments/:id/holders', async (req) => {
    const doc = await serveDoc<InstrumentDetail>(db, 'instrument_detail', keyed(INSTRUMENT, param(req, 'id')), asOfOf(req));
    return doc.holders;
  });

  // Only GPU-backed facilities have a collateral document. The builder runs for every instrument
  // alongside instrument_detail, so once the instrument's detail is built a missing collateral doc
  // means it has no GPU collateral: 404, not the 503 of a document still waiting to be built.
  app.get('/api/instruments/:id/collateral', async (req) => {
    const id = param(req, 'id');
    const asOf = asOfOf(req);
    const doc = await db.readModel<GpuCollateral>('gpu_collateral', id, asOf);
    if (doc !== null) return doc;
    if ((await db.readModel('instrument_detail', id, asOf)) !== null) throw notFound('gpu_collateral');
    return serveDoc<GpuCollateral>(db, 'gpu_collateral', keyed(INSTRUMENT, id), asOf);
  });

  // ?kind=&risk= (spec 24 index filters); the Excel export applies the same filters.
  app.get('/api/payers', async (req) => {
    const doc = await serveDoc<PayersPage>(db, 'payers', 'all', asOfOf(req));
    return { ...doc, rows: filterPayers(doc.rows, queryOf(req)) };
  });

  app.get('/api/payers/:slug', async (req) => serveDoc(db, 'payer', keyed(ENTITY, param(req, 'slug')), asOfOf(req)));

  app.get('/api/payers/:slug/capacity', async (req) =>
    serveDoc(db, 'payer_capacity', keyed(ENTITY, param(req, 'slug')), asOfOf(req)));
}
