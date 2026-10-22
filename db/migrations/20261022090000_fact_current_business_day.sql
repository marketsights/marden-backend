-- migrate:up
-- fact_current: a fact whose valid_to is D is current through D - 1 in business time (US Eastern; snapshots and
-- as-of days are dated in US Eastern, spec 22). It compared valid_to with current_date, the date in the
-- session's zone: UTC, so from 8 pm EDT / 7 pm EST a fact ending the next New York day had already dropped out,
-- a day early. (now() AT TIME ZONE 'America/New_York')::date is the New York date whatever the session's zone,
-- as stress_run's today. Same columns, rows and precedence otherwise. (marden.core.business_day.business_today
-- and apps/api/src/lib/business-day.ts businessToday give the same day where code reads fact itself.)
CREATE OR REPLACE VIEW fact_current AS
SELECT DISTINCT ON (subject_type, subject_id, field) *
FROM fact
WHERE status = 'published' AND (valid_to IS NULL OR valid_to > (now() AT TIME ZONE 'America/New_York')::date)
ORDER BY subject_type, subject_id, field,
         (tag = 'M'), valid_from DESC,
         (CASE tag WHEN 'F' THEN 0 WHEN 'R' THEN 1 ELSE 2 END), learned_at DESC, id DESC;

-- migrate:down
CREATE OR REPLACE VIEW fact_current AS
SELECT DISTINCT ON (subject_type, subject_id, field) *
FROM fact
WHERE status = 'published' AND (valid_to IS NULL OR valid_to > current_date)
ORDER BY subject_type, subject_id, field,
         (tag = 'M'), valid_from DESC,
         (CASE tag WHEN 'F' THEN 0 WHEN 'R' THEN 1 ELSE 2 END), learned_at DESC, id DESC;
