-- migrate:up
-- Last active (spec 13 Team table): when the member last used Marden in this organization. The API
-- stamps it on authenticated requests (apps/api/src/auth/activity.ts), at most once every few minutes
-- per member, and GET /api/account serves it as team[].lastActiveAt. NULL until the member's first
-- request after this migration (shown as n/d, never guessed).
ALTER TABLE membership ADD COLUMN last_active_at timestamptz;

-- migrate:down
ALTER TABLE membership DROP COLUMN IF EXISTS last_active_at;
