// Stress (spec 09): the Excel export of a run and the saved-scenario shape the web app reads. The
// stress model's SQL function stress_run(jsonb) is stood in for by a test function in the throwaway
// schema, so the API's own handling (request parsing, rows, queued answers) is what is tested here.
import ExcelJS from 'exceljs';
import type { FastifyInstance } from 'fastify';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { makeApp, testDb } from './helpers.js';

/** A stand-in stress_run that echoes the request into one debt-hit row. */
const ECHO_RUN = `
CREATE OR REPLACE FUNCTION stress_run(req jsonb) RETURNS jsonb LANGUAGE sql AS $$
  SELECT jsonb_build_object(
    'kpis', jsonb_build_object('exposedB', 15, 'valueLostB', 0.7, 'valueLostPct', 4.6, 'loansBelow95', '[]'::jsonb, 'fundsHit', 0),
    'flow', '[]'::jsonb,
    'rows', jsonb_build_array(jsonb_build_object(
      'instrumentId', 'ST-TEST-LOAN', 'name', 'Test construction loan',
      'sizeB', jsonb_build_object('value', 15, 'tag', 'R', 'asOf', '2026-10-15', 'sourceName', 'Test record'),
      'why', 'Delay of ' || (req->'params'->>'months') || ' months, ' || (req->>'severity'),
      'priceNow', 97.2, 'priceAfter', 92.6, 'holders', 'Test bank')))
$$`;
/** A stand-in stress_run shaped like the real model's answer: rows out of order, the tenant's notes, one unpriced row. */
const MODEL_RUN = `
CREATE OR REPLACE FUNCTION stress_run(req jsonb) RETURNS jsonb LANGUAGE sql AS $$
  SELECT jsonb_build_object(
    'status', 'done', 'asOf', '2026-10-21',
    'basis', jsonb_build_object('methodVersion', 'stress v0.1 (model v0)', 'sites', jsonb_build_object('jupiter', jsonb_build_object('rule', 'T1'))),
    'kpis', jsonb_build_object('exposedB', 74.45, 'valueLostB', 0.964, 'valueLostPct', 1.29, 'loansBelow95', '[]'::jsonb, 'fundsHit', 1381),
    'flow', '[]'::jsonb,
    'rows', jsonb_build_array(
      jsonb_build_object('instrumentId', 'notes:oracle', 'name', 'oracle notes (53 issues)', 'sizeB', jsonb_build_object('value', 119.5, 'tag', 'F', 'asOf', '2026-10-21', 'sourceName', 'Marden instrument facts (sum)'),
        'why', 'Tenant''s own notes re-priced', 'priceNow', 77.092, 'priceAfter', 72.458, 'holders', '7123 fund lines'),
      jsonb_build_object('instrumentId', 'EX-ABILENE-000', 'name', 'Stargate Abilene Phase 1 construction loan', 'sizeB', jsonb_build_object('value', 2.3, 'tag', 'M', 'asOf', '2026-10-20', 'sourceName', 'Marden research archive'),
        'why', 'Tenant oracle cut to BB+', 'ruleCode', 'T1', 'priceNow', 99.407, 'priceAfter', 98.762, 'holders', 'no fund holds it'),
      jsonb_build_object('instrumentId', 'EX-UNPRICED-1', 'name', 'Unpriced loan', 'sizeB', jsonb_build_object('value', 1, 'tag', 'R', 'asOf', '2026-10-20', 'sourceName', 'Press'),
        'why', 'Tenant oracle cut to BB+', 'ruleCode', 'T1', 'priceNow', 95, 'priceAfter', null, 'holders', ''),
      jsonb_build_object('instrumentId', 'EX-SALINE-308', 'name', 'Saline Project bonds', 'sizeB', jsonb_build_object('value', 14, 'tag', 'M', 'asOf', '2026-10-20', 'sourceName', 'Marden research archive'),
        'why', 'Tenant oracle cut to BB+', 'ruleCode', 'T1', 'priceNow', 85.244, 'priceAfter', 82.867, 'holders', 'Pimco Income Fund +343 more')))
$$`;
const QUEUED_RUN = `CREATE OR REPLACE FUNCTION stress_run(req jsonb) RETURNS jsonb LANGUAGE sql AS $$ SELECT '{"status":"queued","jobId":"job-1"}'::jsonb $$`;
/** Queued on the first call, done on the next (the model's worker stored the result in between). */
const QUEUED_THEN_DONE = `
CREATE TABLE IF NOT EXISTS stress_test_calls (n int NOT NULL);
DELETE FROM stress_test_calls;
CREATE OR REPLACE FUNCTION stress_run(req jsonb) RETURNS jsonb LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO stress_test_calls VALUES (1);
  IF (SELECT count(*) FROM stress_test_calls) < 2 THEN
    RETURN '{"status":"queued","jobId":7,"subject":"custom:abc"}'::jsonb;
  END IF;
  RETURN jsonb_build_object('kpis', '{}'::jsonb, 'flow', '[]'::jsonb, 'rows', '[]'::jsonb, 'status', 'done');
END $$`;
/** Drops only this test schema's stand-in (search_path is <test schema>, public: an unqualified DROP
 *  would reach the real stress_run in public once the stand-in is gone). */
const DROP_STAND_IN = `DO $$ BEGIN EXECUTE format('DROP FUNCTION IF EXISTS %I.stress_run(jsonb)', current_schema()); END $$`;

describe('stress', () => {
  let db: Db;
  let app: FastifyInstance;

  beforeAll(async () => {
    db = testDb();
    app = await makeApp({ mode: 'dev', db });
  });
  afterEach(async () => {
    await db.query(DROP_STAND_IN);
    delete process.env.MARDEN_STRESS_WAIT_MS;
  });
  afterAll(async () => {
    await db.query(`DELETE FROM stress_saved WHERE org_id = 'dev'`);
    await app.close();
    await db.close();
  });

  const get = (url: string) => app.inject({ method: 'GET', url });

  it('answers 503 not_ready for the export until the stress model exists, and 400 for a bad run', async () => {
    const res = await get('/api/export/stress.xlsx?scenario=power_delay&severity=base&months=6');
    expect(res.statusCode).toBe(503);
    expect(res.json()).toEqual({ error: 'not_ready', view: 'model:stress', key: 'power_delay' });
    expect((await get('/api/export/stress.xlsx?scenario=meteor')).statusCode).toBe(400);
    expect((await get('/api/export/stress.xlsx')).statusCode).toBe(400);
    expect((await get('/api/export/stress.xlsx?scenario=power_delay&severity=extreme')).statusCode).toBe(400);
  });

  it('exports the debt-hit rows of the run the URL names, with its parameters', async () => {
    await db.query(ECHO_RUN);
    const res = await get('/api/export/stress.xlsx?scenario=power_delay&severity=severe&months=9');
    expect(res.statusCode).toBe(200);
    expect(res.headers['content-type']).toContain('spreadsheetml');
    expect(res.headers['content-disposition']).toContain('marden-stress-');
    const wb = new ExcelJS.Workbook();
    await wb.xlsx.load(res.rawPayload as unknown as ArrayBuffer);
    const sheet = wb.worksheets[0];
    const header = (sheet?.getRow(1).values as unknown[]).filter(Boolean);
    expect(header).toEqual(['instrumentId', 'instrument', 'sizeB', 'why', 'rule', 'priceNow', 'priceAfter', 'change', 'holders']);
    const cells = (sheet?.getRow(2).values as unknown[]).filter((v) => v !== undefined);
    expect(cells).toEqual(['ST-TEST-LOAN', 'Test construction loan', 15, 'Delay of 9 months, severe', '—', 97.2, 92.6, -4.6, 'Test bank']);
    const sources = wb.worksheets[1];
    expect(sources?.rowCount).toBeGreaterThan(1); // sizeB is a Sourced value: listed with its tag
  });

  it('exports the debt-hit table as the screen shows it: its columns, worst first, notes last, model prices tagged M', async () => {
    await db.query(MODEL_RUN);
    const res = await get('/api/export/stress.xlsx?scenario=tenant_downgrade&severity=base&tenant=oracle&rating=BB%2B');
    expect(res.statusCode).toBe(200);
    expect(res.headers['content-disposition']).toContain('2026-10-21');
    const wb = new ExcelJS.Workbook();
    await wb.xlsx.load(res.rawPayload as unknown as ArrayBuffer);
    const data = wb.worksheets[0]!;
    const values = (n: number) => (data.getRow(n).values as unknown[]).slice(1);
    expect(values(1)).toEqual(['instrumentId', 'instrument', 'sizeB', 'why', 'rule', 'priceNow', 'priceAfter', 'change', 'holders']);
    expect(values(2)).toEqual(['EX-SALINE-308', 'Saline Project bonds', 14, 'Tenant oracle cut to BB+', 'T1', 85.244, 82.867, -2.377, 'Pimco Income Fund +343 more']);
    expect(values(3)[0]).toBe('EX-ABILENE-000');
    expect(values(4)).toEqual(['EX-UNPRICED-1', 'Unpriced loan', 1, 'Tenant oracle cut to BB+', 'T1', 95, 'n/d', 'n/d', 'n/d']);
    expect(values(5).slice(0, 5)).toEqual(['notes:oracle', 'oracle notes (53 issues)', 119.5, 'Tenant\'s own notes re-priced', '—']);
    expect(data.rowCount).toBe(5);
    const sources = wb.worksheets[1]!;
    const lines = Array.from({ length: sources.rowCount - 1 }, (_, i) => (sources.getRow(i + 2).values as unknown[]).slice(1));
    expect(lines).toContainEqual(['EX-SALINE-308', 'priceAfter', 82.867, 'M', 'Marden stress model', '', '2026-10-21', 'stress v0.1 (model v0)']);
    expect(lines).toContainEqual(['EX-SALINE-308', 'sizeB', 14, 'M', 'Marden research archive', '', '2026-10-20', '']);
  });

  it('passes the same request to the run endpoint', async () => {
    await db.query(ECHO_RUN);
    const res = await app.inject({ method: 'POST', url: '/api/stress/run', payload: { scenario: 'power_delay', params: { months: 3 }, severity: 'base' } });
    expect(res.statusCode).toBe(200);
    expect(res.json().rows[0]).toMatchObject({ instrumentId: 'ST-TEST-LOAN', why: 'Delay of 3 months, base' });
  });

  it('waits for a queued custom run and returns it once the model stored it', async () => {
    await db.query(QUEUED_THEN_DONE);
    const payload = { scenario: 'tenant_downgrade', params: { tenant: 'oracle', rating: 'BB' }, severity: 'base' };
    const res = await app.inject({ method: 'POST', url: '/api/stress/run', payload });
    expect(res.statusCode).toBe(200);
    expect(res.json()).toMatchObject({ status: 'done', rows: [] });
  });

  it('answers 202 with the job when the run is still queued after the wait', async () => {
    await db.query(QUEUED_RUN);
    process.env.MARDEN_STRESS_WAIT_MS = '300';
    const payload = { scenario: 'power_delay', params: { months: 9 }, severity: 'base' };
    const res = await app.inject({ method: 'POST', url: '/api/stress/run', payload });
    expect(res.statusCode).toBe(202);
    expect(res.json()).toEqual({ status: 'queued', jobId: 'job-1' });
  });

  it('does not export a run the model has only queued', async () => {
    await db.query(QUEUED_RUN);
    const res = await get('/api/export/stress.xlsx?scenario=tenant_downgrade&tenant=oracle&rating=BB%2B');
    expect(res.statusCode).toBe(503);
    expect(res.json()).toEqual({ error: 'not_ready', view: 'model:stress', key: 'tenant_downgrade' });
  });

  it('returns savedAt (= createdAt) on saved scenarios, as the web app reads it', async () => {
    const request = { scenario: 'power_delay', params: { months: 6 }, severity: 'base' };
    const saved = await app.inject({ method: 'POST', url: '/api/stress/saved', payload: { name: 'Power delay 6 months', request } });
    expect(saved.statusCode).toBe(201);
    const body = saved.json();
    expect(body).toMatchObject({ name: 'Power delay 6 months', request, result: null });
    expect(body.savedAt).toBe(body.createdAt);
    expect((await get('/api/stress/saved')).json()[0]).toMatchObject({ id: body.id, savedAt: body.createdAt });
  });

  it('stores the model result with a saved scenario when the model already has it, not a queued answer', async () => {
    await db.query(ECHO_RUN);
    const request = { scenario: 'power_delay', params: { months: 4 }, severity: 'base' };
    const done = (await app.inject({ method: 'POST', url: '/api/stress/saved', payload: { name: 'Four months', request } })).json();
    expect(done.result.rows[0]).toMatchObject({ instrumentId: 'ST-TEST-LOAN', why: 'Delay of 4 months, base' });
    expect(done.ranAt).not.toBeNull();
    expect((await get(`/api/stress/saved/${done.id}`)).json().result).toEqual(done.result);
    await db.query(QUEUED_RUN);
    const queued = (await app.inject({ method: 'POST', url: '/api/stress/saved', payload: { name: 'Queued', request } })).json();
    expect(queued).toMatchObject({ result: null, ranAt: null });
  });
});
