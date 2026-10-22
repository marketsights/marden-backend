// The agent system's tables (db/migrations/20261022001000_agents.sql): uploads staged for the worker,
// document processing status, the staff review queue and the decisions the worker applies.
// The API never publishes a fact itself: the Python worker applies each decision through the fact
// writer, which marks read models and models dirty (docs/agents.md).
import type { Queryable } from '../db.js';

export interface UploadInput {
  filename: string; contentType: string; kind: string; size: number; sha256: string; data: Buffer;
  uploadedBy: string; orgId: string; links: Record<string, string>; filed: boolean;
  sourceUrl: string | null; title: string | null; docDate: string | null;
}

export async function insertUpload(c: Queryable, u: UploadInput): Promise<{ id: string; createdAt: string }> {
  const res = await c.query<{ id: string; created_at: string }>(
    `INSERT INTO agent_upload (filename, content_type, kind, size, sha256, data, uploaded_by, org_id, links, filed,
                               source_url, title, doc_date)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) RETURNING id::text, created_at`,
    [u.filename, u.contentType, u.kind, u.size, u.sha256, u.data, u.uploadedBy, u.orgId, JSON.stringify(u.links),
      u.filed, u.sourceUrl, u.title, u.docDate],
  );
  const row = res.rows[0]!;
  return { id: row.id, createdAt: row.created_at };
}

const SUBJECT_TABLES: Readonly<Record<string, [string, string]>> = {
  site: ['site', 'slug'], instrument: ['instrument', 'id'], entity: ['entity', 'id'],
};

export async function subjectMissing(c: Queryable, links: Record<string, string>): Promise<string | null> {
  for (const [kind, id] of Object.entries(links)) {
    const [table, column] = SUBJECT_TABLES[kind]!;
    const res = await c.query(`SELECT 1 FROM ${table} WHERE ${column} = $1`, [id]);
    if ((res.rowCount ?? 0) === 0) return kind;
  }
  return null;
}

export interface AgentStatus {
  backend: string; detail: string | null; models: Record<string, string>; checkedAt: string | null;
  /** The worker writes agent_status on every sweep (30 s); older than STATUS_STALE_MS means it is not running. */
  stale: boolean;
}

export const STATUS_STALE_MS = 10 * 60 * 1000;

export async function agentStatus(c: Queryable, now: number = Date.now()): Promise<AgentStatus> {
  const res = await c.query<{ backend: string; detail: string | null; models: Record<string, string>; checked_at: string }>(
    `SELECT backend, detail, models, checked_at FROM agent_status LIMIT 1`);
  const row = res.rows[0];
  if (!row) {
    return { backend: 'not_configured', detail: 'the agent worker has not reported yet', models: {}, checkedAt: null, stale: true };
  }
  const stale = now - new Date(row.checked_at).getTime() > STATUS_STALE_MS;
  return { backend: row.backend, detail: row.detail, models: row.models, checkedAt: row.checked_at, stale };
}

export async function upload(c: Queryable, id: string) {
  const res = await c.query(
    `SELECT id::text, filename, kind, size, sha256, status, document_id::text AS "documentId", error, links, filed,
            created_at AS "createdAt", stored_at AS "storedAt"
     FROM agent_upload WHERE id = $1`, [id]);
  return res.rows[0] ?? null;
}

/** One status word per document: queued, routed, extracted, verified, linked, published, needs_review, failed,
 * skipped, budget (paused by the token budget) or not_configured. */
export const STATUS_SQL = `CASE
  WHEN j.id IS NULL AND d.extract_state = 'queued' AND s.backend = 'not_configured' THEN 'not_configured'
  WHEN j.id IS NULL THEN 'queued'
  WHEN j.state IN ('queued', 'dispatched') THEN 'queued'
  WHEN j.state = 'running' THEN CASE j.stage WHEN 'queued' THEN 'running' ELSE j.stage END
  ELSE j.state END`;

const DOC_SELECT = `
  SELECT d.id::text, d.source_id AS "sourceId", d.doc_type AS "docType", d.title, d.url, d.doc_date AS "docDate",
         d.fetched_at AS "fetchedAt", d.extract_state AS "extractState", ${STATUS_SQL} AS status,
         j.id::text AS "jobId", j.priority, j.priority_reason AS "priorityReason", j.areas, j.stats, j.error,
         j.attempts, j.updated_at AS "updatedAt"
  FROM document d
  LEFT JOIN LATERAL (SELECT * FROM agent_job WHERE document_id = d.id ORDER BY id DESC LIMIT 1) j ON true
  LEFT JOIN agent_status s ON true`;

export async function listDocuments(c: Queryable, opts: { status: string | null; limit: number; offset: number }) {
  const res = await c.query(
    `SELECT * FROM (${DOC_SELECT} WHERE d.extract_state <> 'none' OR j.id IS NOT NULL) x
     WHERE ($1::text IS NULL OR status = $1) ORDER BY "updatedAt" DESC NULLS LAST, id DESC LIMIT $2 OFFSET $3`,
    [opts.status, opts.limit, opts.offset]);
  const counts = await c.query<{ status: string; n: string }>(
    `SELECT status, count(*)::text AS n FROM (${DOC_SELECT} WHERE d.extract_state <> 'none' OR j.id IS NOT NULL) x
     GROUP BY 1 ORDER BY 1`);
  return { documents: res.rows, counts: Object.fromEntries(counts.rows.map((r) => [r.status, Number(r.n)])) };
}

export async function documentStatus(c: Queryable, id: string) {
  const doc = (await c.query(`${DOC_SELECT} WHERE d.id = $1`, [id])).rows[0];
  if (!doc) return null;
  const job = doc.jobId as string | null;
  const candidates = job ? (await c.query(
    `SELECT id::text, area, field, subject_mention AS "subjectMention", subject_type AS "subjectType",
            subject_id AS "subjectId", value, unit, quote, page, verdict, verdict_reason AS "verdictReason",
            material, material_codes AS "materialCodes", outcome, outcome_reason AS "outcomeReason",
            fact_id::text AS "factId"
     FROM agent_candidate WHERE job_id = $1 ORDER BY id`, [job])).rows : [];
  const usage = job ? (await c.query(
    `SELECT agent, model, backend, count(*)::int AS calls, sum(input_tokens)::int AS "inputTokens",
            sum(output_tokens)::int AS "outputTokens", sum(cache_read_tokens)::int AS "cacheReadTokens",
            sum(cache_write_tokens)::int AS "cacheWriteTokens", sum(cost_usd)::float AS "costUsd"
     FROM agent_call WHERE job_id = $1 GROUP BY 1, 2, 3 ORDER BY 1`, [job])).rows : [];
  return { ...doc, candidates, usage };
}

export async function jobCounts(c: Queryable) {
  const jobs = await c.query<{ state: string; n: string }>(`SELECT state, count(*)::text AS n FROM agent_job GROUP BY 1`);
  const tokens = await c.query<{ n: string; cost: number | null }>(
    `SELECT coalesce(sum(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens), 0)::text AS n,
            sum(cost_usd)::float AS cost
     FROM agent_call WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'`);
  const pending = await c.query<{ uploads: string; decisions: string; reviews: string }>(
    `SELECT (SELECT count(*) FROM agent_upload WHERE status = 'pending')::text AS uploads,
            (SELECT count(*) FROM agent_decision WHERE applied_at IS NULL)::text AS decisions,
            (SELECT count(*) FROM review_item WHERE status = 'open')::text AS reviews`);
  const p = pending.rows[0]!;
  return {
    jobs: Object.fromEntries(jobs.rows.map((r) => [r.state, Number(r.n)])),
    tokensToday: Number(tokens.rows[0]?.n ?? 0), costTodayUsd: tokens.rows[0]?.cost ?? 0,
    pendingUploads: Number(p.uploads), pendingDecisions: Number(p.decisions), openReviews: Number(p.reviews),
  };
}

const REVIEW_SELECT = `
  SELECT r.id::text, r.kind, r.ref_type AS "refType", r.ref_id AS "refId", r.title, r.reason, r.payload, r.status,
         r.created_at AS "createdAt", r.decided_by AS "decidedBy", r.decided_at AS "decidedAt",
         f.subject_type AS "subjectType", f.subject_id AS "subjectId", f.field, f.value, f.unit, f.tag,
         f.quote, f.quote_loc AS "quoteLoc", f.source_name AS "sourceName", f.source_url AS "sourceUrl",
         f.valid_from AS "asOf", f.verifier, f.material, f.producer, f.status AS "factStatus",
         coalesce(f.document_id, (r.payload->>'document_id')::bigint)::text AS "documentId",
         dec.id::text AS "pendingDecisionId"
  FROM review_item r
  LEFT JOIN fact f ON r.kind = 'fact' AND r.ref_type = 'fact' AND f.id::text = r.ref_id
  LEFT JOIN agent_decision dec ON dec.review_item_id = r.id AND dec.applied_at IS NULL`;

/** A review item about a fact from a source the unlicensed-price switch hides is left out (lib/licensing.ts). */
const LICENSED = (hiddenPh: string): string => `NOT EXISTS (SELECT 1 FROM fact h WHERE r.kind = 'fact'
  AND r.ref_type = 'fact' AND h.id::text = r.ref_id AND h.source_name = ANY(${hiddenPh}::text[]))`;

/** hidden: FastifyInstance.hiddenSources. */
export interface ReviewFilter {
  status: string; kind: string | null; area: string | null; limit: number; offset: number; hidden: readonly string[];
}

export async function listReview(c: Queryable, f: ReviewFilter) {
  const res = await c.query(
    `${REVIEW_SELECT}
     WHERE r.status = $1 AND ($2::text IS NULL OR r.kind = $2) AND ($3::text IS NULL OR r.payload->>'area' = $3)
       AND ${LICENSED('$6')}
     ORDER BY (r.payload->'material_codes' IS NOT NULL AND r.payload->'material_codes' <> '[]'::jsonb) DESC,
              r.created_at DESC, r.id DESC
     LIMIT $4 OFFSET $5`,
    [f.status, f.kind, f.area, f.limit, f.offset, f.hidden]);
  const total = await c.query<{ n: string }>(
    `SELECT count(*)::text AS n FROM review_item r WHERE r.status = $1 AND ($2::text IS NULL OR r.kind = $2)
       AND ($3::text IS NULL OR r.payload->>'area' = $3) AND ${LICENSED('$4')}`, [f.status, f.kind, f.area, f.hidden]);
  return { items: res.rows, total: Number(total.rows[0]?.n ?? 0) };
}

export async function reviewItem(c: Queryable, id: string, hidden: readonly string[]) {
  const res = await c.query(`${REVIEW_SELECT} WHERE r.id = $1 AND ${LICENSED('$2')}`, [id, hidden]);
  return res.rows[0] ?? null;
}

export async function insertDecision(c: Queryable, d: {
  reviewItemId: string; action: 'approve' | 'reject' | 'edit'; decidedBy: string; reason: string | null;
  edit: Record<string, unknown> | null;
}): Promise<string | null> {
  const res = await c.query<{ id: string }>(
    `INSERT INTO agent_decision (review_item_id, action, decided_by, reason, edit) VALUES ($1, $2, $3, $4, $5)
     ON CONFLICT (review_item_id) WHERE applied_at IS NULL DO NOTHING RETURNING id::text`,
    [d.reviewItemId, d.action, d.decidedBy, d.reason, d.edit === null ? null : JSON.stringify(d.edit)]);
  return res.rows[0]?.id ?? null;
}

export async function decision(c: Queryable, id: string) {
  const res = await c.query(
    `SELECT id::text, review_item_id::text AS "reviewItemId", action, decided_by AS "decidedBy", reason, edit,
            created_at AS "createdAt", applied_at AS "appliedAt", result, error
     FROM agent_decision WHERE id = $1`, [id]);
  return res.rows[0] ?? null;
}
