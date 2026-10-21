-- migrate:up
-- Interconnection queue entries (ISO/RTO generation, transmission and large-load requests).
-- Identity only, like campus and power_plant: every attribute (status, MW, dates, fuel, links to
-- Marden campuses, sites and plants) is a fact with subject_type 'queue_project' and
-- subject_id = this id ('<iso>:<queue id>', e.g. 'pjm:AE1-070', 'nyiso:1670').
CREATE TABLE IF NOT EXISTS queue_project (
  id             text PRIMARY KEY,             -- '<iso>:<queue id>'
  iso            text NOT NULL,                -- 'pjm', 'miso', 'nyiso', 'isone', 'ercot', ...
  queue_id       text NOT NULL,                -- the operator's queue position / project number
  name           text,
  state          text,
  county         text,
  first_seen_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (iso, queue_id),
  CHECK (id = iso || ':' || queue_id)
);
CREATE INDEX IF NOT EXISTS queue_project_place ON queue_project (state, county);

-- migrate:down
DROP TABLE IF EXISTS queue_project;
