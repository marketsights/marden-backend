// Last active (spec 13 Team table): stamps membership.last_active_at when a signed-in member makes a
// request. Throttled twice so it costs nothing per request: this process skips the write when it
// stamped the same member within the interval, and the UPDATE itself only moves a stamp that is older
// than the interval (several API processes, or a restart, write at most once per interval).
import type { Queryable } from '../db.js';

/** How stale a stamp may get: Last active is shown by day, so minutes are plenty. */
export const ACTIVITY_INTERVAL_MS = 5 * 60_000;

export interface ActivityRecorder {
  /** Stamps the member as active now, unless it was stamped within the interval. */
  touch(orgId: string, userId: string): Promise<void>;
}

export function createActivityRecorder(db: Queryable, opts: { intervalMs?: number; now?: () => number } = {}): ActivityRecorder {
  const intervalMs = opts.intervalMs ?? ACTIVITY_INTERVAL_MS;
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
        await db.query(
          `UPDATE membership SET last_active_at = now()
           WHERE org_id = $1 AND user_id = $2
             AND (last_active_at IS NULL OR last_active_at < now() - make_interval(secs => $3::double precision / 1000))`,
          [orgId, userId, intervalMs],
        );
      } catch (err) {
        // Not stamped: the next request tries again.
        if (stamped.get(key) === at) stamped.delete(key);
        throw err;
      }
    },
  };
}
