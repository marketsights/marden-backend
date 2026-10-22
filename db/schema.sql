\restrict dbmate

-- Dumped from database version 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)
-- Dumped by pg_dump version 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;


--
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';


--
-- Name: pgcrypto; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;


--
-- Name: EXTENSION pgcrypto; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pgcrypto IS 'cryptographic functions';


--
-- Name: postgis; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS postgis WITH SCHEMA public;


--
-- Name: EXTENSION postgis; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION postgis IS 'PostGIS geometry and geography spatial types and functions';


--
-- Name: procrastinate_job_event_type; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.procrastinate_job_event_type AS ENUM (
    'deferred',
    'started',
    'deferred_for_retry',
    'failed',
    'succeeded',
    'cancelled',
    'abort_requested',
    'aborted',
    'scheduled',
    'retried'
);


--
-- Name: procrastinate_job_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.procrastinate_job_status AS ENUM (
    'todo',
    'doing',
    'succeeded',
    'failed',
    'cancelled',
    'aborting',
    'aborted'
);


--
-- Name: procrastinate_job_to_defer_v1; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.procrastinate_job_to_defer_v1 AS (
	queue_name character varying,
	task_name character varying,
	priority integer,
	lock text,
	queueing_lock text,
	args jsonb,
	scheduled_at timestamp with time zone
);


--
-- Name: agent_notify(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.agent_notify() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  PERFORM pg_notify('marden_agents', TG_TABLE_NAME || ':' || NEW.id::text);
  RETURN NEW;
END $$;


--
-- Name: audit_log_immutable(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.audit_log_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only';
END $$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: fact; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fact (
    id bigint NOT NULL,
    subject_type text NOT NULL,
    subject_id text NOT NULL,
    field text NOT NULL,
    value jsonb,
    unit text,
    tag character(1) NOT NULL,
    source_name text NOT NULL,
    source_url text,
    document_id bigint,
    quote text,
    quote_loc text,
    valid_from date NOT NULL,
    valid_to date,
    learned_at timestamp with time zone DEFAULT now() NOT NULL,
    status text DEFAULT 'candidate'::text NOT NULL,
    method_version text,
    producer text NOT NULL,
    confidence real,
    verifier text,
    material boolean DEFAULT false NOT NULL,
    dedupe_key text NOT NULL,
    CONSTRAINT fact_check CHECK (((tag <> 'M'::bpchar) OR (method_version IS NOT NULL))),
    CONSTRAINT fact_status_check CHECK ((status = ANY (ARRAY['candidate'::text, 'published'::text, 'rejected'::text, 'superseded'::text]))),
    CONSTRAINT fact_tag_check CHECK ((tag = ANY (ARRAY['F'::bpchar, 'R'::bpchar, 'M'::bpchar]))),
    CONSTRAINT fact_verifier_check CHECK ((verifier = ANY (ARRAY['agree'::text, 'disagree'::text, 'unsupported'::text])))
);


--
-- Name: fact_asof(date); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.fact_asof(as_of date) RETURNS SETOF public.fact
    LANGUAGE sql STABLE
    AS $$
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


--
-- Name: procrastinate_cancel_job_v1(bigint, boolean, boolean); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.procrastinate_cancel_job_v1(job_id bigint, abort boolean, delete_job boolean) RETURNS bigint
    LANGUAGE plpgsql
    AS $$
DECLARE
    _job_id bigint;
BEGIN
    IF delete_job THEN
        DELETE FROM procrastinate_jobs
        WHERE id = job_id AND status = 'todo'
        RETURNING id INTO _job_id;
    END IF;
    IF _job_id IS NULL THEN
        IF abort THEN
            UPDATE procrastinate_jobs
            SET abort_requested = true,
                status = CASE status
                    WHEN 'todo' THEN 'cancelled'::procrastinate_job_status ELSE status
                END
            WHERE id = job_id AND status IN ('todo', 'doing')
            RETURNING id INTO _job_id;
        ELSE
            UPDATE procrastinate_jobs
            SET status = 'cancelled'::procrastinate_job_status
            WHERE id = job_id AND status = 'todo'
            RETURNING id INTO _job_id;
        END IF;
    END IF;
    RETURN _job_id;
END;
$$;


--
-- Name: procrastinate_defer_jobs_v1(public.procrastinate_job_to_defer_v1[]); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.procrastinate_defer_jobs_v1(jobs public.procrastinate_job_to_defer_v1[]) RETURNS bigint[]
    LANGUAGE plpgsql
    AS $$
DECLARE
    job_ids bigint[];
BEGIN
    WITH inserted_jobs AS (
        INSERT INTO procrastinate_jobs (queue_name, task_name, priority, lock, queueing_lock, args, scheduled_at)
        SELECT (job).queue_name,
               (job).task_name,
               (job).priority,
               (job).lock,
               (job).queueing_lock,
               (job).args,
               (job).scheduled_at
        FROM unnest(jobs) AS job
        RETURNING id
    )
    SELECT array_agg(id) FROM inserted_jobs INTO job_ids;

    RETURN job_ids;
END;
$$;


--
-- Name: procrastinate_defer_periodic_job_v2(character varying, character varying, character varying, character varying, integer, character varying, bigint, jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.procrastinate_defer_periodic_job_v2(_queue_name character varying, _lock character varying, _queueing_lock character varying, _task_name character varying, _priority integer, _periodic_id character varying, _defer_timestamp bigint, _args jsonb) RETURNS bigint
    LANGUAGE plpgsql
    AS $$
DECLARE
	_job_id bigint;
	_defer_id bigint;
BEGIN
    INSERT
        INTO procrastinate_periodic_defers (task_name, periodic_id, defer_timestamp)
        VALUES (_task_name, _periodic_id, _defer_timestamp)
        ON CONFLICT DO NOTHING
        RETURNING id into _defer_id;

    IF _defer_id IS NULL THEN
        RETURN NULL;
    END IF;

    UPDATE procrastinate_periodic_defers
        SET job_id = (
            SELECT COALESCE((
                SELECT unnest(procrastinate_defer_jobs_v1(
                    ARRAY[
                        ROW(
                            _queue_name,
                            _task_name,
                            _priority,
                            _lock,
                            _queueing_lock,
                            _args,
                            NULL::timestamptz
                        )
                    ]::procrastinate_job_to_defer_v1[]
                ))
            ), NULL)
        )
        WHERE id = _defer_id
        RETURNING job_id INTO _job_id;

    DELETE
        FROM procrastinate_periodic_defers
        USING (
            SELECT id
            FROM procrastinate_periodic_defers
            WHERE procrastinate_periodic_defers.task_name = _task_name
            AND procrastinate_periodic_defers.periodic_id = _periodic_id
            AND procrastinate_periodic_defers.defer_timestamp < _defer_timestamp
            ORDER BY id
            FOR UPDATE
        ) to_delete
        WHERE procrastinate_periodic_defers.id = to_delete.id;

    RETURN _job_id;
END;
$$;


--
-- Name: procrastinate_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.procrastinate_jobs (
    id bigint NOT NULL,
    queue_name character varying(128) NOT NULL,
    task_name character varying(128) NOT NULL,
    priority integer DEFAULT 0 NOT NULL,
    lock text,
    queueing_lock text,
    args jsonb DEFAULT '{}'::jsonb NOT NULL,
    status public.procrastinate_job_status DEFAULT 'todo'::public.procrastinate_job_status NOT NULL,
    scheduled_at timestamp with time zone,
    attempts integer DEFAULT 0 NOT NULL,
    abort_requested boolean DEFAULT false NOT NULL,
    worker_id bigint,
    CONSTRAINT check_not_todo_abort_requested CHECK ((NOT ((status = 'todo'::public.procrastinate_job_status) AND (abort_requested = true))))
);


--
-- Name: procrastinate_fetch_job_v2(character varying[], bigint); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.procrastinate_fetch_job_v2(target_queue_names character varying[], p_worker_id bigint) RETURNS public.procrastinate_jobs
    LANGUAGE plpgsql
    AS $$
DECLARE
	found_jobs procrastinate_jobs;
BEGIN
    WITH candidate AS (
        SELECT jobs.*
            FROM procrastinate_jobs AS jobs
            WHERE
                -- reject the job if its lock has earlier or higher priority jobs
                NOT EXISTS (
                    SELECT 1
                        FROM procrastinate_jobs AS other_jobs
                        WHERE
                            jobs.lock IS NOT NULL
                            AND other_jobs.lock = jobs.lock
                            AND (
                                -- job with same lock is already running
                                other_jobs.status = 'doing'
                                OR
                                -- job with same lock is waiting and has higher priority (or same priority but was queued first)
                                (
                                    other_jobs.status = 'todo'
                                    AND (
                                        other_jobs.priority > jobs.priority
                                        OR (
                                        other_jobs.priority = jobs.priority
                                        AND other_jobs.id < jobs.id
                                        )
                                    )
                                )
                            )
                )
                AND jobs.status = 'todo'
                AND (target_queue_names IS NULL OR jobs.queue_name = ANY( target_queue_names ))
                AND (jobs.scheduled_at IS NULL OR jobs.scheduled_at <= now())
            ORDER BY jobs.priority DESC, jobs.id ASC LIMIT 1
            FOR UPDATE OF jobs SKIP LOCKED
    )
    UPDATE procrastinate_jobs
        SET status = 'doing', worker_id = p_worker_id
        FROM candidate
        WHERE procrastinate_jobs.id = candidate.id
        RETURNING procrastinate_jobs.* INTO found_jobs;

 RETURN found_jobs;
END;
$$;


--
-- Name: procrastinate_finish_job_v1(bigint, public.procrastinate_job_status, boolean); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.procrastinate_finish_job_v1(job_id bigint, end_status public.procrastinate_job_status, delete_job boolean) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    _job_id bigint;
BEGIN
    IF end_status NOT IN ('succeeded', 'failed', 'aborted') THEN
        RAISE 'End status should be either "succeeded", "failed" or "aborted" (job id: %)', job_id;
    END IF;
    IF delete_job THEN
        DELETE FROM procrastinate_jobs
        WHERE id = job_id AND status IN ('todo', 'doing')
        RETURNING id INTO _job_id;
    ELSE
        UPDATE procrastinate_jobs
        SET status = end_status,
            abort_requested = false,
            attempts = CASE status
                WHEN 'doing' THEN attempts + 1 ELSE attempts
            END
        WHERE id = job_id AND status IN ('todo', 'doing')
        RETURNING id INTO _job_id;
    END IF;
    IF _job_id IS NULL THEN
        RAISE 'Job was not found or not in "doing" or "todo" status (job id: %)', job_id;
    END IF;
END;
$$;


--
-- Name: procrastinate_notify_queue_abort_job_v1(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.procrastinate_notify_queue_abort_job_v1() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    payload TEXT;
BEGIN
    SELECT json_build_object('type', 'abort_job_requested', 'job_id', NEW.id)::text INTO payload;
	PERFORM pg_notify('procrastinate_queue_v1#' || NEW.queue_name, payload);
	PERFORM pg_notify('procrastinate_any_queue_v1', payload);
	RETURN NEW;
END;
$$;


--
-- Name: procrastinate_notify_queue_job_inserted_v1(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.procrastinate_notify_queue_job_inserted_v1() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    payload TEXT;
BEGIN
    SELECT json_build_object('type', 'job_inserted', 'job_id', NEW.id)::text INTO payload;
	PERFORM pg_notify('procrastinate_queue_v1#' || NEW.queue_name, payload);
	PERFORM pg_notify('procrastinate_any_queue_v1', payload);
	RETURN NEW;
END;
$$;


--
-- Name: procrastinate_prune_stalled_workers_v1(double precision); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.procrastinate_prune_stalled_workers_v1(seconds_since_heartbeat double precision) RETURNS TABLE(worker_id bigint)
    LANGUAGE plpgsql
    AS $$
BEGIN
    RETURN QUERY
    DELETE FROM procrastinate_workers
    WHERE last_heartbeat < NOW() - (seconds_since_heartbeat || 'SECOND')::INTERVAL
    RETURNING procrastinate_workers.id;
END;
$$;


--
-- Name: procrastinate_register_worker_v1(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.procrastinate_register_worker_v1() RETURNS TABLE(worker_id bigint)
    LANGUAGE plpgsql
    AS $$
BEGIN
    RETURN QUERY
    INSERT INTO procrastinate_workers DEFAULT VALUES
    RETURNING procrastinate_workers.id;
END;
$$;


--
-- Name: procrastinate_retry_job_v1(bigint, timestamp with time zone, integer, character varying, character varying); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.procrastinate_retry_job_v1(job_id bigint, retry_at timestamp with time zone, new_priority integer, new_queue_name character varying, new_lock character varying) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    _job_id bigint;
    _abort_requested boolean;
BEGIN
    SELECT abort_requested FROM procrastinate_jobs
    WHERE id = job_id AND status = 'doing'
    FOR UPDATE
    INTO _abort_requested;
    IF _abort_requested THEN
        UPDATE procrastinate_jobs
        SET status = 'failed'::procrastinate_job_status
        WHERE id = job_id AND status = 'doing'
        RETURNING id INTO _job_id;
    ELSE
        UPDATE procrastinate_jobs
        SET status = 'todo'::procrastinate_job_status,
            attempts = attempts + 1,
            scheduled_at = retry_at,
            priority = COALESCE(new_priority, priority),
            queue_name = COALESCE(new_queue_name, queue_name),
            lock = COALESCE(new_lock, lock)
        WHERE id = job_id AND status = 'doing'
        RETURNING id INTO _job_id;
    END IF;

    IF _job_id IS NULL THEN
        RAISE 'Job was not found or not in "doing" status (job id: %)', job_id;
    END IF;
END;
$$;


--
-- Name: procrastinate_retry_job_v2(bigint, timestamp with time zone, integer, character varying, character varying); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.procrastinate_retry_job_v2(job_id bigint, retry_at timestamp with time zone, new_priority integer, new_queue_name character varying, new_lock character varying) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    _job_id bigint;
    _abort_requested boolean;
    _current_status procrastinate_job_status;
BEGIN
    SELECT status, abort_requested FROM procrastinate_jobs
    WHERE id = job_id AND status IN ('doing', 'failed')
    FOR UPDATE
    INTO _current_status, _abort_requested;
    IF _current_status = 'doing' AND _abort_requested THEN
        UPDATE procrastinate_jobs
        SET status = 'failed'::procrastinate_job_status
        WHERE id = job_id AND status = 'doing'
        RETURNING id INTO _job_id;
    ELSE
        UPDATE procrastinate_jobs
        SET status = 'todo'::procrastinate_job_status,
            attempts = attempts + 1,
            scheduled_at = retry_at,
            priority = COALESCE(new_priority, priority),
            queue_name = COALESCE(new_queue_name, queue_name),
            lock = COALESCE(new_lock, lock)
        WHERE id = job_id AND status IN ('doing', 'failed')
        RETURNING id INTO _job_id;
    END IF;

    IF _job_id IS NULL THEN
        RAISE 'Job was not found or has an invalid status to retry (job id: %)', job_id;
    END IF;

END;
$$;


--
-- Name: procrastinate_trigger_abort_requested_events_procedure_v1(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.procrastinate_trigger_abort_requested_events_procedure_v1() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    INSERT INTO procrastinate_events(job_id, type)
        VALUES (NEW.id, 'abort_requested'::procrastinate_job_event_type);
    RETURN NEW;
END;
$$;


--
-- Name: procrastinate_trigger_function_scheduled_events_v1(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.procrastinate_trigger_function_scheduled_events_v1() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    INSERT INTO procrastinate_events(job_id, type, at)
        VALUES (NEW.id, 'scheduled'::procrastinate_job_event_type, NEW.scheduled_at);

	RETURN NEW;
END;
$$;


--
-- Name: procrastinate_trigger_function_status_events_insert_v1(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.procrastinate_trigger_function_status_events_insert_v1() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    INSERT INTO procrastinate_events(job_id, type)
        VALUES (NEW.id, 'deferred'::procrastinate_job_event_type);
	RETURN NEW;
END;
$$;


--
-- Name: procrastinate_trigger_function_status_events_update_v1(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.procrastinate_trigger_function_status_events_update_v1() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    WITH t AS (
        SELECT CASE
            WHEN OLD.status = 'todo'::procrastinate_job_status
                AND NEW.status = 'doing'::procrastinate_job_status
                THEN 'started'::procrastinate_job_event_type
            WHEN OLD.status = 'doing'::procrastinate_job_status
                AND NEW.status = 'todo'::procrastinate_job_status
                THEN 'deferred_for_retry'::procrastinate_job_event_type
            WHEN OLD.status = 'doing'::procrastinate_job_status
                AND NEW.status = 'failed'::procrastinate_job_status
                THEN 'failed'::procrastinate_job_event_type
            WHEN OLD.status = 'doing'::procrastinate_job_status
                AND NEW.status = 'succeeded'::procrastinate_job_status
                THEN 'succeeded'::procrastinate_job_event_type
            WHEN OLD.status = 'todo'::procrastinate_job_status
                AND (
                    NEW.status = 'cancelled'::procrastinate_job_status
                    OR NEW.status = 'failed'::procrastinate_job_status
                    OR NEW.status = 'succeeded'::procrastinate_job_status
                )
                THEN 'cancelled'::procrastinate_job_event_type
            WHEN OLD.status = 'doing'::procrastinate_job_status
                AND NEW.status = 'aborted'::procrastinate_job_status
                THEN 'aborted'::procrastinate_job_event_type
            WHEN OLD.status = 'failed'::procrastinate_job_status
                AND NEW.status = 'todo'::procrastinate_job_status
                THEN 'retried'::procrastinate_job_event_type
            ELSE NULL
        END as event_type
    )
    INSERT INTO procrastinate_events(job_id, type)
        SELECT NEW.id, t.event_type
        FROM t
        WHERE t.event_type IS NOT NULL;
	RETURN NEW;
END;
$$;


--
-- Name: procrastinate_unlink_periodic_defers_v1(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.procrastinate_unlink_periodic_defers_v1() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    UPDATE procrastinate_periodic_defers
    SET job_id = NULL
    WHERE job_id = OLD.id;
    RETURN OLD;
END;
$$;


--
-- Name: procrastinate_unregister_worker_v1(bigint); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.procrastinate_unregister_worker_v1(worker_id bigint) RETURNS void
    LANGUAGE plpgsql
    AS $$
BEGIN
    DELETE FROM procrastinate_workers
    WHERE id = worker_id;
END;
$$;


--
-- Name: procrastinate_update_heartbeat_v1(bigint); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.procrastinate_update_heartbeat_v1(worker_id bigint) RETURNS void
    LANGUAGE plpgsql
    AS $$
BEGIN
    UPDATE procrastinate_workers
    SET last_heartbeat = NOW()
    WHERE id = worker_id;
END;
$$;


--
-- Name: stream_event_notify(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.stream_event_notify() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  PERFORM pg_notify('marden_stream', NEW.id::text);
  RETURN NEW;
END $$;


--
-- Name: stress_run(jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.stress_run(request jsonb) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
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


--
-- Name: agent_batch; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_batch (
    id text NOT NULL,
    stage text NOT NULL,
    request_count integer NOT NULL,
    custom_ids jsonb NOT NULL,
    status text DEFAULT 'in_progress'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    ended_at timestamp with time zone,
    applied_at timestamp with time zone,
    error text,
    CONSTRAINT agent_batch_status_check CHECK ((status = ANY (ARRAY['in_progress'::text, 'ended'::text, 'applied'::text, 'failed'::text])))
);


--
-- Name: agent_call; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_call (
    id bigint NOT NULL,
    request_hash text NOT NULL,
    agent text NOT NULL,
    backend text NOT NULL,
    model text NOT NULL,
    prompt_version text NOT NULL,
    job_id bigint,
    document_sha256 text,
    section text,
    response jsonb,
    stop_reason text,
    input_tokens integer DEFAULT 0 NOT NULL,
    output_tokens integer DEFAULT 0 NOT NULL,
    cache_read_tokens integer DEFAULT 0 NOT NULL,
    cache_write_tokens integer DEFAULT 0 NOT NULL,
    cost_usd numeric,
    batch_id text,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT agent_call_backend_check CHECK ((backend = ANY (ARRAY['api'::text, 'cli'::text, 'batch'::text])))
);


--
-- Name: agent_call_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_call_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_call_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_call_id_seq OWNED BY public.agent_call.id;


--
-- Name: agent_candidate; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_candidate (
    id bigint NOT NULL,
    job_id bigint NOT NULL,
    document_id bigint NOT NULL,
    area text NOT NULL,
    section text,
    subject_kind text NOT NULL,
    subject_mention text NOT NULL,
    subject_type text,
    subject_id text,
    link_id bigint,
    field text NOT NULL,
    value jsonb NOT NULL,
    unit text,
    as_of date,
    quote text NOT NULL,
    quote_loc text,
    page integer,
    confidence real,
    checks jsonb DEFAULT '{}'::jsonb NOT NULL,
    verdict text,
    verdict_reason text,
    material boolean DEFAULT false NOT NULL,
    material_codes text[] DEFAULT '{}'::text[] NOT NULL,
    fact_id bigint,
    outcome text DEFAULT 'pending'::text NOT NULL,
    outcome_reason text,
    dedupe_key text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT agent_candidate_outcome_check CHECK ((outcome = ANY (ARRAY['pending'::text, 'published'::text, 'review'::text, 'rejected'::text, 'duplicate'::text]))),
    CONSTRAINT agent_candidate_verdict_check CHECK ((verdict = ANY (ARRAY['agree'::text, 'disagree'::text, 'unsupported'::text])))
);


--
-- Name: agent_candidate_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_candidate_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_candidate_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_candidate_id_seq OWNED BY public.agent_candidate.id;


--
-- Name: agent_decision; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_decision (
    id bigint NOT NULL,
    review_item_id bigint NOT NULL,
    action text NOT NULL,
    decided_by text NOT NULL,
    reason text,
    edit jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    applied_at timestamp with time zone,
    result jsonb,
    error text,
    CONSTRAINT agent_decision_action_check CHECK ((action = ANY (ARRAY['approve'::text, 'reject'::text, 'edit'::text])))
);


--
-- Name: agent_decision_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_decision_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_decision_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_decision_id_seq OWNED BY public.agent_decision.id;


--
-- Name: agent_file; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_file (
    sha256 text NOT NULL,
    file_id text NOT NULL,
    bytes bigint NOT NULL,
    uploaded_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_inbox; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_inbox (
    key text NOT NULL,
    etag text NOT NULL,
    document_id bigint,
    seen_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_job; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_job (
    id bigint NOT NULL,
    document_id bigint NOT NULL,
    sha256 text NOT NULL,
    source_id text NOT NULL,
    prompt_version text NOT NULL,
    state text DEFAULT 'queued'::text NOT NULL,
    stage text DEFAULT 'queued'::text NOT NULL,
    mode text DEFAULT 'live'::text NOT NULL,
    priority integer DEFAULT 50 NOT NULL,
    priority_reason text,
    attempts integer DEFAULT 0 NOT NULL,
    route jsonb,
    areas text[] DEFAULT '{}'::text[] NOT NULL,
    links jsonb DEFAULT '{}'::jsonb NOT NULL,
    stats jsonb DEFAULT '{}'::jsonb NOT NULL,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    CONSTRAINT agent_job_mode_check CHECK ((mode = ANY (ARRAY['live'::text, 'batch'::text]))),
    CONSTRAINT agent_job_stage_check CHECK ((stage = ANY (ARRAY['queued'::text, 'routed'::text, 'extracted'::text, 'verified'::text, 'linked'::text, 'published'::text]))),
    CONSTRAINT agent_job_state_check CHECK ((state = ANY (ARRAY['queued'::text, 'dispatched'::text, 'running'::text, 'published'::text, 'needs_review'::text, 'failed'::text, 'skipped'::text, 'budget'::text])))
);


--
-- Name: agent_job_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_job_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_job_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_job_id_seq OWNED BY public.agent_job.id;


--
-- Name: agent_link; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_link (
    id bigint NOT NULL,
    document_id bigint,
    job_id bigint,
    mention text NOT NULL,
    mention_key text NOT NULL,
    subject_type text NOT NULL,
    subject_id text NOT NULL,
    relation text DEFAULT 'is'::text NOT NULL,
    related_type text,
    related_id text,
    evidence text,
    method text NOT NULL,
    score real,
    status text DEFAULT 'proposed'::text NOT NULL,
    decided_by text,
    decided_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT agent_link_method_check CHECK ((method = ANY (ARRAY['identifier'::text, 'upload'::text, 'confirmed'::text, 'name'::text, 'fuzzy'::text, 'agent'::text]))),
    CONSTRAINT agent_link_status_check CHECK ((status = ANY (ARRAY['proposed'::text, 'accepted'::text, 'rejected'::text])))
);


--
-- Name: agent_link_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_link_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_link_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_link_id_seq OWNED BY public.agent_link.id;


--
-- Name: agent_ops_run; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_ops_run (
    id bigint NOT NULL,
    role text NOT NULL,
    trigger text DEFAULT 'manual'::text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    status text DEFAULT 'running'::text NOT NULL,
    model_status text,
    detail text,
    report jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT agent_ops_run_role_check CHECK ((role = ANY (ARRAY['researcher'::text, 'watchdog'::text, 'note_writer'::text]))),
    CONSTRAINT agent_ops_run_status_check CHECK ((status = ANY (ARRAY['running'::text, 'ok'::text, 'partial'::text, 'failed'::text])))
);


--
-- Name: agent_ops_run_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_ops_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_ops_run_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_ops_run_id_seq OWNED BY public.agent_ops_run.id;


--
-- Name: agent_status; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_status (
    id boolean DEFAULT true NOT NULL,
    backend text NOT NULL,
    detail text,
    models jsonb DEFAULT '{}'::jsonb NOT NULL,
    checked_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT agent_status_backend_check CHECK ((backend = ANY (ARRAY['api'::text, 'cli'::text, 'not_configured'::text]))),
    CONSTRAINT agent_status_id_check CHECK (id)
);


--
-- Name: agent_upload; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_upload (
    id bigint NOT NULL,
    filename text NOT NULL,
    content_type text NOT NULL,
    kind text NOT NULL,
    size bigint NOT NULL,
    sha256 text NOT NULL,
    data bytea,
    uploaded_by text NOT NULL,
    org_id text,
    links jsonb DEFAULT '{}'::jsonb NOT NULL,
    filed boolean DEFAULT false NOT NULL,
    source_url text,
    title text,
    doc_date date,
    status text DEFAULT 'pending'::text NOT NULL,
    document_id bigint,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    stored_at timestamp with time zone,
    CONSTRAINT agent_upload_kind_check CHECK ((kind = ANY (ARRAY['pdf'::text, 'docx'::text, 'html'::text, 'txt'::text, 'xlsx'::text]))),
    CONSTRAINT agent_upload_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'stored'::text, 'failed'::text])))
);


--
-- Name: agent_upload_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_upload_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_upload_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_upload_id_seq OWNED BY public.agent_upload.id;


--
-- Name: alert; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alert (
    id bigint NOT NULL,
    org_id text NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    kind text NOT NULL,
    headline text NOT NULL,
    tag character(1) NOT NULL,
    rule_label text NOT NULL,
    rule_code text,
    ref_url text NOT NULL,
    subject_type text NOT NULL,
    subject_id text NOT NULL,
    event_id bigint,
    severity text NOT NULL,
    read_at timestamp with time zone,
    emailed_at timestamp with time zone,
    user_id text NOT NULL,
    body text NOT NULL,
    facts jsonb DEFAULT '{}'::jsonb NOT NULL,
    sources jsonb DEFAULT '[]'::jsonb NOT NULL,
    occurred_on date NOT NULL,
    trigger_key text NOT NULL,
    dedupe_key text NOT NULL,
    backfill boolean DEFAULT false NOT NULL,
    CONSTRAINT alert_severity_check CHECK ((severity = ANY (ARRAY['red'::text, 'amber'::text, 'green'::text]))),
    CONSTRAINT alert_tag_check CHECK ((tag = ANY (ARRAY['F'::bpchar, 'R'::bpchar, 'M'::bpchar])))
);


--
-- Name: alert_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.alert_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: alert_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.alert_id_seq OWNED BY public.alert.id;


--
-- Name: alert_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alert_settings (
    org_id text NOT NULL,
    user_id text NOT NULL,
    settings jsonb NOT NULL
);


--
-- Name: alert_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alert_state (
    model text NOT NULL,
    subject_type text NOT NULL,
    subject_id text NOT NULL,
    as_of date NOT NULL,
    output_hash text NOT NULL,
    state jsonb NOT NULL,
    evaluated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: alert_trigger; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alert_trigger (
    trigger_key text NOT NULL,
    kind text NOT NULL,
    subject_type text NOT NULL,
    subject_id text NOT NULL,
    occurred_on date NOT NULL,
    material boolean NOT NULL,
    codes text[] DEFAULT '{}'::text[] NOT NULL,
    alerts integer DEFAULT 0 NOT NULL,
    dry_run boolean DEFAULT false NOT NULL,
    evaluated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT alert_trigger_kind_check CHECK ((kind = ANY (ARRAY['event'::text, 'fact'::text, 'model'::text])))
);


--
-- Name: app_user; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.app_user (
    id text NOT NULL,
    email text NOT NULL,
    name text,
    is_staff boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_log (
    id bigint NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    org_id text,
    user_id text,
    action text NOT NULL,
    target text,
    ip text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: audit_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.audit_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: audit_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.audit_log_id_seq OWNED BY public.audit_log.id;


--
-- Name: backup_run; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.backup_run (
    id bigint NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    status text NOT NULL,
    database text,
    bucket text,
    object_key text,
    bytes bigint,
    sha256 text,
    pg_dump_version text,
    detail text,
    CONSTRAINT backup_run_bytes_check CHECK (((bytes IS NULL) OR (bytes >= 0))),
    CONSTRAINT backup_run_check CHECK (((status <> 'ok'::text) OR ((object_key IS NOT NULL) AND (bytes IS NOT NULL) AND (sha256 IS NOT NULL) AND (finished_at IS NOT NULL)))),
    CONSTRAINT backup_run_sha256_check CHECK (((sha256 IS NULL) OR (sha256 ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT backup_run_status_check CHECK ((status = ANY (ARRAY['running'::text, 'ok'::text, 'failed'::text, 'not_configured'::text])))
);


--
-- Name: backup_run_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.backup_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: backup_run_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.backup_run_id_seq OWNED BY public.backup_run.id;


--
-- Name: building; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.building (
    id text NOT NULL,
    campus_id text NOT NULL,
    label text NOT NULL,
    footprint public.geometry(Polygon,4326),
    parcel_ids text[] DEFAULT '{}'::text[] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: campus; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.campus (
    id text NOT NULL,
    name text NOT NULL,
    aliases text[] DEFAULT '{}'::text[] NOT NULL,
    county text,
    state text,
    geom public.geometry(Point,4326),
    parcel_ids text[] DEFAULT '{}'::text[] NOT NULL,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: chain_party; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chain_party (
    site_slug text NOT NULL,
    role text NOT NULL,
    entity_id text,
    label text NOT NULL,
    CONSTRAINT chain_party_role_check CHECK ((role = ANY (ARRAY['end_user'::text, 'tenant'::text, 'developer'::text, 'lenders'::text])))
);


--
-- Name: document; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document (
    id bigint NOT NULL,
    source_id text NOT NULL,
    url text NOT NULL,
    sha256 text NOT NULL,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL,
    content_type text,
    bytes bigint,
    storage_key text NOT NULL,
    title text,
    doc_type text,
    doc_date date,
    external_id text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    extract_state text DEFAULT 'none'::text NOT NULL,
    CONSTRAINT document_extract_state_check CHECK ((extract_state = ANY (ARRAY['none'::text, 'queued'::text, 'done'::text, 'failed'::text, 'skipped'::text])))
);


--
-- Name: document_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.document_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: document_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.document_id_seq OWNED BY public.document.id;


--
-- Name: email_outbox; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.email_outbox (
    id bigint NOT NULL,
    org_id text NOT NULL,
    user_id text NOT NULL,
    kind text NOT NULL,
    idempotency_key text NOT NULL,
    to_email text NOT NULL,
    subject text NOT NULL,
    html text,
    text_body text,
    headers jsonb DEFAULT '{}'::jsonb NOT NULL,
    alert_ids bigint[] DEFAULT '{}'::bigint[] NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    provider text,
    provider_message_id text,
    attempts integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    sent_at timestamp with time zone,
    CONSTRAINT email_outbox_kind_check CHECK ((kind = ANY (ARRAY['instant'::text, 'daily_digest'::text, 'weekly_digest'::text]))),
    CONSTRAINT email_outbox_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'sent'::text, 'failed'::text, 'not_configured'::text, 'cancelled'::text])))
);


--
-- Name: email_outbox_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.email_outbox_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: email_outbox_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.email_outbox_id_seq OWNED BY public.email_outbox.id;


--
-- Name: email_provider_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.email_provider_state (
    id text NOT NULL,
    provider text,
    configured boolean NOT NULL,
    from_email text,
    reason text,
    checked_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: entity; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.entity (
    id text NOT NULL,
    name text NOT NULL,
    kind text NOT NULL,
    parent_id text,
    aliases text[] DEFAULT '{}'::text[] NOT NULL,
    cik text,
    lei text,
    ticker text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: event; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.event (
    id bigint NOT NULL,
    subject_type text NOT NULL,
    subject_id text NOT NULL,
    event_date date NOT NULL,
    kind text NOT NULL,
    title text NOT NULL,
    severity text,
    rule_code text,
    material boolean DEFAULT false NOT NULL,
    tag character(1) NOT NULL,
    source_name text NOT NULL,
    source_url text,
    document_id bigint,
    fact_id bigint,
    status text DEFAULT 'candidate'::text NOT NULL,
    learned_at timestamp with time zone DEFAULT now() NOT NULL,
    dedupe_key text NOT NULL,
    CONSTRAINT event_severity_check CHECK ((severity = ANY (ARRAY['red'::text, 'amber'::text, 'green'::text, 'none'::text]))),
    CONSTRAINT event_status_check CHECK ((status = ANY (ARRAY['candidate'::text, 'published'::text, 'rejected'::text]))),
    CONSTRAINT event_tag_check CHECK ((tag = ANY (ARRAY['F'::bpchar, 'R'::bpchar, 'M'::bpchar])))
);


--
-- Name: event_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: event_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.event_id_seq OWNED BY public.event.id;


--
-- Name: fact_current; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.fact_current AS
 SELECT DISTINCT ON (subject_type, subject_id, field) id,
    subject_type,
    subject_id,
    field,
    value,
    unit,
    tag,
    source_name,
    source_url,
    document_id,
    quote,
    quote_loc,
    valid_from,
    valid_to,
    learned_at,
    status,
    method_version,
    producer,
    confidence,
    verifier,
    material,
    dedupe_key
   FROM public.fact
  WHERE ((status = 'published'::text) AND ((valid_to IS NULL) OR (valid_to > ((now() AT TIME ZONE 'America/New_York'::text))::date)))
  ORDER BY subject_type, subject_id, field, (tag = 'M'::bpchar), valid_from DESC,
        CASE tag
            WHEN 'F'::bpchar THEN 0
            WHEN 'R'::bpchar THEN 1
            ELSE 2
        END, learned_at DESC, id DESC;


--
-- Name: fact_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.fact_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: fact_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.fact_id_seq OWNED BY public.fact.id;


--
-- Name: grid_region; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.grid_region (
    id text NOT NULL,
    kind text NOT NULL,
    name text NOT NULL,
    country text,
    parent_id text,
    geom public.geometry(MultiPolygon,4326),
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT grid_region_check CHECK ((parent_id IS DISTINCT FROM id)),
    CONSTRAINT grid_region_kind_check CHECK ((kind = ANY (ARRAY['interconnection'::text, 'iso_rto'::text, 'balancing_authority'::text, 'utility'::text, 'tso'::text, 'reliability_region'::text])))
);


--
-- Name: holding; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.holding (
    id bigint NOT NULL,
    fund_entity_id text NOT NULL,
    instrument_id text,
    issuer_name text,
    title text,
    cusip text,
    isin text,
    period_end date NOT NULL,
    filing_type text NOT NULL,
    filing_url text NOT NULL,
    accession text NOT NULL,
    filed_at date,
    par numeric,
    value_usd numeric,
    mark_price numeric,
    pct_nav numeric,
    fund_net_assets numeric,
    non_accrual boolean,
    document_id bigint,
    line_key text NOT NULL,
    learned_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: holding_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.holding_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: holding_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.holding_id_seq OWNED BY public.holding.id;


--
-- Name: home_layout; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.home_layout (
    org_id text NOT NULL,
    user_id text NOT NULL,
    layout jsonb NOT NULL
);


--
-- Name: instrument; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.instrument (
    id text NOT NULL,
    name text NOT NULL,
    kind text NOT NULL,
    issuer_entity_id text,
    issuer_name text NOT NULL,
    site_slug text,
    cusip text,
    isin text,
    in_v1 boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: member_product_role; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.member_product_role (
    org_id text NOT NULL,
    user_id text NOT NULL,
    product_id text NOT NULL,
    role text NOT NULL,
    granted_by text,
    granted_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT member_product_role_role_check CHECK ((role = ANY (ARRAY['owner'::text, 'admin'::text, 'member'::text, 'viewer'::text])))
);


--
-- Name: membership; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.membership (
    org_id text NOT NULL,
    user_id text NOT NULL,
    role text NOT NULL,
    role_source text DEFAULT 'system'::text NOT NULL,
    last_active_at timestamp with time zone,
    session_started_at timestamp with time zone,
    last_visit_at timestamp with time zone,
    CONSTRAINT membership_role_check CHECK ((role = ANY (ARRAY['owner'::text, 'admin'::text, 'member'::text, 'viewer'::text]))),
    CONSTRAINT membership_role_source_check CHECK ((role_source = ANY (ARRAY['clerk'::text, 'first_member'::text, 'invitation'::text, 'default'::text, 'changed'::text, 'system'::text])))
);


--
-- Name: COLUMN membership.role; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.membership.role IS 'Marden org role (owner/admin/member/viewer), owned by Marden: set on creation (first member owner, invitation role, else viewer), changed only by PATCH /api/account/members/:id. Never from Clerk.';


--
-- Name: COLUMN membership.role_source; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.membership.role_source IS 'Where role came from: clerk (mirrored from a Clerk org role before Marden owned roles), first_member, invitation, default (joined without a Marden invitation), changed (an org owner or admin), system (dev, seeds).';


--
-- Name: model_dirty; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.model_dirty (
    model text NOT NULL,
    key text NOT NULL,
    marked_at timestamp with time zone DEFAULT now() NOT NULL,
    reason text
);


--
-- Name: model_output; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.model_output (
    id bigint NOT NULL,
    run_id bigint,
    model text NOT NULL,
    version text NOT NULL,
    subject_type text NOT NULL,
    subject_id text NOT NULL,
    as_of date NOT NULL,
    output jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: model_output_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.model_output_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: model_output_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.model_output_id_seq OWNED BY public.model_output.id;


--
-- Name: model_run; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.model_run (
    id bigint NOT NULL,
    model text NOT NULL,
    version text NOT NULL,
    trigger text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    ok boolean,
    error text,
    inputs_hash text
);


--
-- Name: model_run_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.model_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: model_run_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.model_run_id_seq OWNED BY public.model_run.id;


--
-- Name: obligation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.obligation (
    id text NOT NULL,
    type text NOT NULL,
    from_entity_id text NOT NULL,
    to_entity_id text NOT NULL,
    site_slug text,
    campus_id text,
    instrument_ids text[] DEFAULT '{}'::text[] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: org; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.org (
    id text NOT NULL,
    name text NOT NULL,
    plan text DEFAULT 'trial'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT org_plan_check CHECK ((plan = ANY (ARRAY['trial'::text, 'design_partner'::text, 'paid'::text])))
);


--
-- Name: COLUMN org.plan; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.org.plan IS 'Deprecated: plans live on org_product (per product). Kept for now; nothing reads it.';


--
-- Name: org_invitation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.org_invitation (
    id text NOT NULL,
    org_id text NOT NULL,
    email text NOT NULL,
    role text NOT NULL,
    invited_by text,
    status text DEFAULT 'pending'::text NOT NULL,
    expires_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    accepted_by text,
    accepted_at timestamp with time zone,
    CONSTRAINT org_invitation_check CHECK (((status = 'accepted'::text) = (accepted_at IS NOT NULL))),
    CONSTRAINT org_invitation_email_check CHECK (((email = lower(email)) AND (email <> ''::text))),
    CONSTRAINT org_invitation_role_check CHECK ((role = ANY (ARRAY['owner'::text, 'admin'::text, 'member'::text, 'viewer'::text]))),
    CONSTRAINT org_invitation_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'accepted'::text, 'revoked'::text])))
);


--
-- Name: org_product; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.org_product (
    org_id text NOT NULL,
    product_id text NOT NULL,
    plan text NOT NULL,
    seats integer,
    status text DEFAULT 'active'::text NOT NULL,
    starts_on date NOT NULL,
    ends_on date,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT org_product_check CHECK (((ends_on IS NULL) OR (ends_on >= starts_on))),
    CONSTRAINT org_product_plan_check CHECK ((plan = ANY (ARRAY['trial'::text, 'design_partner'::text, 'paid'::text]))),
    CONSTRAINT org_product_seats_check CHECK (((seats IS NULL) OR (seats >= 0))),
    CONSTRAINT org_product_status_check CHECK ((status = ANY (ARRAY['active'::text, 'suspended'::text, 'ended'::text])))
);


--
-- Name: plant_serves; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.plant_serves (
    plant_id text NOT NULL,
    campus_id text NOT NULL,
    evidence_fact_id bigint
);


--
-- Name: power_plant; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.power_plant (
    id text NOT NULL,
    name text NOT NULL,
    county text,
    state text,
    geom public.geometry(Point,4326),
    eia_plant_id integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: power_unit; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.power_unit (
    id text NOT NULL,
    plant_id text NOT NULL,
    oem text,
    model text,
    count integer DEFAULT 1 NOT NULL,
    permit_ref text
);


--
-- Name: price_obs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.price_obs (
    id bigint NOT NULL,
    instrument_id text NOT NULL,
    obs_date date NOT NULL,
    obs_at timestamp with time zone,
    kind text NOT NULL,
    price numeric,
    yield_pct numeric,
    spread_bp numeric,
    volume numeric,
    tag character(1) NOT NULL,
    source_name text NOT NULL,
    source_url text,
    method_version text,
    document_id bigint,
    learned_at timestamp with time zone DEFAULT now() NOT NULL,
    dedupe_key text NOT NULL,
    CONSTRAINT price_obs_kind_check CHECK ((kind = ANY (ARRAY['trade'::text, 'quote'::text, 'fund_mark'::text, 'exchange'::text, 'dealer'::text, 'marden'::text, 'legacy'::text]))),
    CONSTRAINT price_obs_tag_check CHECK ((tag = ANY (ARRAY['F'::bpchar, 'R'::bpchar, 'M'::bpchar])))
);


--
-- Name: price_obs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.price_obs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: price_obs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.price_obs_id_seq OWNED BY public.price_obs.id;


--
-- Name: procrastinate_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.procrastinate_events (
    id bigint NOT NULL,
    job_id bigint NOT NULL,
    type public.procrastinate_job_event_type,
    at timestamp with time zone DEFAULT now()
);


--
-- Name: procrastinate_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.procrastinate_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: procrastinate_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.procrastinate_events_id_seq OWNED BY public.procrastinate_events.id;


--
-- Name: procrastinate_jobs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.procrastinate_jobs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: procrastinate_jobs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.procrastinate_jobs_id_seq OWNED BY public.procrastinate_jobs.id;


--
-- Name: procrastinate_periodic_defers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.procrastinate_periodic_defers (
    id bigint NOT NULL,
    task_name character varying(128) NOT NULL,
    defer_timestamp bigint,
    job_id bigint,
    periodic_id character varying(128) DEFAULT ''::character varying NOT NULL
);


--
-- Name: procrastinate_periodic_defers_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.procrastinate_periodic_defers_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: procrastinate_periodic_defers_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.procrastinate_periodic_defers_id_seq OWNED BY public.procrastinate_periodic_defers.id;


--
-- Name: procrastinate_workers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.procrastinate_workers (
    id bigint NOT NULL,
    last_heartbeat timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: procrastinate_workers_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.procrastinate_workers ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.procrastinate_workers_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: product; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.product (
    id text NOT NULL,
    name text NOT NULL,
    description text,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: queue_project; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.queue_project (
    id text NOT NULL,
    iso text NOT NULL,
    queue_id text NOT NULL,
    name text,
    state text,
    county text,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT queue_project_check CHECK ((id = ((iso || ':'::text) || queue_id)))
);


--
-- Name: read_model; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.read_model (
    view text NOT NULL,
    key text NOT NULL,
    as_of date NOT NULL,
    doc jsonb NOT NULL,
    built_at timestamp with time zone DEFAULT now() NOT NULL,
    inputs_hash text
);


--
-- Name: read_model_dirty; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.read_model_dirty (
    view text NOT NULL,
    key text NOT NULL,
    marked_at timestamp with time zone DEFAULT now() NOT NULL,
    reason text
);


--
-- Name: research_find; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.research_find (
    id bigint NOT NULL,
    search_id bigint NOT NULL,
    url text NOT NULL,
    title text,
    form text,
    filed date,
    outcome text NOT NULL,
    reason text,
    document_id bigint,
    job_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT research_find_outcome_check CHECK ((outcome = ANY (ARRAY['queued'::text, 'already_stored'::text, 'already_read'::text, 'skipped'::text, 'failed'::text])))
);


--
-- Name: research_find_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.research_find_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: research_find_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.research_find_id_seq OWNED BY public.research_find.id;


--
-- Name: research_search; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.research_search (
    id bigint NOT NULL,
    run_id bigint,
    endpoint text NOT NULL,
    query text NOT NULL,
    window_from date,
    window_to date,
    subjects jsonb DEFAULT '[]'::jsonb NOT NULL,
    gaps jsonb DEFAULT '[]'::jsonb NOT NULL,
    document_id bigint,
    hits integer,
    status text NOT NULL,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT research_search_endpoint_check CHECK ((endpoint = ANY (ARRAY['edgar_fts'::text, 'ferc_elibrary'::text]))),
    CONSTRAINT research_search_status_check CHECK ((status = ANY (ARRAY['ok'::text, 'failed'::text, 'blocked'::text])))
);


--
-- Name: research_search_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.research_search_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: research_search_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.research_search_id_seq OWNED BY public.research_search.id;


--
-- Name: review_item; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.review_item (
    id bigint NOT NULL,
    kind text NOT NULL,
    ref_type text NOT NULL,
    ref_id text NOT NULL,
    title text NOT NULL,
    reason text NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_by text,
    decided_at timestamp with time zone,
    CONSTRAINT review_item_kind_check CHECK ((kind = ANY (ARRAY['fact'::text, 'event'::text, 'link'::text, 'merge'::text, 'note'::text, 'source'::text, 'issue'::text]))),
    CONSTRAINT review_item_status_check CHECK ((status = ANY (ARRAY['open'::text, 'approved'::text, 'rejected'::text])))
);


--
-- Name: review_item_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.review_item_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: review_item_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.review_item_id_seq OWNED BY public.review_item.id;


--
-- Name: saved_view; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.saved_view (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id text NOT NULL,
    user_id text NOT NULL,
    screen text NOT NULL,
    name text NOT NULL,
    query text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT saved_view_name_check CHECK (((length(name) >= 1) AND (length(name) <= 80))),
    CONSTRAINT saved_view_query_check CHECK ((length(query) <= 2000))
);


--
-- Name: schema_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.schema_migrations (
    version character varying NOT NULL
);


--
-- Name: site; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.site (
    slug text NOT NULL,
    name text NOT NULL,
    legacy_group text,
    state text,
    county text,
    geom public.geometry(Point,4326),
    tracking_names text[] DEFAULT '{}'::text[] NOT NULL,
    in_v1 boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: site_link; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.site_link (
    site_slug text NOT NULL,
    campus_id text NOT NULL,
    building_ids text[],
    evidence_fact_id bigint,
    status text DEFAULT 'proposed'::text NOT NULL,
    approved_by text,
    approved_at timestamp with time zone,
    CONSTRAINT site_link_status_check CHECK ((status = ANY (ARRAY['proposed'::text, 'approved'::text, 'rejected'::text])))
);


--
-- Name: source; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.source (
    id text NOT NULL,
    name text NOT NULL,
    kind text NOT NULL,
    base_url text,
    cadence_minutes integer,
    terms_note text,
    blocked boolean DEFAULT false NOT NULL,
    blocked_reason text,
    last_ok_at timestamp with time zone,
    last_error_at timestamp with time zone,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT source_kind_check CHECK ((kind = ANY (ARRAY['api'::text, 'portal'::text, 'feed'::text, 'seed'::text, 'press'::text, 'model'::text, 'manual'::text])))
);


--
-- Name: source_run; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.source_run (
    id bigint NOT NULL,
    source_id text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    ok boolean,
    items_seen integer DEFAULT 0 NOT NULL,
    items_new integer DEFAULT 0 NOT NULL,
    error text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: source_run_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.source_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: source_run_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.source_run_id_seq OWNED BY public.source_run.id;


--
-- Name: stream_event; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stream_event (
    id bigint NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    org_id text,
    payload jsonb NOT NULL,
    user_id text
);


--
-- Name: stream_event_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.stream_event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: stream_event_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.stream_event_id_seq OWNED BY public.stream_event.id;


--
-- Name: stress_saved; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stress_saved (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id text NOT NULL,
    user_id text,
    name text NOT NULL,
    request jsonb NOT NULL,
    result jsonb,
    ran_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: us_county; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.us_county (
    geoid text NOT NULL,
    statefp text NOT NULL,
    countyfp text NOT NULL,
    stusps text NOT NULL,
    name text NOT NULL,
    namelsad text NOT NULL,
    geom public.geometry(MultiPolygon,4326) NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT us_county_check CHECK ((geoid = (statefp || countyfp)))
);


--
-- Name: watchlist_item; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.watchlist_item (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id text NOT NULL,
    user_id text,
    kind text NOT NULL,
    ref_id text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT watchlist_item_kind_check CHECK ((kind = ANY (ARRAY['site'::text, 'instrument'::text])))
);


--
-- Name: weekly_note; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.weekly_note (
    id bigint NOT NULL,
    week date NOT NULL,
    version integer NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    writer text NOT NULL,
    note jsonb NOT NULL,
    figures jsonb NOT NULL,
    template jsonb NOT NULL,
    checks jsonb DEFAULT '{}'::jsonb NOT NULL,
    source_output_id bigint,
    inputs_hash text NOT NULL,
    model text,
    call_id bigint,
    run_id bigint,
    review_item_id bigint,
    decided_by text,
    decided_at timestamp with time zone,
    reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT weekly_note_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'approved'::text, 'rejected'::text, 'superseded'::text]))),
    CONSTRAINT weekly_note_writer_check CHECK ((writer = ANY (ARRAY['model'::text, 'code'::text])))
);


--
-- Name: weekly_note_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.weekly_note_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: weekly_note_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.weekly_note_id_seq OWNED BY public.weekly_note.id;


--
-- Name: agent_call id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_call ALTER COLUMN id SET DEFAULT nextval('public.agent_call_id_seq'::regclass);


--
-- Name: agent_candidate id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_candidate ALTER COLUMN id SET DEFAULT nextval('public.agent_candidate_id_seq'::regclass);


--
-- Name: agent_decision id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_decision ALTER COLUMN id SET DEFAULT nextval('public.agent_decision_id_seq'::regclass);


--
-- Name: agent_job id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_job ALTER COLUMN id SET DEFAULT nextval('public.agent_job_id_seq'::regclass);


--
-- Name: agent_link id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_link ALTER COLUMN id SET DEFAULT nextval('public.agent_link_id_seq'::regclass);


--
-- Name: agent_ops_run id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_ops_run ALTER COLUMN id SET DEFAULT nextval('public.agent_ops_run_id_seq'::regclass);


--
-- Name: agent_upload id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_upload ALTER COLUMN id SET DEFAULT nextval('public.agent_upload_id_seq'::regclass);


--
-- Name: alert id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert ALTER COLUMN id SET DEFAULT nextval('public.alert_id_seq'::regclass);


--
-- Name: audit_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log ALTER COLUMN id SET DEFAULT nextval('public.audit_log_id_seq'::regclass);


--
-- Name: backup_run id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backup_run ALTER COLUMN id SET DEFAULT nextval('public.backup_run_id_seq'::regclass);


--
-- Name: document id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document ALTER COLUMN id SET DEFAULT nextval('public.document_id_seq'::regclass);


--
-- Name: email_outbox id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_outbox ALTER COLUMN id SET DEFAULT nextval('public.email_outbox_id_seq'::regclass);


--
-- Name: event id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event ALTER COLUMN id SET DEFAULT nextval('public.event_id_seq'::regclass);


--
-- Name: fact id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact ALTER COLUMN id SET DEFAULT nextval('public.fact_id_seq'::regclass);


--
-- Name: holding id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.holding ALTER COLUMN id SET DEFAULT nextval('public.holding_id_seq'::regclass);


--
-- Name: model_output id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_output ALTER COLUMN id SET DEFAULT nextval('public.model_output_id_seq'::regclass);


--
-- Name: model_run id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_run ALTER COLUMN id SET DEFAULT nextval('public.model_run_id_seq'::regclass);


--
-- Name: price_obs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.price_obs ALTER COLUMN id SET DEFAULT nextval('public.price_obs_id_seq'::regclass);


--
-- Name: procrastinate_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.procrastinate_events ALTER COLUMN id SET DEFAULT nextval('public.procrastinate_events_id_seq'::regclass);


--
-- Name: procrastinate_jobs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.procrastinate_jobs ALTER COLUMN id SET DEFAULT nextval('public.procrastinate_jobs_id_seq'::regclass);


--
-- Name: procrastinate_periodic_defers id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.procrastinate_periodic_defers ALTER COLUMN id SET DEFAULT nextval('public.procrastinate_periodic_defers_id_seq'::regclass);


--
-- Name: research_find id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.research_find ALTER COLUMN id SET DEFAULT nextval('public.research_find_id_seq'::regclass);


--
-- Name: research_search id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.research_search ALTER COLUMN id SET DEFAULT nextval('public.research_search_id_seq'::regclass);


--
-- Name: review_item id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.review_item ALTER COLUMN id SET DEFAULT nextval('public.review_item_id_seq'::regclass);


--
-- Name: source_run id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_run ALTER COLUMN id SET DEFAULT nextval('public.source_run_id_seq'::regclass);


--
-- Name: stream_event id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stream_event ALTER COLUMN id SET DEFAULT nextval('public.stream_event_id_seq'::regclass);


--
-- Name: weekly_note id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.weekly_note ALTER COLUMN id SET DEFAULT nextval('public.weekly_note_id_seq'::regclass);


--
-- Name: agent_batch agent_batch_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_batch
    ADD CONSTRAINT agent_batch_pkey PRIMARY KEY (id);


--
-- Name: agent_call agent_call_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_call
    ADD CONSTRAINT agent_call_pkey PRIMARY KEY (id);


--
-- Name: agent_call agent_call_request_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_call
    ADD CONSTRAINT agent_call_request_hash_key UNIQUE (request_hash);


--
-- Name: agent_candidate agent_candidate_dedupe_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_candidate
    ADD CONSTRAINT agent_candidate_dedupe_key_key UNIQUE (dedupe_key);


--
-- Name: agent_candidate agent_candidate_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_candidate
    ADD CONSTRAINT agent_candidate_pkey PRIMARY KEY (id);


--
-- Name: agent_decision agent_decision_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_decision
    ADD CONSTRAINT agent_decision_pkey PRIMARY KEY (id);


--
-- Name: agent_file agent_file_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_file
    ADD CONSTRAINT agent_file_pkey PRIMARY KEY (sha256);


--
-- Name: agent_inbox agent_inbox_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_inbox
    ADD CONSTRAINT agent_inbox_pkey PRIMARY KEY (key);


--
-- Name: agent_job agent_job_document_id_prompt_version_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_job
    ADD CONSTRAINT agent_job_document_id_prompt_version_key UNIQUE (document_id, prompt_version);


--
-- Name: agent_job agent_job_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_job
    ADD CONSTRAINT agent_job_pkey PRIMARY KEY (id);


--
-- Name: agent_link agent_link_mention_key_subject_type_subject_id_relation_rel_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_link
    ADD CONSTRAINT agent_link_mention_key_subject_type_subject_id_relation_rel_key UNIQUE NULLS NOT DISTINCT (mention_key, subject_type, subject_id, relation, related_type, related_id);


--
-- Name: agent_link agent_link_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_link
    ADD CONSTRAINT agent_link_pkey PRIMARY KEY (id);


--
-- Name: agent_ops_run agent_ops_run_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_ops_run
    ADD CONSTRAINT agent_ops_run_pkey PRIMARY KEY (id);


--
-- Name: agent_status agent_status_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_status
    ADD CONSTRAINT agent_status_pkey PRIMARY KEY (id);


--
-- Name: agent_upload agent_upload_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_upload
    ADD CONSTRAINT agent_upload_pkey PRIMARY KEY (id);


--
-- Name: alert alert_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert
    ADD CONSTRAINT alert_pkey PRIMARY KEY (id);


--
-- Name: alert_settings alert_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_settings
    ADD CONSTRAINT alert_settings_pkey PRIMARY KEY (org_id, user_id);


--
-- Name: alert_state alert_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_state
    ADD CONSTRAINT alert_state_pkey PRIMARY KEY (model, subject_type, subject_id);


--
-- Name: alert_trigger alert_trigger_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_trigger
    ADD CONSTRAINT alert_trigger_pkey PRIMARY KEY (trigger_key);


--
-- Name: alert alert_user_dedupe; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert
    ADD CONSTRAINT alert_user_dedupe UNIQUE (org_id, user_id, dedupe_key);


--
-- Name: app_user app_user_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_user
    ADD CONSTRAINT app_user_pkey PRIMARY KEY (id);


--
-- Name: audit_log audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);


--
-- Name: backup_run backup_run_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backup_run
    ADD CONSTRAINT backup_run_pkey PRIMARY KEY (id);


--
-- Name: building building_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.building
    ADD CONSTRAINT building_pkey PRIMARY KEY (id);


--
-- Name: campus campus_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.campus
    ADD CONSTRAINT campus_pkey PRIMARY KEY (id);


--
-- Name: chain_party chain_party_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chain_party
    ADD CONSTRAINT chain_party_pkey PRIMARY KEY (site_slug, role);


--
-- Name: document document_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document
    ADD CONSTRAINT document_pkey PRIMARY KEY (id);


--
-- Name: document document_source_id_url_sha256_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document
    ADD CONSTRAINT document_source_id_url_sha256_key UNIQUE (source_id, url, sha256);


--
-- Name: email_outbox email_outbox_idempotency_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_outbox
    ADD CONSTRAINT email_outbox_idempotency_key_key UNIQUE (idempotency_key);


--
-- Name: email_outbox email_outbox_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_outbox
    ADD CONSTRAINT email_outbox_pkey PRIMARY KEY (id);


--
-- Name: email_provider_state email_provider_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_provider_state
    ADD CONSTRAINT email_provider_state_pkey PRIMARY KEY (id);


--
-- Name: entity entity_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity
    ADD CONSTRAINT entity_pkey PRIMARY KEY (id);


--
-- Name: event event_dedupe_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event
    ADD CONSTRAINT event_dedupe_key_key UNIQUE (dedupe_key);


--
-- Name: event event_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event
    ADD CONSTRAINT event_pkey PRIMARY KEY (id);


--
-- Name: fact fact_dedupe_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact
    ADD CONSTRAINT fact_dedupe_key_key UNIQUE (dedupe_key);


--
-- Name: fact fact_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact
    ADD CONSTRAINT fact_pkey PRIMARY KEY (id);


--
-- Name: grid_region grid_region_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grid_region
    ADD CONSTRAINT grid_region_pkey PRIMARY KEY (id);


--
-- Name: holding holding_fund_entity_id_accession_line_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.holding
    ADD CONSTRAINT holding_fund_entity_id_accession_line_key_key UNIQUE (fund_entity_id, accession, line_key);


--
-- Name: holding holding_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.holding
    ADD CONSTRAINT holding_pkey PRIMARY KEY (id);


--
-- Name: home_layout home_layout_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.home_layout
    ADD CONSTRAINT home_layout_pkey PRIMARY KEY (org_id, user_id);


--
-- Name: instrument instrument_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instrument
    ADD CONSTRAINT instrument_pkey PRIMARY KEY (id);


--
-- Name: member_product_role member_product_role_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.member_product_role
    ADD CONSTRAINT member_product_role_pkey PRIMARY KEY (org_id, user_id, product_id);


--
-- Name: membership membership_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.membership
    ADD CONSTRAINT membership_pkey PRIMARY KEY (org_id, user_id);


--
-- Name: model_dirty model_dirty_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_dirty
    ADD CONSTRAINT model_dirty_pkey PRIMARY KEY (model, key);


--
-- Name: model_output model_output_model_version_subject_type_subject_id_as_of_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_output
    ADD CONSTRAINT model_output_model_version_subject_type_subject_id_as_of_key UNIQUE (model, version, subject_type, subject_id, as_of);


--
-- Name: model_output model_output_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_output
    ADD CONSTRAINT model_output_pkey PRIMARY KEY (id);


--
-- Name: model_run model_run_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_run
    ADD CONSTRAINT model_run_pkey PRIMARY KEY (id);


--
-- Name: obligation obligation_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.obligation
    ADD CONSTRAINT obligation_pkey PRIMARY KEY (id);


--
-- Name: org_invitation org_invitation_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_invitation
    ADD CONSTRAINT org_invitation_pkey PRIMARY KEY (id);


--
-- Name: org org_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org
    ADD CONSTRAINT org_pkey PRIMARY KEY (id);


--
-- Name: org_product org_product_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_product
    ADD CONSTRAINT org_product_pkey PRIMARY KEY (org_id, product_id);


--
-- Name: plant_serves plant_serves_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plant_serves
    ADD CONSTRAINT plant_serves_pkey PRIMARY KEY (plant_id, campus_id);


--
-- Name: power_plant power_plant_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.power_plant
    ADD CONSTRAINT power_plant_pkey PRIMARY KEY (id);


--
-- Name: power_unit power_unit_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.power_unit
    ADD CONSTRAINT power_unit_pkey PRIMARY KEY (id);


--
-- Name: price_obs price_obs_dedupe_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.price_obs
    ADD CONSTRAINT price_obs_dedupe_key_key UNIQUE (dedupe_key);


--
-- Name: price_obs price_obs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.price_obs
    ADD CONSTRAINT price_obs_pkey PRIMARY KEY (id);


--
-- Name: procrastinate_events procrastinate_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.procrastinate_events
    ADD CONSTRAINT procrastinate_events_pkey PRIMARY KEY (id);


--
-- Name: procrastinate_jobs procrastinate_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.procrastinate_jobs
    ADD CONSTRAINT procrastinate_jobs_pkey PRIMARY KEY (id);


--
-- Name: procrastinate_periodic_defers procrastinate_periodic_defers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.procrastinate_periodic_defers
    ADD CONSTRAINT procrastinate_periodic_defers_pkey PRIMARY KEY (id);


--
-- Name: procrastinate_periodic_defers procrastinate_periodic_defers_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.procrastinate_periodic_defers
    ADD CONSTRAINT procrastinate_periodic_defers_unique UNIQUE (task_name, periodic_id, defer_timestamp);


--
-- Name: procrastinate_workers procrastinate_workers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.procrastinate_workers
    ADD CONSTRAINT procrastinate_workers_pkey PRIMARY KEY (id);


--
-- Name: product product_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product
    ADD CONSTRAINT product_pkey PRIMARY KEY (id);


--
-- Name: queue_project queue_project_iso_queue_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.queue_project
    ADD CONSTRAINT queue_project_iso_queue_id_key UNIQUE (iso, queue_id);


--
-- Name: queue_project queue_project_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.queue_project
    ADD CONSTRAINT queue_project_pkey PRIMARY KEY (id);


--
-- Name: read_model_dirty read_model_dirty_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.read_model_dirty
    ADD CONSTRAINT read_model_dirty_pkey PRIMARY KEY (view, key);


--
-- Name: read_model read_model_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.read_model
    ADD CONSTRAINT read_model_pkey PRIMARY KEY (view, key, as_of);


--
-- Name: research_find research_find_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.research_find
    ADD CONSTRAINT research_find_pkey PRIMARY KEY (id);


--
-- Name: research_find research_find_search_id_url_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.research_find
    ADD CONSTRAINT research_find_search_id_url_key UNIQUE (search_id, url);


--
-- Name: research_search research_search_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.research_search
    ADD CONSTRAINT research_search_pkey PRIMARY KEY (id);


--
-- Name: review_item review_item_kind_ref_type_ref_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.review_item
    ADD CONSTRAINT review_item_kind_ref_type_ref_id_key UNIQUE (kind, ref_type, ref_id);


--
-- Name: review_item review_item_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.review_item
    ADD CONSTRAINT review_item_pkey PRIMARY KEY (id);


--
-- Name: saved_view saved_view_org_id_user_id_screen_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.saved_view
    ADD CONSTRAINT saved_view_org_id_user_id_screen_name_key UNIQUE (org_id, user_id, screen, name);


--
-- Name: saved_view saved_view_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.saved_view
    ADD CONSTRAINT saved_view_pkey PRIMARY KEY (id);


--
-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (version);


--
-- Name: site site_legacy_group_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.site
    ADD CONSTRAINT site_legacy_group_key UNIQUE (legacy_group);


--
-- Name: site_link site_link_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.site_link
    ADD CONSTRAINT site_link_pkey PRIMARY KEY (site_slug, campus_id);


--
-- Name: site site_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.site
    ADD CONSTRAINT site_pkey PRIMARY KEY (slug);


--
-- Name: source source_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source
    ADD CONSTRAINT source_pkey PRIMARY KEY (id);


--
-- Name: source_run source_run_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_run
    ADD CONSTRAINT source_run_pkey PRIMARY KEY (id);


--
-- Name: stream_event stream_event_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stream_event
    ADD CONSTRAINT stream_event_pkey PRIMARY KEY (id);


--
-- Name: stress_saved stress_saved_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stress_saved
    ADD CONSTRAINT stress_saved_pkey PRIMARY KEY (id);


--
-- Name: us_county us_county_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.us_county
    ADD CONSTRAINT us_county_pkey PRIMARY KEY (geoid);


--
-- Name: watchlist_item watchlist_item_org_id_kind_ref_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.watchlist_item
    ADD CONSTRAINT watchlist_item_org_id_kind_ref_id_key UNIQUE (org_id, kind, ref_id);


--
-- Name: watchlist_item watchlist_item_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.watchlist_item
    ADD CONSTRAINT watchlist_item_pkey PRIMARY KEY (id);


--
-- Name: weekly_note weekly_note_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.weekly_note
    ADD CONSTRAINT weekly_note_pkey PRIMARY KEY (id);


--
-- Name: weekly_note weekly_note_week_version_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.weekly_note
    ADD CONSTRAINT weekly_note_week_version_key UNIQUE (week, version);


--
-- Name: agent_call_day; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_call_day ON public.agent_call USING btree (created_at);


--
-- Name: agent_call_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_call_job ON public.agent_call USING btree (job_id);


--
-- Name: agent_candidate_fact; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_candidate_fact ON public.agent_candidate USING btree (fact_id) WHERE (fact_id IS NOT NULL);


--
-- Name: agent_candidate_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_candidate_job ON public.agent_candidate USING btree (job_id);


--
-- Name: agent_decision_one_open; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX agent_decision_one_open ON public.agent_decision USING btree (review_item_id) WHERE (applied_at IS NULL);


--
-- Name: agent_decision_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_decision_pending ON public.agent_decision USING btree (created_at) WHERE (applied_at IS NULL);


--
-- Name: agent_job_ready; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_job_ready ON public.agent_job USING btree (priority DESC, created_at) WHERE (state = 'queued'::text);


--
-- Name: agent_job_sha; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_job_sha ON public.agent_job USING btree (sha256);


--
-- Name: agent_job_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_job_state ON public.agent_job USING btree (state, updated_at DESC);


--
-- Name: agent_link_mention; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_link_mention ON public.agent_link USING btree (mention_key) WHERE (status = 'accepted'::text);


--
-- Name: agent_ops_run_role; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_ops_run_role ON public.agent_ops_run USING btree (role, started_at DESC);


--
-- Name: agent_upload_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_upload_pending ON public.agent_upload USING btree (created_at) WHERE (status = 'pending'::text);


--
-- Name: alert_matter; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX alert_matter ON public.alert USING btree (org_id, user_id, subject_type, subject_id, rule_code, occurred_on DESC);


--
-- Name: alert_trigger_subject; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX alert_trigger_subject ON public.alert_trigger USING btree (subject_type, subject_id, occurred_on DESC);


--
-- Name: alert_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX alert_user ON public.alert USING btree (org_id, user_id, at DESC);


--
-- Name: audit_org; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX audit_org ON public.audit_log USING btree (org_id, at DESC);


--
-- Name: backup_run_object_key_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX backup_run_object_key_idx ON public.backup_run USING btree (bucket, object_key) WHERE (object_key IS NOT NULL);


--
-- Name: backup_run_started_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX backup_run_started_idx ON public.backup_run USING btree (started_at DESC);


--
-- Name: building_campus; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX building_campus ON public.building USING btree (campus_id);


--
-- Name: building_footprint; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX building_footprint ON public.building USING gist (footprint);


--
-- Name: campus_geom; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX campus_geom ON public.campus USING gist (geom);


--
-- Name: campus_name_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX campus_name_trgm ON public.campus USING gin (name public.gin_trgm_ops);


--
-- Name: document_external; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_external ON public.document USING btree (source_id, external_id);


--
-- Name: document_extract; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_extract ON public.document USING btree (extract_state) WHERE (extract_state = 'queued'::text);


--
-- Name: email_outbox_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX email_outbox_due ON public.email_outbox USING btree (next_attempt_at) WHERE (status = 'queued'::text);


--
-- Name: email_outbox_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX email_outbox_user ON public.email_outbox USING btree (org_id, user_id, created_at DESC);


--
-- Name: entity_cik; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX entity_cik ON public.entity USING btree (cik) WHERE (cik IS NOT NULL);


--
-- Name: entity_name_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX entity_name_trgm ON public.entity USING gin (name public.gin_trgm_ops);


--
-- Name: event_subject; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX event_subject ON public.event USING btree (subject_type, subject_id, event_date DESC);


--
-- Name: fact_document; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX fact_document ON public.fact USING btree (document_id) WHERE (document_id IS NOT NULL);


--
-- Name: fact_status_learned; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX fact_status_learned ON public.fact USING btree (status, learned_at);


--
-- Name: fact_subject_known; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX fact_subject_known ON public.fact USING btree (subject_type, subject_id, field) WHERE (status = ANY (ARRAY['published'::text, 'superseded'::text]));


--
-- Name: fact_subject_pub; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX fact_subject_pub ON public.fact USING btree (subject_type, subject_id, field) WHERE (status = 'published'::text);


--
-- Name: grid_region_geom; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX grid_region_geom ON public.grid_region USING gist (geom);


--
-- Name: grid_region_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX grid_region_kind ON public.grid_region USING btree (kind);


--
-- Name: grid_region_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX grid_region_parent ON public.grid_region USING btree (parent_id);


--
-- Name: holding_cusip; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX holding_cusip ON public.holding USING btree (cusip) WHERE (cusip IS NOT NULL);


--
-- Name: holding_instrument; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX holding_instrument ON public.holding USING btree (instrument_id, period_end DESC);


--
-- Name: idx_procrastinate_jobs_worker_not_null; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_procrastinate_jobs_worker_not_null ON public.procrastinate_jobs USING btree (worker_id) WHERE ((worker_id IS NOT NULL) AND (status = 'doing'::public.procrastinate_job_status));


--
-- Name: idx_procrastinate_workers_last_heartbeat; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_procrastinate_workers_last_heartbeat ON public.procrastinate_workers USING btree (last_heartbeat);


--
-- Name: instrument_cusip; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX instrument_cusip ON public.instrument USING btree (cusip) WHERE (cusip IS NOT NULL);


--
-- Name: instrument_name_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX instrument_name_trgm ON public.instrument USING gin (name public.gin_trgm_ops);


--
-- Name: instrument_site; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX instrument_site ON public.instrument USING btree (site_slug);


--
-- Name: member_product_role_product; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX member_product_role_product ON public.member_product_role USING btree (org_id, product_id);


--
-- Name: model_dirty_marked; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX model_dirty_marked ON public.model_dirty USING btree (model, marked_at);


--
-- Name: model_output_subject; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX model_output_subject ON public.model_output USING btree (model, subject_type, subject_id, as_of DESC);


--
-- Name: obligation_from; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX obligation_from ON public.obligation USING btree (from_entity_id);


--
-- Name: obligation_to; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX obligation_to ON public.obligation USING btree (to_entity_id);


--
-- Name: org_invitation_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX org_invitation_pending ON public.org_invitation USING btree (org_id, email) WHERE (status = 'pending'::text);


--
-- Name: org_product_product; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX org_product_product ON public.org_product USING btree (product_id);


--
-- Name: power_plant_geom; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX power_plant_geom ON public.power_plant USING gist (geom);


--
-- Name: price_obs_instrument; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX price_obs_instrument ON public.price_obs USING btree (instrument_id, obs_date DESC);


--
-- Name: procrastinate_events_job_id_fkey_v1; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX procrastinate_events_job_id_fkey_v1 ON public.procrastinate_events USING btree (job_id);


--
-- Name: procrastinate_jobs_id_lock_idx_v1; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX procrastinate_jobs_id_lock_idx_v1 ON public.procrastinate_jobs USING btree (id, lock) WHERE (status = ANY (ARRAY['todo'::public.procrastinate_job_status, 'doing'::public.procrastinate_job_status]));


--
-- Name: procrastinate_jobs_lock_idx_v1; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX procrastinate_jobs_lock_idx_v1 ON public.procrastinate_jobs USING btree (lock) WHERE (status = 'doing'::public.procrastinate_job_status);


--
-- Name: procrastinate_jobs_priority_idx_v1; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX procrastinate_jobs_priority_idx_v1 ON public.procrastinate_jobs USING btree (priority DESC, id) WHERE (status = 'todo'::public.procrastinate_job_status);


--
-- Name: procrastinate_jobs_queue_name_idx_v1; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX procrastinate_jobs_queue_name_idx_v1 ON public.procrastinate_jobs USING btree (queue_name);


--
-- Name: procrastinate_jobs_queueing_lock_idx_v1; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX procrastinate_jobs_queueing_lock_idx_v1 ON public.procrastinate_jobs USING btree (queueing_lock) WHERE (status = 'todo'::public.procrastinate_job_status);


--
-- Name: procrastinate_periodic_defers_job_id_fkey_v1; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX procrastinate_periodic_defers_job_id_fkey_v1 ON public.procrastinate_periodic_defers USING btree (job_id);


--
-- Name: queue_project_place; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX queue_project_place ON public.queue_project USING btree (state, county);


--
-- Name: read_model_latest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX read_model_latest ON public.read_model USING btree (view, as_of DESC);


--
-- Name: research_find_url; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX research_find_url ON public.research_find USING btree (url);


--
-- Name: research_search_query; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX research_search_query ON public.research_search USING btree (endpoint, query, created_at DESC);


--
-- Name: research_search_run; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX research_search_run ON public.research_search USING btree (run_id);


--
-- Name: review_open; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX review_open ON public.review_item USING btree (status, created_at) WHERE (status = 'open'::text);


--
-- Name: saved_view_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX saved_view_owner ON public.saved_view USING btree (org_id, user_id, screen, created_at DESC);


--
-- Name: source_run_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX source_run_source ON public.source_run USING btree (source_id, started_at DESC);


--
-- Name: us_county_geom; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX us_county_geom ON public.us_county USING gist (geom);


--
-- Name: us_county_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX us_county_state ON public.us_county USING btree (stusps);


--
-- Name: weekly_note_one_approved; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX weekly_note_one_approved ON public.weekly_note USING btree (week) WHERE (status = 'approved'::text);


--
-- Name: weekly_note_one_draft; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX weekly_note_one_draft ON public.weekly_note USING btree (week) WHERE (status = 'draft'::text);


--
-- Name: agent_decision agent_decision_notify; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER agent_decision_notify AFTER INSERT ON public.agent_decision FOR EACH ROW EXECUTE FUNCTION public.agent_notify();


--
-- Name: agent_upload agent_upload_notify; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER agent_upload_notify AFTER INSERT ON public.agent_upload FOR EACH ROW EXECUTE FUNCTION public.agent_notify();


--
-- Name: audit_log audit_log_no_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER audit_log_no_update BEFORE DELETE OR UPDATE ON public.audit_log FOR EACH ROW EXECUTE FUNCTION public.audit_log_immutable();


--
-- Name: document document_agent_notify; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER document_agent_notify AFTER INSERT OR UPDATE OF extract_state ON public.document FOR EACH ROW WHEN ((new.extract_state = 'queued'::text)) EXECUTE FUNCTION public.agent_notify();


--
-- Name: procrastinate_jobs procrastinate_jobs_notify_queue_job_aborted_v1; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER procrastinate_jobs_notify_queue_job_aborted_v1 AFTER UPDATE OF abort_requested ON public.procrastinate_jobs FOR EACH ROW WHEN (((old.abort_requested = false) AND (new.abort_requested = true) AND (new.status = 'doing'::public.procrastinate_job_status))) EXECUTE FUNCTION public.procrastinate_notify_queue_abort_job_v1();


--
-- Name: procrastinate_jobs procrastinate_jobs_notify_queue_job_inserted_v1; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER procrastinate_jobs_notify_queue_job_inserted_v1 AFTER INSERT ON public.procrastinate_jobs FOR EACH ROW WHEN ((new.status = 'todo'::public.procrastinate_job_status)) EXECUTE FUNCTION public.procrastinate_notify_queue_job_inserted_v1();


--
-- Name: procrastinate_jobs procrastinate_trigger_abort_requested_events_v1; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER procrastinate_trigger_abort_requested_events_v1 AFTER UPDATE OF abort_requested ON public.procrastinate_jobs FOR EACH ROW WHEN ((new.abort_requested = true)) EXECUTE FUNCTION public.procrastinate_trigger_abort_requested_events_procedure_v1();


--
-- Name: procrastinate_jobs procrastinate_trigger_delete_jobs_v1; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER procrastinate_trigger_delete_jobs_v1 BEFORE DELETE ON public.procrastinate_jobs FOR EACH ROW EXECUTE FUNCTION public.procrastinate_unlink_periodic_defers_v1();


--
-- Name: procrastinate_jobs procrastinate_trigger_scheduled_events_v1; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER procrastinate_trigger_scheduled_events_v1 AFTER INSERT OR UPDATE ON public.procrastinate_jobs FOR EACH ROW WHEN (((new.scheduled_at IS NOT NULL) AND (new.status = 'todo'::public.procrastinate_job_status))) EXECUTE FUNCTION public.procrastinate_trigger_function_scheduled_events_v1();


--
-- Name: procrastinate_jobs procrastinate_trigger_status_events_insert_v1; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER procrastinate_trigger_status_events_insert_v1 AFTER INSERT ON public.procrastinate_jobs FOR EACH ROW WHEN ((new.status = 'todo'::public.procrastinate_job_status)) EXECUTE FUNCTION public.procrastinate_trigger_function_status_events_insert_v1();


--
-- Name: procrastinate_jobs procrastinate_trigger_status_events_update_v1; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER procrastinate_trigger_status_events_update_v1 AFTER UPDATE OF status ON public.procrastinate_jobs FOR EACH ROW EXECUTE FUNCTION public.procrastinate_trigger_function_status_events_update_v1();


--
-- Name: stream_event stream_event_notify; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER stream_event_notify AFTER INSERT ON public.stream_event FOR EACH ROW EXECUTE FUNCTION public.stream_event_notify();


--
-- Name: agent_call agent_call_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_call
    ADD CONSTRAINT agent_call_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.agent_job(id) ON DELETE SET NULL;


--
-- Name: agent_candidate agent_candidate_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_candidate
    ADD CONSTRAINT agent_candidate_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.document(id);


--
-- Name: agent_candidate agent_candidate_fact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_candidate
    ADD CONSTRAINT agent_candidate_fact_id_fkey FOREIGN KEY (fact_id) REFERENCES public.fact(id);


--
-- Name: agent_candidate agent_candidate_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_candidate
    ADD CONSTRAINT agent_candidate_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.agent_job(id) ON DELETE CASCADE;


--
-- Name: agent_decision agent_decision_review_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_decision
    ADD CONSTRAINT agent_decision_review_item_id_fkey FOREIGN KEY (review_item_id) REFERENCES public.review_item(id);


--
-- Name: agent_inbox agent_inbox_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_inbox
    ADD CONSTRAINT agent_inbox_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.document(id);


--
-- Name: agent_job agent_job_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_job
    ADD CONSTRAINT agent_job_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.document(id);


--
-- Name: agent_link agent_link_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_link
    ADD CONSTRAINT agent_link_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.document(id);


--
-- Name: agent_link agent_link_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_link
    ADD CONSTRAINT agent_link_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.agent_job(id) ON DELETE SET NULL;


--
-- Name: agent_upload agent_upload_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_upload
    ADD CONSTRAINT agent_upload_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.document(id);


--
-- Name: alert alert_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert
    ADD CONSTRAINT alert_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.event(id);


--
-- Name: alert alert_member; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert
    ADD CONSTRAINT alert_member FOREIGN KEY (org_id, user_id) REFERENCES public.membership(org_id, user_id) ON DELETE CASCADE;


--
-- Name: alert alert_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert
    ADD CONSTRAINT alert_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.org(id) ON DELETE CASCADE;


--
-- Name: alert_settings alert_settings_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_settings
    ADD CONSTRAINT alert_settings_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.org(id) ON DELETE CASCADE;


--
-- Name: alert_settings alert_settings_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert_settings
    ADD CONSTRAINT alert_settings_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;


--
-- Name: alert alert_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert
    ADD CONSTRAINT alert_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;


--
-- Name: building building_campus_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.building
    ADD CONSTRAINT building_campus_id_fkey FOREIGN KEY (campus_id) REFERENCES public.campus(id) ON DELETE CASCADE;


--
-- Name: chain_party chain_party_entity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chain_party
    ADD CONSTRAINT chain_party_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity(id);


--
-- Name: chain_party chain_party_site_slug_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chain_party
    ADD CONSTRAINT chain_party_site_slug_fkey FOREIGN KEY (site_slug) REFERENCES public.site(slug) ON DELETE CASCADE;


--
-- Name: document document_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document
    ADD CONSTRAINT document_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.source(id);


--
-- Name: email_outbox email_outbox_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_outbox
    ADD CONSTRAINT email_outbox_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.org(id) ON DELETE CASCADE;


--
-- Name: email_outbox email_outbox_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_outbox
    ADD CONSTRAINT email_outbox_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;


--
-- Name: entity entity_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity
    ADD CONSTRAINT entity_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.entity(id);


--
-- Name: event event_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event
    ADD CONSTRAINT event_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.document(id);


--
-- Name: event event_fact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event
    ADD CONSTRAINT event_fact_id_fkey FOREIGN KEY (fact_id) REFERENCES public.fact(id);


--
-- Name: fact fact_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact
    ADD CONSTRAINT fact_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.document(id);


--
-- Name: grid_region grid_region_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.grid_region
    ADD CONSTRAINT grid_region_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.grid_region(id) ON DELETE SET NULL;


--
-- Name: holding holding_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.holding
    ADD CONSTRAINT holding_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.document(id);


--
-- Name: holding holding_fund_entity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.holding
    ADD CONSTRAINT holding_fund_entity_id_fkey FOREIGN KEY (fund_entity_id) REFERENCES public.entity(id);


--
-- Name: holding holding_instrument_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.holding
    ADD CONSTRAINT holding_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);


--
-- Name: home_layout home_layout_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.home_layout
    ADD CONSTRAINT home_layout_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.org(id) ON DELETE CASCADE;


--
-- Name: home_layout home_layout_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.home_layout
    ADD CONSTRAINT home_layout_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;


--
-- Name: instrument instrument_issuer_entity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instrument
    ADD CONSTRAINT instrument_issuer_entity_id_fkey FOREIGN KEY (issuer_entity_id) REFERENCES public.entity(id);


--
-- Name: instrument instrument_site_slug_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instrument
    ADD CONSTRAINT instrument_site_slug_fkey FOREIGN KEY (site_slug) REFERENCES public.site(slug);


--
-- Name: member_product_role member_product_role_granted_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.member_product_role
    ADD CONSTRAINT member_product_role_granted_by_fkey FOREIGN KEY (granted_by) REFERENCES public.app_user(id) ON DELETE SET NULL;


--
-- Name: member_product_role member_product_role_org_id_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.member_product_role
    ADD CONSTRAINT member_product_role_org_id_product_id_fkey FOREIGN KEY (org_id, product_id) REFERENCES public.org_product(org_id, product_id) ON DELETE CASCADE;


--
-- Name: member_product_role member_product_role_org_id_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.member_product_role
    ADD CONSTRAINT member_product_role_org_id_user_id_fkey FOREIGN KEY (org_id, user_id) REFERENCES public.membership(org_id, user_id) ON DELETE CASCADE;


--
-- Name: membership membership_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.membership
    ADD CONSTRAINT membership_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.org(id) ON DELETE CASCADE;


--
-- Name: membership membership_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.membership
    ADD CONSTRAINT membership_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;


--
-- Name: model_output model_output_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_output
    ADD CONSTRAINT model_output_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.model_run(id);


--
-- Name: obligation obligation_campus_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.obligation
    ADD CONSTRAINT obligation_campus_id_fkey FOREIGN KEY (campus_id) REFERENCES public.campus(id);


--
-- Name: obligation obligation_from_entity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.obligation
    ADD CONSTRAINT obligation_from_entity_id_fkey FOREIGN KEY (from_entity_id) REFERENCES public.entity(id);


--
-- Name: obligation obligation_site_slug_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.obligation
    ADD CONSTRAINT obligation_site_slug_fkey FOREIGN KEY (site_slug) REFERENCES public.site(slug);


--
-- Name: obligation obligation_to_entity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.obligation
    ADD CONSTRAINT obligation_to_entity_id_fkey FOREIGN KEY (to_entity_id) REFERENCES public.entity(id);


--
-- Name: org_invitation org_invitation_accepted_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_invitation
    ADD CONSTRAINT org_invitation_accepted_by_fkey FOREIGN KEY (accepted_by) REFERENCES public.app_user(id) ON DELETE SET NULL;


--
-- Name: org_invitation org_invitation_invited_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_invitation
    ADD CONSTRAINT org_invitation_invited_by_fkey FOREIGN KEY (invited_by) REFERENCES public.app_user(id) ON DELETE SET NULL;


--
-- Name: org_invitation org_invitation_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_invitation
    ADD CONSTRAINT org_invitation_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.org(id) ON DELETE CASCADE;


--
-- Name: org_product org_product_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_product
    ADD CONSTRAINT org_product_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.org(id) ON DELETE CASCADE;


--
-- Name: org_product org_product_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_product
    ADD CONSTRAINT org_product_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.product(id);


--
-- Name: plant_serves plant_serves_campus_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plant_serves
    ADD CONSTRAINT plant_serves_campus_id_fkey FOREIGN KEY (campus_id) REFERENCES public.campus(id) ON DELETE CASCADE;


--
-- Name: plant_serves plant_serves_evidence_fact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plant_serves
    ADD CONSTRAINT plant_serves_evidence_fact_id_fkey FOREIGN KEY (evidence_fact_id) REFERENCES public.fact(id);


--
-- Name: plant_serves plant_serves_plant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plant_serves
    ADD CONSTRAINT plant_serves_plant_id_fkey FOREIGN KEY (plant_id) REFERENCES public.power_plant(id) ON DELETE CASCADE;


--
-- Name: power_unit power_unit_plant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.power_unit
    ADD CONSTRAINT power_unit_plant_id_fkey FOREIGN KEY (plant_id) REFERENCES public.power_plant(id) ON DELETE CASCADE;


--
-- Name: price_obs price_obs_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.price_obs
    ADD CONSTRAINT price_obs_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.document(id);


--
-- Name: price_obs price_obs_instrument_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.price_obs
    ADD CONSTRAINT price_obs_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES public.instrument(id);


--
-- Name: procrastinate_events procrastinate_events_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.procrastinate_events
    ADD CONSTRAINT procrastinate_events_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.procrastinate_jobs(id) ON DELETE CASCADE;


--
-- Name: procrastinate_jobs procrastinate_jobs_worker_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.procrastinate_jobs
    ADD CONSTRAINT procrastinate_jobs_worker_id_fkey FOREIGN KEY (worker_id) REFERENCES public.procrastinate_workers(id) ON DELETE SET NULL;


--
-- Name: procrastinate_periodic_defers procrastinate_periodic_defers_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.procrastinate_periodic_defers
    ADD CONSTRAINT procrastinate_periodic_defers_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.procrastinate_jobs(id);


--
-- Name: research_find research_find_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.research_find
    ADD CONSTRAINT research_find_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.document(id);


--
-- Name: research_find research_find_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.research_find
    ADD CONSTRAINT research_find_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.agent_job(id) ON DELETE SET NULL;


--
-- Name: research_find research_find_search_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.research_find
    ADD CONSTRAINT research_find_search_id_fkey FOREIGN KEY (search_id) REFERENCES public.research_search(id) ON DELETE CASCADE;


--
-- Name: research_search research_search_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.research_search
    ADD CONSTRAINT research_search_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.document(id);


--
-- Name: research_search research_search_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.research_search
    ADD CONSTRAINT research_search_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.agent_ops_run(id) ON DELETE SET NULL;


--
-- Name: saved_view saved_view_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.saved_view
    ADD CONSTRAINT saved_view_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.org(id) ON DELETE CASCADE;


--
-- Name: saved_view saved_view_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.saved_view
    ADD CONSTRAINT saved_view_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;


--
-- Name: site_link site_link_campus_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.site_link
    ADD CONSTRAINT site_link_campus_id_fkey FOREIGN KEY (campus_id) REFERENCES public.campus(id) ON DELETE CASCADE;


--
-- Name: site_link site_link_evidence_fact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.site_link
    ADD CONSTRAINT site_link_evidence_fact_id_fkey FOREIGN KEY (evidence_fact_id) REFERENCES public.fact(id);


--
-- Name: site_link site_link_site_slug_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.site_link
    ADD CONSTRAINT site_link_site_slug_fkey FOREIGN KEY (site_slug) REFERENCES public.site(slug) ON DELETE CASCADE;


--
-- Name: source_run source_run_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_run
    ADD CONSTRAINT source_run_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.source(id);


--
-- Name: stress_saved stress_saved_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stress_saved
    ADD CONSTRAINT stress_saved_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.org(id) ON DELETE CASCADE;


--
-- Name: stress_saved stress_saved_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stress_saved
    ADD CONSTRAINT stress_saved_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE SET NULL;


--
-- Name: watchlist_item watchlist_item_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.watchlist_item
    ADD CONSTRAINT watchlist_item_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.org(id) ON DELETE CASCADE;


--
-- Name: watchlist_item watchlist_item_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.watchlist_item
    ADD CONSTRAINT watchlist_item_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE SET NULL;


--
-- Name: weekly_note weekly_note_call_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.weekly_note
    ADD CONSTRAINT weekly_note_call_id_fkey FOREIGN KEY (call_id) REFERENCES public.agent_call(id) ON DELETE SET NULL;


--
-- Name: weekly_note weekly_note_review_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.weekly_note
    ADD CONSTRAINT weekly_note_review_item_id_fkey FOREIGN KEY (review_item_id) REFERENCES public.review_item(id);


--
-- Name: weekly_note weekly_note_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.weekly_note
    ADD CONSTRAINT weekly_note_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.agent_ops_run(id) ON DELETE SET NULL;


--
-- Name: weekly_note weekly_note_source_output_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.weekly_note
    ADD CONSTRAINT weekly_note_source_output_id_fkey FOREIGN KEY (source_output_id) REFERENCES public.model_output(id) ON DELETE SET NULL;


--
-- PostgreSQL database dump complete
--

\unrestrict dbmate


--
-- Dbmate schema migrations
--

INSERT INTO public.schema_migrations (version) VALUES
    ('20261021100000'),
    ('20261021100100'),
    ('20261021140000'),
    ('20261021150000'),
    ('20261021170000'),
    ('20261021180000'),
    ('20261021190000'),
    ('20261021200000'),
    ('20261021210000'),
    ('20261021223000'),
    ('20261021233000'),
    ('20261021234100'),
    ('20261021235500'),
    ('20261022001000'),
    ('20261022020000'),
    ('20261022030000'),
    ('20261022041700'),
    ('20261022050000'),
    ('20261022090000'),
    ('20261022200000');
