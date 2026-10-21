-- migrate:up
-- Point-in-time reads (marden.models.pit) and several read-model dependency lookups
-- (marden.readmodels.deps) filter facts on status IN ('published', 'superseded'), which the
-- published-only index fact_subject_pub cannot serve: each such lookup was a sequential scan of
-- the whole fact table, run once per written instrument, plant or entity value (an N-PORT run
-- writes thousands). This index serves both those lookups and status = 'published'.
CREATE INDEX fact_subject_known ON fact (subject_type, subject_id, field)
  WHERE status IN ('published', 'superseded');

-- migrate:down
DROP INDEX fact_subject_known;
