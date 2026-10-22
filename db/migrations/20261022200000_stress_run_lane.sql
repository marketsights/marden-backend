-- migrate:up
-- stress_run(jsonb) queues a custom run on the `stress` queue instead of `models`: the worker keeps a reserved
-- lane on it (marden.worker run_worker, MARDEN_STRESS_LANE_CONCURRENCY), so a run the Stress screen waits for never
-- queues behind long collector, agent or model jobs. Otherwise identical to 20261021210000_stress_run.sql.
-- A job already queued on `models` is still worked there (the main worker works every queue).
CREATE OR REPLACE FUNCTION stress_run(request jsonb) RETURNS jsonb
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  scen text := request->>'scenario';
  sev text := coalesce(request->>'severity', 'base');
  params jsonb := coalesce(request->'params', '{}'::jsonb);
  norm jsonb;
  subj text;
  hit record;
  job record;
  new_id bigint;
  today date := (now() AT TIME ZONE 'America/New_York')::date;
BEGIN
  IF scen IS NULL OR scen NOT IN ('end_user_funding_miss', 'tenant_downgrade', 'power_delay', 'contagion') THEN
    RETURN jsonb_build_object('status', 'error', 'error', 'unknown scenario: ' || coalesce(scen, 'null'));
  END IF;
  IF sev NOT IN ('base', 'severe') OR jsonb_typeof(params) <> 'object' THEN
    RETURN jsonb_build_object('status', 'error', 'error', 'severity must be base or severe; params an object');
  END IF;
  IF scen = 'contagion' AND coalesce(params->>'company', '') = '' THEN
    RETURN jsonb_build_object('status', 'error', 'error', 'contagion needs params.company');
  END IF;

  subj := CASE WHEN scen = 'contagion' THEN 'contagion:' || (params->>'company') || ':' || sev
               ELSE scen || ':' || sev END;
  SELECT output, as_of INTO hit FROM model_output
   WHERE model = 'stress' AND subject_type = 'scenario' AND subject_id = subj
   ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1;
  IF FOUND AND (params = '{}'::jsonb OR hit.output->'request'->'params' @> params) THEN
    RETURN hit.output || jsonb_build_object('status', 'done', 'asOf', hit.as_of, 'subject', subj);
  END IF;

  norm := jsonb_build_object('scenario', scen, 'params', params, 'severity', sev);
  subj := 'custom:' || md5(norm::text);
  SELECT output, as_of INTO hit FROM model_output
   WHERE model = 'stress' AND subject_type = 'scenario' AND subject_id = subj AND as_of >= today
   ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1;
  IF FOUND THEN
    RETURN hit.output || jsonb_build_object('status', 'done', 'asOf', hit.as_of, 'subject', subj);
  END IF;

  SELECT id, status INTO job FROM procrastinate_jobs
   WHERE queueing_lock = 'stress:' || subj AND status IN ('todo', 'doing')
   ORDER BY id DESC LIMIT 1;
  IF FOUND THEN
    RETURN jsonb_build_object('status', CASE WHEN job.status = 'doing' THEN 'running' ELSE 'queued' END,
                              'jobId', job.id, 'subject', subj);
  END IF;
  BEGIN
    SELECT (procrastinate_defer_jobs_v1(ARRAY[ROW(
              'stress', 'models.stress_custom', 0, NULL, 'stress:' || subj,
              jsonb_build_object('request', norm, 'key', subj), NULL
            )::procrastinate_job_to_defer_v1]))[1] INTO new_id;
  EXCEPTION WHEN unique_violation THEN  -- another caller queued it a moment ago
    SELECT id INTO new_id FROM procrastinate_jobs
     WHERE queueing_lock = 'stress:' || subj AND status = 'todo' ORDER BY id DESC LIMIT 1;
  END;
  RETURN jsonb_build_object('status', 'queued', 'jobId', new_id, 'subject', subj);
END;
$$;

-- migrate:down
CREATE OR REPLACE FUNCTION stress_run(request jsonb) RETURNS jsonb
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  scen text := request->>'scenario';
  sev text := coalesce(request->>'severity', 'base');
  params jsonb := coalesce(request->'params', '{}'::jsonb);
  norm jsonb;
  subj text;
  hit record;
  job record;
  new_id bigint;
  today date := (now() AT TIME ZONE 'America/New_York')::date;
BEGIN
  IF scen IS NULL OR scen NOT IN ('end_user_funding_miss', 'tenant_downgrade', 'power_delay', 'contagion') THEN
    RETURN jsonb_build_object('status', 'error', 'error', 'unknown scenario: ' || coalesce(scen, 'null'));
  END IF;
  IF sev NOT IN ('base', 'severe') OR jsonb_typeof(params) <> 'object' THEN
    RETURN jsonb_build_object('status', 'error', 'error', 'severity must be base or severe; params an object');
  END IF;
  IF scen = 'contagion' AND coalesce(params->>'company', '') = '' THEN
    RETURN jsonb_build_object('status', 'error', 'error', 'contagion needs params.company');
  END IF;

  subj := CASE WHEN scen = 'contagion' THEN 'contagion:' || (params->>'company') || ':' || sev
               ELSE scen || ':' || sev END;
  SELECT output, as_of INTO hit FROM model_output
   WHERE model = 'stress' AND subject_type = 'scenario' AND subject_id = subj
   ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1;
  IF FOUND AND (params = '{}'::jsonb OR hit.output->'request'->'params' @> params) THEN
    RETURN hit.output || jsonb_build_object('status', 'done', 'asOf', hit.as_of, 'subject', subj);
  END IF;

  norm := jsonb_build_object('scenario', scen, 'params', params, 'severity', sev);
  subj := 'custom:' || md5(norm::text);
  SELECT output, as_of INTO hit FROM model_output
   WHERE model = 'stress' AND subject_type = 'scenario' AND subject_id = subj AND as_of >= today
   ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1;
  IF FOUND THEN
    RETURN hit.output || jsonb_build_object('status', 'done', 'asOf', hit.as_of, 'subject', subj);
  END IF;

  SELECT id, status INTO job FROM procrastinate_jobs
   WHERE queueing_lock = 'stress:' || subj AND status IN ('todo', 'doing')
   ORDER BY id DESC LIMIT 1;
  IF FOUND THEN
    RETURN jsonb_build_object('status', CASE WHEN job.status = 'doing' THEN 'running' ELSE 'queued' END,
                              'jobId', job.id, 'subject', subj);
  END IF;
  BEGIN
    SELECT (procrastinate_defer_jobs_v1(ARRAY[ROW(
              'models', 'models.stress_custom', 0, NULL, 'stress:' || subj,
              jsonb_build_object('request', norm, 'key', subj), NULL
            )::procrastinate_job_to_defer_v1]))[1] INTO new_id;
  EXCEPTION WHEN unique_violation THEN  -- another caller queued it a moment ago
    SELECT id INTO new_id FROM procrastinate_jobs
     WHERE queueing_lock = 'stress:' || subj AND status = 'todo' ORDER BY id DESC LIMIT 1;
  END;
  RETURN jsonb_build_object('status', 'queued', 'jobId', new_id, 'subject', subj);
END;
$$;
