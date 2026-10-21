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
    AND f.learned_at < (as_of + 1)::timestamptz
    AND (f.valid_to IS NULL OR f.valid_to > as_of)
  ORDER BY subject_type, subject_id, field,
           (tag = 'M'), valid_from DESC,
           (CASE tag WHEN 'F' THEN 0 WHEN 'R' THEN 1 ELSE 2 END), learned_at DESC, id DESC
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
    severity text,
    read_at timestamp with time zone,
    emailed_at timestamp with time zone,
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
  WHERE ((status = 'published'::text) AND ((valid_to IS NULL) OR (valid_to > CURRENT_DATE)))
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
-- Name: membership; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.membership (
    org_id text NOT NULL,
    user_id text NOT NULL,
    role text NOT NULL,
    CONSTRAINT membership_role_check CHECK ((role = ANY (ARRAY['owner'::text, 'admin'::text, 'member'::text, 'viewer'::text])))
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
    CONSTRAINT review_item_kind_check CHECK ((kind = ANY (ARRAY['fact'::text, 'event'::text, 'link'::text, 'merge'::text, 'note'::text, 'source'::text]))),
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
    payload jsonb NOT NULL
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
-- Name: alert id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert ALTER COLUMN id SET DEFAULT nextval('public.alert_id_seq'::regclass);


--
-- Name: audit_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log ALTER COLUMN id SET DEFAULT nextval('public.audit_log_id_seq'::regclass);


--
-- Name: document id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document ALTER COLUMN id SET DEFAULT nextval('public.document_id_seq'::regclass);


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
-- Name: alert alert_org_id_kind_subject_type_subject_id_event_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert
    ADD CONSTRAINT alert_org_id_kind_subject_type_subject_id_event_id_key UNIQUE (org_id, kind, subject_type, subject_id, event_id);


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
-- Name: membership membership_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.membership
    ADD CONSTRAINT membership_pkey PRIMARY KEY (org_id, user_id);


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
-- Name: org org_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org
    ADD CONSTRAINT org_pkey PRIMARY KEY (id);


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
-- Name: alert_org; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX alert_org ON public.alert USING btree (org_id, at DESC);


--
-- Name: audit_org; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX audit_org ON public.audit_log USING btree (org_id, at DESC);


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
-- Name: fact_subject_pub; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX fact_subject_pub ON public.fact USING btree (subject_type, subject_id, field) WHERE (status = 'published'::text);


--
-- Name: holding_cusip; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX holding_cusip ON public.holding USING btree (cusip) WHERE (cusip IS NOT NULL);


--
-- Name: holding_instrument; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX holding_instrument ON public.holding USING btree (instrument_id, period_end DESC);


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
-- Name: power_plant_geom; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX power_plant_geom ON public.power_plant USING gist (geom);


--
-- Name: price_obs_instrument; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX price_obs_instrument ON public.price_obs USING btree (instrument_id, obs_date DESC);


--
-- Name: read_model_latest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX read_model_latest ON public.read_model USING btree (view, as_of DESC);


--
-- Name: review_open; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX review_open ON public.review_item USING btree (status, created_at) WHERE (status = 'open'::text);


--
-- Name: source_run_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX source_run_source ON public.source_run USING btree (source_id, started_at DESC);


--
-- Name: audit_log audit_log_no_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER audit_log_no_update BEFORE DELETE OR UPDATE ON public.audit_log FOR EACH ROW EXECUTE FUNCTION public.audit_log_immutable();


--
-- Name: stream_event stream_event_notify; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER stream_event_notify AFTER INSERT ON public.stream_event FOR EACH ROW EXECUTE FUNCTION public.stream_event_notify();


--
-- Name: alert alert_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alert
    ADD CONSTRAINT alert_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.event(id);


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
-- PostgreSQL database dump complete
--

\unrestrict dbmate


--
-- Dbmate schema migrations
--

INSERT INTO public.schema_migrations (version) VALUES
    ('20261021100000');
