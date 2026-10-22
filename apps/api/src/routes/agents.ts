// Staff routes for the agent system (docs/agents.md):
//   POST /api/documents                      upload a PDF / DOCX / XLSX / HTML / TXT for the agents (multipart:
//                                            file + optional site, instrument, entity, filed, sourceUrl, title, docDate)
//   GET  /api/documents, /api/documents/:id  processing status: queued, routed, extracted, verified, published,
//                                            needs_review, failed, ... with candidates and token use
//   GET  /api/uploads/:id                    an upload until the worker stores it as a document
//   GET  /api/agents/status                  backend in use (or not configured), jobs, tokens today
//   GET  /api/review, /api/review/:id        the review queue
//   POST /api/review/:id/approve | reject | edit
// Uploads are staged in Postgres; the worker stores them raw (R2) and queues them. Decisions are recorded
// here and applied by the worker through the fact writer (so read models and models are marked dirty).
import { createHash } from 'node:crypto';
import type { FastifyInstance, FastifyRequest } from 'fastify';
import { z } from 'zod';
import { authOf } from '../auth/plugin.js';
import type { Db } from '../db.js';
import { badRequest, conflict, HttpError, notFound } from '../errors.js';
import { num, queryOf } from '../lib/params.js';
import { CONTENT_TYPES, sniffUpload } from '../lib/upload-kind.js';
import {
  agentStatus, decision, documentStatus, insertDecision, insertUpload, jobCounts, listDocuments, listReview,
  reviewItem, subjectMissing, upload,
} from '../queries/agents.js';
import { audit } from '../queries/audit.js';
import { param, parseBody } from './serve.js';

export const DEFAULT_UPLOAD_MAX_BYTES = 50 * 1024 * 1024;
const DECISION_WAIT_MS = 3000;
const SLUG = z.string().trim().min(1).max(200);
const DOC_STATUSES = ['queued', 'running', 'routed', 'extracted', 'verified', 'linked', 'published', 'needs_review',
  'failed', 'skipped', 'budget', 'not_configured'] as const;

const fieldsSchema = z.strictObject({
  site: SLUG.optional(), instrument: SLUG.optional(), entity: SLUG.optional(),
  filed: z.enum(['true', 'false']).optional(),
  sourceUrl: z.url({ protocol: /^https?$/ }).max(2000).optional(),
  title: z.string().trim().min(1).max(300).optional(),
  docDate: z.iso.date().optional(),
});
const rejectSchema = z.strictObject({ reason: z.string().trim().min(3).max(2000) });
const approveSchema = z.strictObject({ note: z.string().trim().max(2000).optional() });
const editSchema = z.strictObject({
  value: z.union([z.string().min(1).max(4000), z.number(), z.boolean(), z.array(z.string()), z.record(z.string(), z.unknown())]),
  quote: z.string().trim().min(3).max(2000),
  unit: z.string().trim().max(20).optional(),
  asOf: z.iso.date().optional(),
  quoteLoc: z.string().trim().max(200).optional(),
  subjectType: z.enum(['site', 'campus', 'building', 'plant', 'unit', 'instrument', 'entity', 'obligation',
    'queue_project']).optional(),
  subjectId: SLUG.optional(),
  field: z.string().regex(/^[a-z][a-z0-9_]*$/).max(80).optional(),
});

export function uploadMaxBytes(env: NodeJS.ProcessEnv = process.env): number {
  const raw = Number(env.MARDEN_UPLOAD_MAX_BYTES ?? '');
  return Number.isInteger(raw) && raw > 0 ? raw : DEFAULT_UPLOAD_MAX_BYTES;
}

interface ParsedUpload { filename: string; data: Buffer; fields: Record<string, string> }

async function readMultipart(req: FastifyRequest, maxBytes: number): Promise<ParsedUpload> {
  if (!req.isMultipart()) throw badRequest('multipart_form_required');
  const fields: Record<string, string> = {};
  let file: { filename: string; data: Buffer } | null = null;
  for await (const part of req.parts({ limits: { fileSize: maxBytes, files: 1, fields: 12, fieldSize: 4096 } })) {
    if (part.type === 'file') {
      if (part.fieldname !== 'file') throw badRequest('unexpected_file_field');
      const data = await part.toBuffer();
      if (part.file.truncated) throw new HttpError(413, 'too_large', { maxBytes });
      file = { filename: (part.filename || 'upload').slice(0, 200), data };
    } else {
      fields[part.fieldname] = String(part.value);
    }
  }
  if (!file) throw badRequest('file_required');
  return { ...file, fields };
}

async function waitApplied(db: Db, id: string): Promise<Record<string, unknown> | null> {
  const until = Date.now() + DECISION_WAIT_MS;
  for (;;) {
    const d = await decision(db, id);
    if (d?.appliedAt || Date.now() >= until) return d;
    await new Promise((r) => setTimeout(r, 250));
  }
}

function uploadRoutes(app: FastifyInstance, db: Db): void {
  const maxBytes = uploadMaxBytes();
  app.post('/api/documents', { bodyLimit: maxBytes + 1024 * 1024 }, async (req, reply) => {
    const auth = authOf(req);
    let parsed: ParsedUpload;
    try {
      parsed = await readMultipart(req, maxBytes);
    } catch (err) {
      if ((err as { code?: string }).code === 'FST_REQ_FILE_TOO_LARGE') throw new HttpError(413, 'too_large', { maxBytes });
      throw err;
    }
    const fields = parseBody(fieldsSchema, parsed.fields);
    const kind = sniffUpload(parsed.data);
    if (!kind) throw new HttpError(415, 'unsupported_file', { accepted: Object.keys(CONTENT_TYPES) });
    const links = Object.fromEntries((['site', 'instrument', 'entity'] as const)
      .filter((k) => fields[k]).map((k) => [k, fields[k] as string]));
    const missing = await subjectMissing(db, links);
    if (missing) throw notFound(missing);
    const sha256 = createHash('sha256').update(parsed.data).digest('hex');
    const row = await db.tx(async (c) => {
      const out = await insertUpload(c, {
        filename: parsed.filename, contentType: CONTENT_TYPES[kind], kind, size: parsed.data.length, sha256,
        data: parsed.data, uploadedBy: auth.userId, orgId: auth.orgId, links, filed: fields.filed === 'true',
        sourceUrl: fields.sourceUrl ?? null, title: fields.title ?? null, docDate: fields.docDate ?? null,
      });
      await audit(c, {
        orgId: auth.orgId, userId: auth.userId, action: 'document.upload', target: `agent_upload:${out.id}`, ip: req.ip,
        meta: { filename: parsed.filename, kind, size: parsed.data.length, sha256, links },
      });
      return out;
    });
    const agents = await agentStatus(db);
    return reply.code(202).send({
      uploadId: row.id, status: 'pending', kind, size: parsed.data.length, sha256, links, createdAt: row.createdAt,
      // Configured only while the worker reports: a stale status (worker down) is not a promise that it runs.
      agents: {
        configured: agents.backend !== 'not_configured' && !agents.stale, backend: agents.backend, detail: agents.detail,
        stale: agents.stale, checkedAt: agents.checkedAt,
      },
    });
  });
}

function statusRoutes(app: FastifyInstance, db: Db): void {
  app.get('/api/uploads/:id', async (req) => {
    const id = param(req, 'id');
    if (!/^\d+$/.test(id)) throw notFound('upload');
    const row = await upload(db, id);
    if (!row) throw notFound('upload');
    return row;
  });

  app.get('/api/documents', async (req) => {
    const q = queryOf(req);
    const status = typeof q.status === 'string' ? q.status : null;
    if (status && !(DOC_STATUSES as readonly string[]).includes(status)) throw badRequest('unknown_status');
    const limit = Math.min(Math.max(Math.trunc(num(q, 'limit') ?? 50), 1), 200);
    const offset = Math.max(Math.trunc(num(q, 'offset') ?? 0), 0);
    return { ...(await listDocuments(db, { status, limit, offset })), agents: await agentStatus(db) };
  });

  app.get('/api/documents/:id', async (req) => {
    const id = param(req, 'id');
    if (!/^\d+$/.test(id)) throw notFound('document');
    const doc = await documentStatus(db, id);
    if (!doc) throw notFound('document');
    return doc;
  });

  app.get('/api/agents/status', async () => ({ ...(await agentStatus(db)), ...(await jobCounts(db)) }));
}

function reviewRoutes(app: FastifyInstance, db: Db): void {
  app.get('/api/review', async (req) => {
    const q = queryOf(req);
    const status = typeof q.status === 'string' ? q.status : 'open';
    if (!['open', 'approved', 'rejected'].includes(status)) throw badRequest('unknown_status');
    const kind = typeof q.kind === 'string' ? q.kind : null;
    const area = typeof q.area === 'string' ? q.area : null;
    const limit = Math.min(Math.max(Math.trunc(num(q, 'limit') ?? 50), 1), 200);
    const offset = Math.max(Math.trunc(num(q, 'offset') ?? 0), 0);
    return listReview(db, { status, kind, area, limit, offset });
  });

  app.get('/api/review/:id', async (req) => {
    const id = param(req, 'id');
    const item = /^\d+$/.test(id) ? await reviewItem(db, id) : null;
    if (!item) throw notFound('review_item');
    return item;
  });

  for (const action of ['approve', 'reject', 'edit'] as const) {
    app.post(`/api/review/:id/${action}`, async (req, reply) => {
      const auth = authOf(req);
      const id = param(req, 'id');
      const item = /^\d+$/.test(id) ? await reviewItem(db, id) : null;
      if (!item) throw notFound('review_item');
      if (item.status !== 'open') throw conflict(`already_${item.status as string}`);
      let reason: string | null = null;
      let edit: Record<string, unknown> | null = null;
      if (action === 'reject') reason = parseBody(rejectSchema, req.body ?? {}).reason;
      else if (action === 'approve') reason = parseBody(approveSchema, req.body ?? {}).note ?? null;
      else {
        const e = parseBody(editSchema, req.body ?? {});
        edit = { value: e.value, quote: e.quote, unit: e.unit, as_of: e.asOf, quote_loc: e.quoteLoc,
          subject_type: e.subjectType, subject_id: e.subjectId, field: e.field };
        reason = 'edited';
      }
      const decisionId = await db.tx(async (c) => {
        const did = await insertDecision(c, { reviewItemId: id, action, decidedBy: auth.userId, reason, edit });
        if (did) {
          await audit(c, { orgId: auth.orgId, userId: auth.userId, action: `review.${action}.requested`,
            target: `review_item:${id}`, ip: req.ip, meta: { decisionId: did, reason } });
        }
        return did;
      });
      if (!decisionId) throw conflict('decision_pending');
      const d = await waitApplied(db, decisionId);
      if (d?.appliedAt) return reply.code(d.error ? 422 : 200).send(d);
      return reply.code(202).send({ ...d, status: 'pending' });
    });
  }
}

export function agentRoutes(app: FastifyInstance, db: Db): void {
  uploadRoutes(app, db);
  statusRoutes(app, db);
  reviewRoutes(app, db);
}
