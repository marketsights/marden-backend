// Last active (spec 13 Team table): stamps membership.last_active_at when a signed-in member makes a
// request. Throttled twice so it costs nothing per request: this process skips the write when it
// stamped the same member within the interval, and the UPDATE itself only moves a stamp that is older
// than the interval (several API processes, or a restart, write at most once per interval).
// Last visit (spec 01 Home subtitle): the same UPDATE starts a new session when the previous stamp is
// more than SESSION_IDLE_MS old (or there is none): the session that ended moves to last_visit_at (its
// start) and session_started_at becomes now. A session whose start the API did not see stays NULL.
import type { Queryable } from '../db.js';

/** How stale a stamp may get: Last active is shown by day, so minutes are plenty. */
export const ACTIVITY_INTERVAL_MS = 5 * 60_000;
/** Idle time after which the next request starts a new session (a new "visit"). */
export const SESSION_IDLE_MS = 30 * 60_000;

/** The member's previous request (stamp) is older than the idle time, or there is none: a new session. */
const NEW_SESSION = `(last_active_at IS NULL OR last_active_at < now() - make_interval(secs => $4::double precision / 1000))`;

export interface ActivityRecorder {
  /** Stamps the member as active now, unless it was stamped within the interval. */
  touch(orgId: string, userId: string): Promise<void>;
}

export function createActivityRecorder(
  db: Queryable,
  opts: { intervalMs?: number; sessionIdleMs?: number; now?: () => number } = {},
): ActivityRecorder {
  const intervalMs = opts.intervalMs ?? ACTIVITY_INTERVAL_MS;
  const idleMs = opts.sessionIdleMs ?? SESSION_IDLE_MS;
  const now = opts.now ?? Date.now;
  const stamped = new Map<string, number>();
  return {
    async touch(orgId, userId) {
      const key = `${orgId}\u0000${userId}`;
      const at = now();
      const last = stamped.get(key);
      if (last !== undefined && at - last < intervalMs) return;
      stamped.set(key, at);
      // Bounded: members of this deployment; drop stale entries when it grows.
      if (stamped.size > 10_000) {
        for (const [k, t] of stamped) if (at - t >= intervalMs) stamped.delete(k);
      }
      try {
        // SET reads the row as it was, so a new session moves the old session_started_at to last_visit_at.
        await db.query(
          `UPDATE membership SET
             last_visit_at = CASE WHEN ${NEW_SESSION} THEN session_started_at ELSE last_visit_at END,
             session_started_at = CASE WHEN ${NEW_SESSION} THEN now() ELSE session_started_at END,
             last_active_at = now()
           WHERE org_id = $1 AND user_id = $2
             AND (last_active_at IS NULL OR last_active_at < now() - make_interval(secs => $3::double precision / 1000))`,
          [orgId, userId, intervalMs, idleMs],
        );
      } catch (err) {
        // Not stamped: the next request tries again.
        if (stamped.get(key) === at) stamped.delete(key);
        throw err;
      }
    },
  };
}
