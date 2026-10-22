// Source heartbeats (spec 12, 22): each source with its latest run. With ?asOf the last good run and
// the latest run are read from source_run as they stood at the end of that day (midnight US Eastern after it).
import type { SourceHealth, SourcesHealthResponse, SourceStatus } from '../contracts/app.js';
import type { Db } from '../db.js';
import { dayEnd, dayEndAt } from '../lib/business-day.js';

interface SourceRow {
  id: string; name: string; kind: string; cadence_minutes: number | null; blocked: boolean; blocked_reason: string | null;
  last_ok_at: Date | null; last_error_at: Date | null; last_error: string | null;
  run_started_at: Date | null; run_finished_at: Date | null; run_ok: boolean | null;
  run_items_seen: number | null; run_items_new: number | null; run_error: string | null;
}

const MINUTE = 60_000;

/**
 * ok while the last good run is within its cadence (plus a small allowance for run time), delayed when
 * older than that (spec 00 "Stale data"), down past three cadences, when it never succeeded, or blocked.
 */
export function sourceStatus(
  s: { blocked: boolean; cadenceMinutes: number | null; lastOkAt: Date | null; lastRunOk: boolean | null },
  now: Date,
): SourceStatus {
  if (s.blocked || !s.lastOkAt) return 'down';
  if (s.cadenceMinutes === null) return s.lastRunOk === false ? 'delayed' : 'ok';
  const ageMinutes = (now.getTime() - s.lastOkAt.getTime()) / MINUTE;
  const allowance = Math.max(5, s.cadenceMinutes * 0.1);
  if (ageMinutes <= s.cadenceMinutes + allowance) return 'ok';
  if (ageMinutes <= s.cadenceMinutes * 3) return 'delayed';
  return 'down';
}

const iso = (d: Date | null): string | null => (d ? d.toISOString() : null);

function toHealth(r: SourceRow, now: Date): SourceHealth {
  return {
    id: r.id, name: r.name, kind: r.kind, cadenceMinutes: r.cadence_minutes,
    blocked: r.blocked, blockedReason: r.blocked_reason,
    lastOkAt: iso(r.last_ok_at), lastErrorAt: iso(r.last_error_at), lastError: r.last_error,
    lastRun: r.run_started_at
      ? {
          startedAt: r.run_started_at.toISOString(), finishedAt: iso(r.run_finished_at), ok: r.run_ok,
          itemsSeen: r.run_items_seen ?? 0, itemsNew: r.run_items_new ?? 0, error: r.run_error,
        }
      : null,
    status: sourceStatus(
      { blocked: r.blocked, cadenceMinutes: r.cadence_minutes, lastOkAt: r.last_ok_at, lastRunOk: r.run_ok },
      now,
    ),
  };
}

export async function sourcesHealth(db: Db, asOf: string | null): Promise<SourcesHealthResponse> {
  const now = asOf ? dayEndAt(asOf) : new Date();
  const res = await db.query<SourceRow>(
    `SELECT s.id, s.name, s.kind, s.cadence_minutes, s.blocked, s.blocked_reason,
       CASE WHEN $1::date IS NULL THEN s.last_ok_at ELSE ok.at END AS last_ok_at,
       CASE WHEN $1::date IS NULL THEN s.last_error_at ELSE bad.at END AS last_error_at,
       CASE WHEN $1::date IS NULL THEN s.last_error ELSE bad.error END AS last_error,
       lr.started_at AS run_started_at, lr.finished_at AS run_finished_at, lr.ok AS run_ok,
       lr.items_seen AS run_items_seen, lr.items_new AS run_items_new, lr.error AS run_error
     FROM source s
     LEFT JOIN LATERAL (SELECT * FROM source_run r WHERE r.source_id = s.id
                        AND ($1::date IS NULL OR r.started_at < ${dayEnd('$1')})
                        ORDER BY r.started_at DESC, r.id DESC LIMIT 1) lr ON true
     LEFT JOIN LATERAL (SELECT max(r.finished_at) AS at FROM source_run r WHERE r.source_id = s.id AND r.ok
                        AND r.finished_at < ${dayEnd('$1')}) ok ON $1::date IS NOT NULL
     LEFT JOIN LATERAL (SELECT coalesce(r.finished_at, r.started_at) AS at, r.error FROM source_run r
                        WHERE r.source_id = s.id AND r.ok = false AND r.started_at < ${dayEnd('$1')}
                        ORDER BY r.started_at DESC, r.id DESC LIMIT 1) bad ON $1::date IS NOT NULL
     ORDER BY s.name, s.id`,
    [asOf],
  );
  return { asOf: now.toISOString(), sources: res.rows.map((r) => toHealth(r, now)) };
}
