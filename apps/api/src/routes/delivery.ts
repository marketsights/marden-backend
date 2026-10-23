// Delivery record (who keeps having construction delays), Mispriced (what the market has not priced yet) and the
// Pipeline (projects before and around the start of construction). Read models delivery, mispriced and pipeline
// (docs/methods/delivery-record-v0.1.md, mispriced-v0.1.md; services/marden/readmodels/views/pipeline.py).
import type { FastifyInstance } from 'fastify';
import type { DeliveryPage, MispricedPage } from '../contracts/delivery.js';
import type { Db } from '../db.js';
import { asOfOf } from '../lib/params.js';
import { serveDoc } from './serve.js';

export function deliveryRoutes(app: FastifyInstance, db: Db): void {
  app.get('/api/delivery', async (req) => serveDoc<DeliveryPage>(db, 'delivery', 'all', asOfOf(req)));
  app.get('/api/mispriced', async (req) => serveDoc<MispricedPage>(db, 'mispriced', 'all', asOfOf(req)));
  app.get('/api/pipeline', async (req) => serveDoc<unknown>(db, 'pipeline', 'all', asOfOf(req)));
}
