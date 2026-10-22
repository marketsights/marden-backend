-- migrate:up
-- Last visit (spec 01 Home: "what moved on your names since your last visit"). A member's session in an
-- organization starts with their first request after 30 minutes without one, measured against
-- membership.last_active_at (stamped by apps/api/src/auth/activity.ts at most every few minutes). On that
-- request the API moves the session it ends to last_visit_at and starts a new one:
--   session_started_at: when the member's current session started;
--   last_visit_at:      when their previous session started (GET /api/home serves it as lastVisit).
-- Both stay NULL until the API has seen those sessions start (shown without a date, never guessed from
-- last_active_at, which is the end of a session, not its start).
ALTER TABLE membership ADD COLUMN session_started_at timestamptz, ADD COLUMN last_visit_at timestamptz;

-- migrate:down
ALTER TABLE membership DROP COLUMN IF EXISTS last_visit_at, DROP COLUMN IF EXISTS session_started_at;
