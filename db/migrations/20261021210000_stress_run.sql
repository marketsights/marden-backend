-- migrate:up
-- stress_run(request jsonb) -> jsonb: the stress model's entry point for POST /api/stress/run
-- (docs/ARCHITECTURE.md "Read without a read model"; marden.models.stress).
--
-- request = StressRequest {scenario, params, severity} or the contagion form {scenario: 'contagion',
-- params: {company, payPct}, severity}.
--
-- 1. Standard request (the saved scenarios the nightly run computes): the subject is
--    '<scenario>:<severity>' ('contagion:<company>:<severity>' for contagion). When the latest stored output
--    of model 'stress' for that subject was computed with these parameters (request params empty, or
--    contained in the stored output's request.params), it is returned at once:
--      <StressResult | ContagionResult> || {status: 'done', asOf, subject, request, missing, basis}
-- 2. Any other parameters (a custom run): the subject is 'custom:' || md5 of the normalized request. A
--    result stored for it today (US Eastern) is returned as in 1. Otherwise one procrastinate job
--    'models.stress_custom' (queue 'models', queueing lock 'stress:<subject>', so concurrent callers share
--    one job) is queued and the function returns {status: 'queued' | 'running', jobId, subject}. The worker
--    writes the result under that subject; the caller calls stress_run again with the same request until
--    the status is 'done' (the API polls for a bounded time, then answers 202 with the jobId).
-- 3. A request this function cannot read returns {status: 'error', error}.
--
-- SECURITY DEFINER so the API role needs only EXECUTE (it never writes model_output or the queue itself).
CREATE FUNCTION stress_run(request jsonb) RETURNS jsonb
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

-- migrate:down
DROP FUNCTION stress_run(jsonb);
