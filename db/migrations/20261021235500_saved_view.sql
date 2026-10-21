-- migrate:up
-- Saved views (spec 03 "Saved views"; also the Data centers and On-site power headers, specs 14, 15):
-- a named set of a list screen's URL filters and sort, kept per user within the org. Served by
-- GET / POST /api/saved-views and DELETE /api/saved-views/:id (apps/api/src/routes/saved-views.ts).
-- `query` is the screen's URL query string without the '?', e.g. 'tenant=Oracle&sort=-debt'.
CREATE TABLE saved_view (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id      text NOT NULL REFERENCES org(id) ON DELETE CASCADE,
  user_id     text NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  screen      text NOT NULL,
  name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  query       text NOT NULL CHECK (length(query) <= 2000),
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, user_id, screen, name)
);
CREATE INDEX saved_view_owner ON saved_view (org_id, user_id, screen, created_at DESC);

-- migrate:down
DROP TABLE IF EXISTS saved_view;
