// Saved views (spec 03 "Saved views"; specs 14, 15): a list screen's URL filters under a name, per user
// within the org. Saving the same name replaces the query; the stored query drops asOf and empty values;
// another user's view is invisible and cannot be deleted; screens outside the list are refused.
import type { FastifyInstance } from 'fastify';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Db } from '../src/db.js';
import { MAX_SAVED_VIEWS, normalizeQuery } from '../src/routes/saved-views.js';
import { makeApp, testDb } from './helpers.js';

describe('saved views', () => {
  let db: Db;
  let app: FastifyInstance;

  beforeAll(async () => {
    db = testDb();
    app = await makeApp({ mode: 'dev', db });
  });
  afterAll(async () => {
    await db.query(`DELETE FROM saved_view`);
    await app.close();
    await db.close();
  });

  const list = async (screen = 'completion') => (await app.inject({ method: 'GET', url: `/api/saved-views?screen=${screen}` })).json();
  const save = (body: unknown) => app.inject({ method: 'POST', url: '/api/saved-views', payload: body as Record<string, unknown> });

  it('stores the query string in a stable form', () => {
    expect(normalizeQuery('?tenant=Oracle&sort=-debt&asOf=2026-10-01&state=')).toBe('sort=-debt&tenant=Oracle');
    expect(normalizeQuery('')).toBe('');
  });

  it('saves, lists newest first, replaces a name, and deletes', async () => {
    expect(await list()).toEqual([]);
    const first = await save({ screen: 'completion', name: 'Oracle sites', query: 'tenant=Oracle&asOf=2026-10-01' });
    expect(first.statusCode).toBe(201);
    expect(first.json()).toMatchObject({ screen: 'completion', name: 'Oracle sites', query: 'tenant=Oracle' });
    const second = await save({ screen: 'completion', name: 'Texas, tightest', query: 'state=TX&sort=cushion' });
    expect(second.statusCode).toBe(201);
    expect((await list()).map((v: { name: string }) => v.name)).toEqual(['Texas, tightest', 'Oracle sites']);

    const replaced = await save({ screen: 'completion', name: 'Oracle sites', query: 'tenant=Oracle&watchlist=1' });
    expect(replaced.statusCode).toBe(200);
    expect(replaced.json().id).toBe(first.json().id);
    expect((await list()).find((v: { name: string }) => v.name === 'Oracle sites').query).toBe('tenant=Oracle&watchlist=1');
    expect(await list('data-centers')).toEqual([]); // per screen

    const del = await app.inject({ method: 'DELETE', url: `/api/saved-views/${second.json().id}` });
    expect(del.statusCode).toBe(204);
    expect((await list()).map((v: { name: string }) => v.name)).toEqual(['Oracle sites']);
    expect((await app.inject({ method: 'DELETE', url: `/api/saved-views/${second.json().id}` })).statusCode).toBe(404);
  });

  it('keeps one user’s views from another in the same org', async () => {
    await db.query(`INSERT INTO app_user (id, email) VALUES ('sv-other', 'sv-other@example.test') ON CONFLICT DO NOTHING`);
    const res = await db.query<{ id: string }>(
      `INSERT INTO saved_view (org_id, user_id, screen, name, query) VALUES ('dev', 'sv-other', 'completion', 'Theirs', 'state=NM') RETURNING id::text`,
    );
    const theirs = res.rows[0]?.id;
    expect((await list()).map((v: { name: string }) => v.name)).not.toContain('Theirs');
    expect((await app.inject({ method: 'DELETE', url: `/api/saved-views/${theirs}` })).statusCode).toBe(404);
    await db.query(`DELETE FROM saved_view WHERE user_id = 'sv-other'`);
    await db.query(`DELETE FROM app_user WHERE id = 'sv-other'`);
  });

  it('refuses unknown screens, empty names and too many views', async () => {
    expect((await app.inject({ method: 'GET', url: '/api/saved-views?screen=nope' })).statusCode).toBe(400);
    expect((await app.inject({ method: 'GET', url: '/api/saved-views' })).statusCode).toBe(400);
    expect((await save({ screen: 'nope', name: 'x', query: '' })).statusCode).toBe(400);
    expect((await save({ screen: 'completion', name: '  ', query: '' })).statusCode).toBe(400);
    for (let i = 0; i < MAX_SAVED_VIEWS; i += 1) await save({ screen: 'power', name: `View ${i}`, query: `state=TX&i=${i}` });
    const over = await save({ screen: 'power', name: 'One too many', query: '' });
    expect(over.statusCode).toBe(409);
    expect((await save({ screen: 'power', name: 'View 3', query: 'state=NM' })).statusCode).toBe(200); // replacing is fine
  });
});
