-- migrate:up
-- Marden core schema: raw documents, entities, the fact store, domain identities,
-- prices and holdings, events, model outputs, read models, review queue and app tables.
-- Rule: identity tables hold only identity (ids, names, geometry, joins). Every displayed
-- attribute is a row in `fact`, with its tag, source and as-of date. Read models are rebuilt
-- from published facts and model outputs; the API reads read models only.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------- sources and raw documents
CREATE TABLE source (
  id               text PRIMARY KEY,            -- 'sec_edgar', 'ferc_elibrary', 'seed_aidebt'
  name             text NOT NULL,               -- shown as sourceName: 'SEC EDGAR', 'N-PORT'
  kind             text NOT NULL CHECK (kind IN ('api', 'portal', 'feed', 'seed', 'press', 'model', 'manual')),
  base_url         text,
  cadence_minutes  integer,                     -- expected cadence; null = on demand. Drives heartbeats.
  terms_note       text,                        -- terms of use / robots summary checked before collecting
  blocked          boolean NOT NULL DEFAULT false, -- terms forbid automated collection: never run
  blocked_reason   text,
  last_ok_at       timestamptz,
  last_error_at    timestamptz,
  last_error       text,
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE source_run (
  id           bigserial PRIMARY KEY,
  source_id    text NOT NULL REFERENCES source(id),
  started_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz,
  ok           boolean,
  items_seen   integer NOT NULL DEFAULT 0,
  items_new    integer NOT NULL DEFAULT 0,
  error        text,
  meta         jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX source_run_source ON source_run (source_id, started_at DESC);

-- Raw first: every fetched file is stored unchanged (R2 or the local raw store) before parsing.
CREATE TABLE document (
  id            bigserial PRIMARY KEY,
  source_id     text NOT NULL REFERENCES source(id),
  url           text NOT NULL,
  sha256        text NOT NULL,
  fetched_at    timestamptz NOT NULL DEFAULT now(),
  content_type  text,
  bytes         bigint,
  storage_key   text NOT NULL,
  title         text,
  doc_type      text,                          -- '8-K', 'N-PORT', 'EX-10', 'air_permit', 'agenda', 'large_load_report'
  doc_date      date,
  external_id   text,                          -- accession number, permit number, docket id
  meta          jsonb NOT NULL DEFAULT '{}',
  extract_state text NOT NULL DEFAULT 'none' CHECK (extract_state IN ('none', 'queued', 'done', 'failed', 'skipped')),
  UNIQUE (source_id, url, sha256)
);
CREATE INDEX document_external ON document (source_id, external_id);
CREATE INDEX document_extract ON document (extract_state) WHERE extract_state = 'queued';

-- ---------------------------------------------------------------- entities (parties)
CREATE TABLE entity (
  id          text PRIMARY KEY,                -- slug: 'oracle', 'red-chiles-a-llc', 'pgim-total-return-bond-fund'
  name        text NOT NULL,
  kind        text NOT NULL,                   -- company, spv, fund, bdc, bank, lender, agency, utility, developer, ai_lab, cloud, neocloud, chipmaker, guarantor, power_provider, county, court, other
  parent_id   text REFERENCES entity(id),
  aliases     text[] NOT NULL DEFAULT '{}',
  cik         text,
  lei         text,
  ticker      text,
  meta        jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX entity_name_trgm ON entity USING gin (name gin_trgm_ops);
CREATE INDEX entity_cik ON entity (cik) WHERE cik IS NOT NULL;

-- ---------------------------------------------------------------- the fact store
-- subject_type: site | campus | building | plant | unit | instrument | entity | contract | obligation | holding
CREATE TABLE fact (
  id              bigserial PRIMARY KEY,
  subject_type    text NOT NULL,
  subject_id      text NOT NULL,
  field           text NOT NULL,
  value           jsonb,
  unit            text,                        -- 'USD', 'USDbn', 'MW', 'pct', 'pts', 'months', 'date'
  tag             char(1) NOT NULL CHECK (tag IN ('F', 'R', 'M')),
  source_name     text NOT NULL,
  source_url      text,
  document_id     bigint REFERENCES document(id),
  quote           text,                        -- exact source passage (required for extracted facts)
  quote_loc       text,                        -- page / section / xpath
  valid_from      date NOT NULL,               -- when the value became true (asOf shown in the UI)
  valid_to        date,
  learned_at      timestamptz NOT NULL DEFAULT now(),
  status          text NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate', 'published', 'rejected', 'superseded')),
  method_version  text,                        -- required for tag M
  producer        text NOT NULL,               -- 'seed:aidebt', 'collector:nport', 'extractor:8k@1', 'model:cushion@0.1'
  confidence      real,
  verifier        text CHECK (verifier IN ('agree', 'disagree', 'unsupported')),
  material        boolean NOT NULL DEFAULT false,
  dedupe_key      text NOT NULL UNIQUE,        -- safe reruns: same subject/field/value/source/date never duplicates
  CHECK (tag <> 'M' OR method_version IS NOT NULL)
);
CREATE INDEX fact_subject_pub ON fact (subject_type, subject_id, field) WHERE status = 'published';
CREATE INDEX fact_status_learned ON fact (status, learned_at);
CREATE INDEX fact_document ON fact (document_id) WHERE document_id IS NOT NULL;

-- Current value per subject/field. Filed or reported values always beat model estimates; among
-- non-model values the latest valid_from wins, then filed over reported, then the latest learned.
CREATE VIEW fact_current AS
SELECT DISTINCT ON (subject_type, subject_id, field) *
FROM fact
WHERE status = 'published' AND (valid_to IS NULL OR valid_to > current_date)
ORDER BY subject_type, subject_id, field,
         (tag = 'M'), valid_from DESC,
         (CASE tag WHEN 'F' THEN 0 WHEN 'R' THEN 1 ELSE 2 END), learned_at DESC, id DESC;

-- Point in time: what Marden knew on a date (backtests, track record, ?asOf reads).
CREATE FUNCTION fact_asof(as_of date)
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

-- ---------------------------------------------------------------- financed sites and chains
CREATE TABLE site (
  slug            text PRIMARY KEY,            -- 'jupiter', 'abilene'
  name            text NOT NULL,
  legacy_group    text UNIQUE,                 -- legacy grp key from the seed data, e.g. 'ABILENE'
  state           text,
  county          text,
  geom            geometry(Point, 4326),
  tracking_names  text[] NOT NULL DEFAULT '{}',
  in_v1           boolean NOT NULL DEFAULT false,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE chain_party (
  site_slug   text NOT NULL REFERENCES site(slug) ON DELETE CASCADE,
  role        text NOT NULL CHECK (role IN ('end_user', 'tenant', 'developer', 'lenders')),
  entity_id   text REFERENCES entity(id),
  label       text NOT NULL,                   -- party text shown in the chain ('Oracle', 'JPMorgan, MUFG')
  PRIMARY KEY (site_slug, role)
);

-- ---------------------------------------------------------------- data centers and on-site power (spec 17)
CREATE TABLE campus (
  id             text PRIMARY KEY,
  name           text NOT NULL,
  aliases        text[] NOT NULL DEFAULT '{}',
  county         text,
  state          text,
  geom           geometry(Point, 4326),
  parcel_ids     text[] NOT NULL DEFAULT '{}',
  first_seen_at  timestamptz NOT NULL DEFAULT now(),
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX campus_geom ON campus USING gist (geom);
CREATE INDEX campus_name_trgm ON campus USING gin (name gin_trgm_ops);

CREATE TABLE building (
  id          text PRIMARY KEY,
  campus_id   text NOT NULL REFERENCES campus(id) ON DELETE CASCADE,
  label       text NOT NULL,
  footprint   geometry(Polygon, 4326),
  parcel_ids  text[] NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX building_campus ON building (campus_id);
CREATE INDEX building_footprint ON building USING gist (footprint);

CREATE TABLE power_plant (
  id            text PRIMARY KEY,
  name          text NOT NULL,
  county        text,
  state         text,
  geom          geometry(Point, 4326),
  eia_plant_id  integer,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX power_plant_geom ON power_plant USING gist (geom);

CREATE TABLE power_unit (
  id          text PRIMARY KEY,
  plant_id    text NOT NULL REFERENCES power_plant(id) ON DELETE CASCADE,
  oem         text,
  model       text,
  count       integer NOT NULL DEFAULT 1,
  permit_ref  text
);

CREATE TABLE plant_serves (
  plant_id          text NOT NULL REFERENCES power_plant(id) ON DELETE CASCADE,
  campus_id         text NOT NULL REFERENCES campus(id) ON DELETE CASCADE,
  evidence_fact_id  bigint REFERENCES fact(id),
  PRIMARY KEY (plant_id, campus_id)
);

-- Financed site -> campus/buildings. Material: a person approves each link once.
CREATE TABLE site_link (
  site_slug         text NOT NULL REFERENCES site(slug) ON DELETE CASCADE,
  campus_id         text NOT NULL REFERENCES campus(id) ON DELETE CASCADE,
  building_ids      text[],                    -- null = all buildings on the campus
  evidence_fact_id  bigint REFERENCES fact(id),
  status            text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'approved', 'rejected')),
  approved_by       text,
  approved_at       timestamptz,
  PRIMARY KEY (site_slug, campus_id)
);

-- ---------------------------------------------------------------- instruments, contracts, obligations
CREATE TABLE instrument (
  id                text PRIMARY KEY,          -- legacy ids kept: 'EX-ABILENE-000', 'NP-BTDR-09175RAE0'
  name              text NOT NULL,
  kind              text NOT NULL,             -- construction_loan, term_loan, ddtl, bond, convert, abs, cmbs, lease, facility, muni, other
  issuer_entity_id  text REFERENCES entity(id),
  issuer_name       text NOT NULL,
  site_slug         text REFERENCES site(slug),
  cusip             text,
  isin              text,
  in_v1             boolean NOT NULL DEFAULT false,
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX instrument_cusip ON instrument (cusip) WHERE cusip IS NOT NULL;
CREATE INDEX instrument_site ON instrument (site_slug);
CREATE INDEX instrument_name_trgm ON instrument USING gin (name gin_trgm_ops);

-- Leases, compute contracts, PPAs, guarantees: one row per obligation; attributes are facts.
CREATE TABLE obligation (
  id              text PRIMARY KEY,
  type            text NOT NULL,               -- EdgeType in types.ts, plus 'Lease' kinds for the lease map
  from_entity_id  text NOT NULL REFERENCES entity(id),  -- who owes
  to_entity_id    text NOT NULL REFERENCES entity(id),  -- who is owed
  site_slug       text REFERENCES site(slug),
  campus_id       text REFERENCES campus(id),
  instrument_ids  text[] NOT NULL DEFAULT '{}',
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX obligation_from ON obligation (from_entity_id);
CREATE INDEX obligation_to ON obligation (to_entity_id);

-- Fund holdings and marks from N-PORT, BDC 10-Q/10-K schedules. Filed values (tag F).
CREATE TABLE holding (
  id                bigserial PRIMARY KEY,
  fund_entity_id    text NOT NULL REFERENCES entity(id),
  instrument_id     text REFERENCES instrument(id),   -- null until linked
  issuer_name       text,
  title             text,
  cusip             text,
  isin              text,
  period_end        date NOT NULL,
  filing_type       text NOT NULL,             -- 'N-PORT', '10-Q', '10-K'
  filing_url        text NOT NULL,
  accession         text NOT NULL,
  filed_at          date,
  par               numeric,
  value_usd         numeric,
  mark_price        numeric,                   -- percent of par
  pct_nav           numeric,
  fund_net_assets   numeric,
  non_accrual       boolean,
  document_id       bigint REFERENCES document(id),
  line_key          text NOT NULL,             -- stable key for the line within the filing
  learned_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (fund_entity_id, accession, line_key)
);
CREATE INDEX holding_instrument ON holding (instrument_id, period_end DESC);
CREATE INDEX holding_cusip ON holding (cusip) WHERE cusip IS NOT NULL;

-- Every observed or computed price: trades, quotes, fund marks, exchange prints, Marden prices.
CREATE TABLE price_obs (
  id             bigserial PRIMARY KEY,
  instrument_id  text NOT NULL REFERENCES instrument(id),
  obs_date       date NOT NULL,
  obs_at         timestamptz,
  kind           text NOT NULL CHECK (kind IN ('trade', 'quote', 'fund_mark', 'exchange', 'dealer', 'marden', 'legacy')),
  price          numeric,                      -- percent of par
  yield_pct      numeric,
  spread_bp      numeric,
  volume         numeric,
  tag            char(1) NOT NULL CHECK (tag IN ('F', 'R', 'M')),
  source_name    text NOT NULL,
  source_url     text,
  method_version text,
  document_id    bigint REFERENCES document(id),
  learned_at     timestamptz NOT NULL DEFAULT now(),
  dedupe_key     text NOT NULL UNIQUE
);
CREATE INDEX price_obs_instrument ON price_obs (instrument_id, obs_date DESC);

-- Dated events on any subject (permits, dockets, notices, ratings, prices). Material events go
-- through the review queue before they change a status or fire an alert.
CREATE TABLE event (
  id            bigserial PRIMARY KEY,
  subject_type  text NOT NULL,
  subject_id    text NOT NULL,
  event_date    date NOT NULL,
  kind          text NOT NULL,                 -- permit, docket, notice, rating, price, opposition, filing, imagery, milestone
  title         text NOT NULL,
  severity      text CHECK (severity IN ('red', 'amber', 'green', 'none')),
  rule_code     text,
  material      boolean NOT NULL DEFAULT false,
  tag           char(1) NOT NULL CHECK (tag IN ('F', 'R', 'M')),
  source_name   text NOT NULL,
  source_url    text,
  document_id   bigint REFERENCES document(id),
  fact_id       bigint REFERENCES fact(id),
  status        text NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate', 'published', 'rejected')),
  learned_at    timestamptz NOT NULL DEFAULT now(),
  dedupe_key    text NOT NULL UNIQUE
);
CREATE INDEX event_subject ON event (subject_type, subject_id, event_date DESC);

-- ---------------------------------------------------------------- models and read models
CREATE TABLE model_run (
  id           bigserial PRIMARY KEY,
  model        text NOT NULL,
  version      text NOT NULL,
  trigger      text NOT NULL,                  -- 'schedule', 'fact:123', 'manual'
  started_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz,
  ok           boolean,
  error        text,
  inputs_hash  text
);

CREATE TABLE model_output (
  id            bigserial PRIMARY KEY,
  run_id        bigint REFERENCES model_run(id),
  model         text NOT NULL,
  version       text NOT NULL,
  subject_type  text NOT NULL,
  subject_id    text NOT NULL,
  as_of         date NOT NULL,
  output        jsonb NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (model, version, subject_type, subject_id, as_of)
);
CREATE INDEX model_output_subject ON model_output (model, subject_type, subject_id, as_of DESC);

-- One precomputed document per screen piece; a page load is one indexed read.
-- Rows are kept per as_of day, so ?asOf reads come from the snapshot of that day.
CREATE TABLE read_model (
  view         text NOT NULL,                  -- 'completion', 'site_detail', 'dc_row', 'instrument_detail'
  key          text NOT NULL,                  -- 'all' for singletons, else the subject id
  as_of        date NOT NULL,
  doc          jsonb NOT NULL,
  built_at     timestamptz NOT NULL DEFAULT now(),
  inputs_hash  text,
  PRIMARY KEY (view, key, as_of)
);
CREATE INDEX read_model_latest ON read_model (view, as_of DESC);

CREATE TABLE read_model_dirty (
  view       text NOT NULL,
  key        text NOT NULL,
  marked_at  timestamptz NOT NULL DEFAULT now(),
  reason     text,
  PRIMARY KEY (view, key)
);

-- ---------------------------------------------------------------- review queue
CREATE TABLE review_item (
  id          bigserial PRIMARY KEY,
  kind        text NOT NULL CHECK (kind IN ('fact', 'event', 'link', 'merge', 'note', 'source')),
  ref_type    text NOT NULL,
  ref_id      text NOT NULL,
  title       text NOT NULL,
  reason      text NOT NULL,                   -- why it needs a person: material, verifier disagreed, conflict
  payload     jsonb NOT NULL DEFAULT '{}',
  status      text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'approved', 'rejected')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  decided_by  text,
  decided_at  timestamptz,
  UNIQUE (kind, ref_type, ref_id)
);
CREATE INDEX review_open ON review_item (status, created_at) WHERE status = 'open';

-- ---------------------------------------------------------------- live updates
-- Every stream event is stored, then announced with NOTIFY; the API replays from Last-Event-ID.
CREATE TABLE stream_event (
  id       bigserial PRIMARY KEY,
  at       timestamptz NOT NULL DEFAULT now(),
  org_id   text,                               -- null = everyone
  payload  jsonb NOT NULL                      -- a StreamEvent from types.ts
);

CREATE FUNCTION stream_event_notify() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('marden_stream', NEW.id::text);
  RETURN NEW;
END $$;
CREATE TRIGGER stream_event_notify AFTER INSERT ON stream_event
  FOR EACH ROW EXECUTE FUNCTION stream_event_notify();

-- ---------------------------------------------------------------- app: orgs, users, watchlists, alerts
CREATE TABLE org (
  id          text PRIMARY KEY,                -- Clerk organization id
  name        text NOT NULL,
  plan        text NOT NULL DEFAULT 'trial' CHECK (plan IN ('trial', 'design_partner', 'paid')),
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app_user (
  id          text PRIMARY KEY,                -- Clerk user id
  email       text NOT NULL,
  name        text,
  is_staff    boolean NOT NULL DEFAULT false,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE membership (
  org_id   text NOT NULL REFERENCES org(id) ON DELETE CASCADE,
  user_id  text NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  role     text NOT NULL CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
  PRIMARY KEY (org_id, user_id)
);

CREATE TABLE watchlist_item (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id      text NOT NULL REFERENCES org(id) ON DELETE CASCADE,
  user_id     text REFERENCES app_user(id) ON DELETE SET NULL,
  kind        text NOT NULL CHECK (kind IN ('site', 'instrument')),
  ref_id      text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, kind, ref_id)
);

CREATE TABLE alert (
  id            bigserial PRIMARY KEY,
  org_id        text NOT NULL REFERENCES org(id) ON DELETE CASCADE,
  at            timestamptz NOT NULL DEFAULT now(),
  kind          text NOT NULL,                 -- AlertSettings type key
  headline      text NOT NULL,
  tag           char(1) NOT NULL CHECK (tag IN ('F', 'R', 'M')),
  rule_label    text NOT NULL,
  rule_code     text,
  ref_url       text NOT NULL,
  subject_type  text NOT NULL,
  subject_id    text NOT NULL,
  event_id      bigint REFERENCES event(id),
  severity      text,
  read_at       timestamptz,
  emailed_at    timestamptz,
  UNIQUE (org_id, kind, subject_type, subject_id, event_id)
);
CREATE INDEX alert_org ON alert (org_id, at DESC);

CREATE TABLE alert_settings (
  org_id    text NOT NULL REFERENCES org(id) ON DELETE CASCADE,
  user_id   text NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  settings  jsonb NOT NULL,
  PRIMARY KEY (org_id, user_id)
);

CREATE TABLE home_layout (
  org_id   text NOT NULL REFERENCES org(id) ON DELETE CASCADE,
  user_id  text NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  layout   jsonb NOT NULL,
  PRIMARY KEY (org_id, user_id)
);

CREATE TABLE stress_saved (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id      text NOT NULL REFERENCES org(id) ON DELETE CASCADE,
  user_id     text REFERENCES app_user(id) ON DELETE SET NULL,
  name        text NOT NULL,
  request     jsonb NOT NULL,
  result      jsonb,
  ran_at      timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit_log (
  id       bigserial PRIMARY KEY,
  at       timestamptz NOT NULL DEFAULT now(),
  org_id   text,
  user_id  text,
  action   text NOT NULL,
  target   text,
  ip       text,
  meta     jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX audit_org ON audit_log (org_id, at DESC);

-- Append-only audit log.
CREATE FUNCTION audit_log_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only';
END $$;
CREATE TRIGGER audit_log_no_update BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();

-- migrate:down
DROP TABLE IF EXISTS audit_log, stress_saved, home_layout, alert_settings, alert, watchlist_item,
  membership, app_user, org, stream_event, review_item, read_model_dirty, read_model, model_output,
  model_run, event, price_obs, holding, obligation, instrument, site_link, plant_serves, power_unit,
  power_plant, building, campus, chain_party, site, fact, entity, document, source_run, source CASCADE;
DROP FUNCTION IF EXISTS audit_log_immutable, stream_event_notify, fact_asof(date);
