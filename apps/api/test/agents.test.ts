// The agent system's staff routes (docs/agents.md): uploads (multipart, sniffed by bytes, size-limited,
// linked subjects must exist), document status, the review queue and decisions (recorded here, applied by
// the Python worker), all staff-only and audit-logged.
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { sniffUpload } from '../src/lib/upload-kind.js';
import { bearer, dropOrgs, makeApp, makeKeys, seedOrg, signToken, testDb, uid, type Keys } from './helpers.js';

const BOUNDARY = 'marden-test-boundary';

function multipart(fields: Record<string, string>, file?: { name: string; data: Buffer }): Buffer {
  const parts: Buffer[] = [];
  for (const [k, v] of Object.entries(fields)) {
    parts.push(Buffer.from(`--${BOUNDARY}\r\nContent-Disposition: form-data; name="${k}"\r\n\r\n${v}\r\n`));
  }
  if (file) {
    parts.push(Buffer.from(`--${BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="${file.name}"\r\n`
      + 'Content-Type: application/octet-stream\r\n\r\n'), file.data, Buffer.from('\r\n'));
  }
  parts.push(Buffer.from(`--${BOUNDARY}--\r\n`));
  return Buffer.concat(parts);
}

// A minimal, valid one-page PDF (test input only; nothing here is served as data).
const PDF = Buffer.from('%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Count 0/Kids[]>>endobj\n'
  + 'trailer<</Root 1 0 R>>\n%%EOF\n');

describe('agent routes', () => {
  let db: Db;
  let app: FastifyInstance;
  let small: FastifyInstance;
  let keys: Keys;
  const org = uid('org_agents');
  const u = { staff: uid('user_staff'), member: uid('user_member') };
  const site = uid('site');
  const auth = async (who: string) => bearer(await signToken(keys, { userId: who, orgId: org, mfa: true }));
  const upload = async (to: FastifyInstance, who: string, fields: Record<string, string>, file?: { name: string; data: Buffer }) =>
    to.inject({
      method: 'POST', url: '/api/documents', payload: multipart(fields, file),
      headers: { ...(await auth(who)), 'content-type': `multipart/form-data; boundary=${BOUNDARY}` },
    });

  beforeAll(async () => {
    db = testDb();
    keys = await makeKeys();
    app = await makeApp({ mode: 'clerk', db, keys });
    process.env.MARDEN_UPLOAD_MAX_BYTES = '200';
    small = await makeApp({ mode: 'clerk', db, keys });
    delete process.env.MARDEN_UPLOAD_MAX_BYTES;
    await seedOrg(db, { id: org, members: [
      { id: u.staff, role: 'member', staff: true, email: `${u.staff}@marden.ai` }, { id: u.member, role: 'owner' }] });
    await db.query(`INSERT INTO site (slug, name) VALUES ($1, 'Agents test site')`, [site]);
    await db.query(`INSERT INTO source (id, name, kind, terms_note) VALUES ('upload', 'Staff upload', 'manual', 'test')
                    ON CONFLICT (id) DO NOTHING`);
  });

  afterAll(async () => {
    await app.close();
    await small.close();
    await db.query(`DELETE FROM agent_decision`);
    await db.query(`DELETE FROM review_item WHERE ref_type IN ('fact', 'agent_candidate') AND title LIKE 'agents-test%'`);
    await db.query(`DELETE FROM agent_upload WHERE uploaded_by = ANY($1)`, [Object.values(u)]);
    await db.query(`DELETE FROM agent_job`);
    await db.query(`DELETE FROM fact WHERE producer = 'agent:debt' AND subject_id = $1`, [site]);
    await db.query(`DELETE FROM document WHERE source_id = 'upload'`);
    await db.query(`DELETE FROM site WHERE slug = $1`, [site]);
    await dropOrgs(db, [org], Object.values(u));
    await db.close();
  });

  it('sniffs uploads by their bytes', () => {
    expect(sniffUpload(PDF)).toBe('pdf');
    expect(sniffUpload(Buffer.from('<!DOCTYPE html><html><body>x</body></html>'))).toBe('html');
    expect(sniffUpload(Buffer.from('Plain text minutes'))).toBe('txt');
    expect(sniffUpload(Buffer.from([0x00, 0xff, 0xfe, 0x01]))).toBeNull();
    expect(sniffUpload(Buffer.concat([Buffer.from([0x50, 0x4b, 0x03, 0x04]), Buffer.from('word/document.xml')]))).toBe('docx');
  });

  it('lets only staff upload documents and read the queue', async () => {
    const denied = await upload(app, u.member, {}, { name: 'a.pdf', data: PDF });
    expect(denied.statusCode).toBe(403);
    for (const url of ['/api/review', '/api/documents', '/api/agents/status']) {
      expect((await app.inject({ method: 'GET', url, headers: await auth(u.member) })).statusCode).toBe(403);
    }
  });

  it('stages an upload with its links and says honestly that the agents are not configured', async () => {
    const res = await upload(app, u.staff, { site, filed: 'true', title: 'County agenda' }, { name: 'agenda.pdf', data: PDF });
    expect(res.statusCode).toBe(202);
    const body = res.json();
    expect(body).toMatchObject({ status: 'pending', kind: 'pdf', size: PDF.length, links: { site } });
    expect(body.agents).toMatchObject({ configured: false, backend: 'not_configured' });
    const row = (await db.query(`SELECT sha256, filed, links, data IS NOT NULL AS staged FROM agent_upload WHERE id = $1`,
      [body.uploadId])).rows[0];
    expect(row).toMatchObject({ sha256: body.sha256, filed: true, links: { site }, staged: true });
    const audit = await db.query(`SELECT action FROM audit_log WHERE target = $1`, [`agent_upload:${body.uploadId}`]);
    expect(audit.rows.map((r) => r.action)).toEqual(['document.upload']);
    const status = await app.inject({ method: 'GET', url: `/api/uploads/${body.uploadId}`, headers: await auth(u.staff) });
    expect(status.json()).toMatchObject({ status: 'pending', documentId: null });
  });

  it('refuses unknown subjects, unsupported files, missing files and files over the limit', async () => {
    expect((await upload(app, u.staff, { site: 'no-such-site' }, { name: 'a.pdf', data: PDF })).statusCode).toBe(404);
    const bin = await upload(app, u.staff, {}, { name: 'a.bin', data: Buffer.from([0x00, 0x01, 0x02, 0xff]) });
    expect(bin.statusCode).toBe(415);
    expect((await upload(app, u.staff, { title: 'x' })).json()).toMatchObject({ reason: 'file_required' });
    const big = await upload(small, u.staff, {}, { name: 'big.txt', data: Buffer.alloc(500, 0x61) });
    expect(big.statusCode).toBe(413);
  });

  it('reports document status from the agent job', async () => {
    const doc = (await db.query<{ id: string }>(
      `INSERT INTO document (source_id, url, sha256, storage_key, title, doc_type, extract_state)
       VALUES ('upload', 'upload://test/1', repeat('a', 64), 'raw/upload/aa/' || repeat('a', 64), 'Test doc', 'upload_pdf', 'done')
       RETURNING id::text`)).rows[0]!.id;
    await db.query(`INSERT INTO agent_job (document_id, sha256, source_id, prompt_version, state, stage, areas, stats)
                    VALUES ($1, repeat('a', 64), 'upload', '0.1', 'needs_review', 'published', '{debt}', '{"review": 1}')`, [doc]);
    const list = await app.inject({ method: 'GET', url: '/api/documents?status=needs_review', headers: await auth(u.staff) });
    expect(list.statusCode).toBe(200);
    expect(list.json().documents.map((d: { id: string }) => d.id)).toContain(doc);
    const one = await app.inject({ method: 'GET', url: `/api/documents/${doc}`, headers: await auth(u.staff) });
    expect(one.json()).toMatchObject({ id: doc, status: 'needs_review', areas: ['debt'], candidates: [], usage: [] });
    expect((await app.inject({ method: 'GET', url: '/api/documents?status=bogus', headers: await auth(u.staff) })).statusCode)
      .toBe(400);
  });

  it('records review decisions for the worker, one open decision per item', async () => {
    const fact = (await db.query<{ id: string }>(
      `INSERT INTO fact (subject_type, subject_id, field, value, tag, source_name, valid_from, producer, quote, dedupe_key)
       VALUES ('site', $1, 'lease_tenant', '"Tenant Co"', 'F', 'Test', '2026-10-01', 'agent:debt', 'Tenant Co', $2)
       RETURNING id::text`, [site, uid('dk')])).rows[0]!.id;
    const item = (await db.query<{ id: string }>(
      `INSERT INTO review_item (kind, ref_type, ref_id, title, reason, payload)
       VALUES ('fact', 'fact', $1, 'agents-test lease_tenant', 'verifier: unsupported', '{"area": "lease"}') RETURNING id::text`,
      [fact])).rows[0]!.id;
    const h = await auth(u.staff);
    const listed = await app.inject({ method: 'GET', url: '/api/review?area=lease', headers: h });
    expect(listed.json().items.find((i: { id: string }) => i.id === item)).toMatchObject({ field: 'lease_tenant', quote: 'Tenant Co' });
    expect((await app.inject({ method: 'POST', url: `/api/review/${item}/reject`, headers: h, payload: {} })).statusCode).toBe(400);
    expect((await app.inject({ method: 'POST', url: `/api/review/${item}/edit`, headers: h, payload: { value: 'X' } })).statusCode)
      .toBe(400);
    const approved = await app.inject({ method: 'POST', url: `/api/review/${item}/approve`, headers: h, payload: {} });
    expect(approved.statusCode).toBe(202); // no worker in this test: the decision waits to be applied
    expect(approved.json()).toMatchObject({ status: 'pending', action: 'approve', decidedBy: u.staff });
    const again = await app.inject({ method: 'POST', url: `/api/review/${item}/reject`, headers: h, payload: { reason: 'wrong' } });
    expect(again.json()).toMatchObject({ error: 'conflict', reason: 'decision_pending' });
    const detail = await app.inject({ method: 'GET', url: `/api/review/${item}`, headers: h });
    expect(detail.json().pendingDecisionId).toBe(approved.json().id);
    const counts = await app.inject({ method: 'GET', url: '/api/agents/status', headers: h });
    expect(counts.json()).toMatchObject({ backend: 'not_configured', pendingDecisions: 1 });
  });
});
