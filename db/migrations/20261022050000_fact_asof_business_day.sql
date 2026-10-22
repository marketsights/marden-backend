-- migrate:up
-- fact_asof(day): what Marden knew on a day means learned before that day ended in business time
-- (midnight US Eastern after it; snapshots and as-of days are dated in US Eastern, spec 22). It compared
-- learned_at with (day + 1)::timestamptz, which in the UTC session is midnight UTC: 8 pm EDT / 7 pm EST of
-- the day, so a fact learned that evening was left out of the day's snapshot and of ?asOf reads until the
-- next day. Same rows and precedence otherwise. (marden.readmodels.context.day_end; marden.core.facts
-- facts_asof and apps/api/src/lib/licensing.ts factSource use the same instant when sources are hidden.)
CREATE OR REPLACE FUNCTION fact_asof(as_of date)
RETURNS SETOF fact LANGUAGE sql STABLE AS $$
  SELECT DISTINCT ON (subject_type, subject_id, field) f.*
  FROM fact f
  WHERE f.status IN ('published', 'superseded')
    AND f.valid_from <= as_of
    AND f.learned_at < ((as_of + 1)::timestamp AT TIME ZONE 'America/New_York')
    AND (f.valid_to IS NULL OR f.valid_to > as_of)
  ORDER BY subject_type, subject_id, field,
           (tag = 'M'), valid_from DESC,
           (CASE tag WHEN 'F' THEN 0 WHEN 'R' THEN 1 ELSE 2 END), learned_at DESC, id DESC
$$;

-- migrate:down
CREATE OR REPLACE FUNCTION fact_asof(as_of date)
RETURNS SETOF fact LANGUAGE sql STABLE AS $$
  SELECT DISTINCT ON (subject_type, subject_id, field) f.*
  FROM fact f
  WHERE f.status IN ('published', 'superseded')
    AND f.valid_from <= as_of
    AND f.learned_at < (as_of + 1)::timestamptz
    AND (f.valid_to IS NULL OR f.valid_to > as_of)
  ORDER BY subject_type, subject_id, field,
           (tag = 'M'), valid_from DESC,
           (CASE tag WHEN 'F' THEN 0 WHEN 'R' THEN 1 ELSE 2 END), learned_at DESC, id DESC
$$;
