// Query-string helpers. Multi-value filters accept repeated params or comma-separated lists.
import type { FastifyRequest } from 'fastify';
import { badRequest } from '../errors.js';
import { businessToday } from './business-day.js';

export type Query = Record<string, string | string[] | undefined>;

export const queryOf = (req: FastifyRequest): Query => (req.query ?? {}) as Query;

export function first(q: Query, name: string): string | undefined {
  const v = q[name];
  const s = Array.isArray(v) ? v[0] : v;
  return s === undefined || s.trim() === '' ? undefined : s.trim();
}

export function list(q: Query, name: string): string[] {
  const v = q[name];
  const raw = Array.isArray(v) ? v : v === undefined ? [] : [v];
  return raw.flatMap((s) => s.split(',')).map((s) => s.trim()).filter(Boolean);
}

export function num(q: Query, name: string): number | undefined {
  const s = first(q, name);
  if (s === undefined) return undefined;
  const n = Number(s);
  if (!Number.isFinite(n)) throw badRequest(`${name} must be a number`);
  return n;
}

export function flag(q: Query, name: string): boolean {
  const s = first(q, name)?.toLowerCase();
  return s === 'true' || s === '1' || s === 'yes' || s === 'on';
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

export function isoDate(q: Query, name: string): string | undefined {
  const s = first(q, name);
  if (s === undefined) return undefined;
  const d = new Date(`${s}T00:00:00Z`);
  if (!ISO_DATE.test(s) || Number.isNaN(d.getTime()) || d.toISOString().slice(0, 10) !== s) {
    throw badRequest(`${name} must be a date YYYY-MM-DD`);
  }
  return s;
}

/** `?asOf=YYYY-MM-DD` snapshot date, accepted on every GET; null = latest. */
export const asOfOf = (req: FastifyRequest): string | null => isoDate(queryOf(req), 'asOf') ?? null;

export function page(q: Query): number {
  const p = num(q, 'page') ?? 1;
  if (!Number.isInteger(p) || p < 1 || p > 100_000) throw badRequest('page must be a positive integer');
  return p;
}

/** Escape LIKE wildcards so user text matches literally. */
export const likePattern = (s: string): string => `%${s.replace(/[\\%_]/g, (c) => `\\${c}`)}%`;

/** The date a snapshot answers for: the asOf date, or today in US Eastern (snapshots are dated Eastern). */
export const asOfDay = (asOf: string | null): string => asOf ?? businessToday();
