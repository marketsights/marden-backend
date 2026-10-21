// Shared helpers for read-model routes: a page load is one indexed read of a precomputed document.
import type { FastifyRequest } from 'fastify';
import type { z } from 'zod';
import type { Db } from '../db.js';
import { badRequest, notFound, notReady } from '../errors.js';
import { subjectExists } from '../queries/rows.js';

/** The identity table a keyed view belongs to; an unknown key is a 404, a known one not yet built is a 503. */
export interface Subject {
  table: 'site' | 'instrument' | 'campus' | 'power_plant' | 'entity';
  column: 'slug' | 'id';
}

export const SITE: Subject = { table: 'site', column: 'slug' };
export const INSTRUMENT: Subject = { table: 'instrument', column: 'id' };
export const CAMPUS: Subject = { table: 'campus', column: 'id' };
export const PLANT: Subject = { table: 'power_plant', column: 'id' };
export const ENTITY: Subject = { table: 'entity', column: 'id' };

export interface SubjectKey { key: string; subject: Subject }

/** A key that belongs to an identity table row (site slug, instrument id, campus id, ...). */
export const keyed = (subject: Subject, key: string): SubjectKey => ({ key, subject });

export async function serveDoc<T>(db: Db, view: string, ref: string | SubjectKey, asOf: string | null): Promise<T> {
  const { key, subject } = typeof ref === 'string' ? { key: ref, subject: null } : ref;
  const doc = await db.readModel<T>(view, key, asOf);
  if (doc !== null) return doc;
  if (subject && !(await subjectExists(db, subject.table, subject.column, key))) throw notFound(subject.table);
  throw notReady(view, key);
}

export function param(req: FastifyRequest, name: string): string {
  const value = (req.params as Record<string, string | undefined>)[name];
  if (!value || value.length > 200) throw badRequest(`${name} is required`);
  return value;
}

export function parseBody<S extends z.ZodType>(schema: S, body: unknown): z.infer<S> {
  const parsed = schema.safeParse(body);
  if (!parsed.success) {
    throw badRequest('invalid_body', parsed.error.issues.map((i) => ({ path: i.path.join('.'), message: i.message })));
  }
  return parsed.data;
}

export const clientIp = (req: FastifyRequest): string => req.ip;
