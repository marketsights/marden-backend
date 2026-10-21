// GET /api/export/:table.xlsx?<same filters as the table> -> data sheet + sources sheet.
import type { FastifyInstance } from 'fastify';
import { authOf } from '../auth/plugin.js';
import type { Db } from '../db.js';
import { loaderFor } from '../lib/export-tables.js';
import { asOfOf, queryOf } from '../lib/params.js';
import { buildWorkbook } from '../lib/xlsx.js';
import { audit } from '../queries/audit.js';
import { param } from './serve.js';

export const XLSX_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';

export function exportRoutes(app: FastifyInstance, db: Db): void {
  app.get('/api/export/:table.xlsx', async (req, reply) => {
    const auth = authOf(req);
    const table = param(req, 'table');
    const load = loaderFor(table);
    const data = await load({ db, q: queryOf(req), orgId: auth.orgId, asOf: asOfOf(req) });
    const file = await buildWorkbook({ table, view: data.view, asOf: data.asOf }, data.rows);
    await audit(db, {
      orgId: auth.orgId, userId: auth.userId, action: 'export.xlsx', target: table, ip: req.ip,
      meta: { rows: data.rows.length, asOf: data.asOf, query: queryOf(req) },
    });
    const day = data.asOf ?? new Date().toISOString().slice(0, 10);
    return reply
      .type(XLSX_TYPE)
      .header('content-disposition', `attachment; filename="marden-${table}-${day}.xlsx"`)
      .send(file);
  });
}
