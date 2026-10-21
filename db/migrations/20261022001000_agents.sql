-- migrate:up
-- The agent system (spec 22 "The agents" and "How every number stays accurate"; docs/agents.md).
--
-- agent_status     what the worker last saw of its agent configuration (backend api | cli | not_configured,
--                  models; never a secret), for honest "not configured" answers from the API.
-- agent_job        one per (document, prompt version): queued -> running -> published | needs_review |
--                  failed | skipped | budget. Idempotent: the same document under the same prompt version
--                  is processed once; identical bytes under another url reuse every cached model call.
-- agent_call       every model call with its token use, keyed by a hash of the whole request (model,
--                  prompts, document bytes), so a rerun or a duplicate document costs nothing.
-- agent_candidate  every candidate fact an extractor proposed, with its code checks, the verifier's verdict
--                  and what happened to it (published, review, rejected, duplicate).
-- agent_link       proposed links from a mention in a document to an entity, site, instrument, ... with
--                  evidence; accepted automatically only on exact identifier matches.
-- agent_upload     files staff upload through the API (POST /api/documents) until the worker stores them in
--                  the raw store (R2) and as a `document`; the bytes are cleared once stored.
-- agent_decision   staff decisions on review items (approve, reject, edit) recorded by the API and applied
--                  by the worker, which publishes through the fact writer (read models and models marked dirty).
-- agent_batch      Message Batches API submissions for backfills (half price), polled until they end.
-- agent_inbox      R2 inbox/ objects already ingested (by key and etag).
-- agent_file       PDFs uploaded once to the Anthropic Files API and reused by file id.
-- Triggers NOTIFY 'marden_agents' when a document is queued for reading, an upload arrives or a decision is
-- made, so a listening dispatcher starts at once; the periodic sweep is the backstop.

CREATE TABLE agent_status (
  id          boolean PRIMARY KEY DEFAULT true CHECK (id),
  backend     text NOT NULL CHECK (backend IN ('api', 'cli', 'not_configured')),
  detail      text,
  models      jsonb NOT NULL DEFAULT '{}',
  checked_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE agent_job (
  id              bigserial PRIMARY KEY,
  document_id     bigint NOT NULL REFERENCES document(id),
  sha256          text NOT NULL,
  source_id       text NOT NULL,
  prompt_version  text NOT NULL,
  state           text NOT NULL DEFAULT 'queued' CHECK (state IN (
                    'queued', 'dispatched', 'running', 'published', 'needs_review', 'failed', 'skipped', 'budget')),
  stage           text NOT NULL DEFAULT 'queued' CHECK (stage IN (
                    'queued', 'routed', 'extracted', 'verified', 'linked', 'published')),
  mode            text NOT NULL DEFAULT 'live' CHECK (mode IN ('live', 'batch')),
  priority        integer NOT NULL DEFAULT 50,
  priority_reason text,
  attempts        integer NOT NULL DEFAULT 0,
  route           jsonb,                       -- the router's classification (areas, kind, scanned, sections)
  areas           text[] NOT NULL DEFAULT '{}',
  links           jsonb NOT NULL DEFAULT '{}',  -- subjects given at intake (an upload's site / instrument / entity)
  stats           jsonb NOT NULL DEFAULT '{}',  -- candidates, published, review, rejected, tokens
  error           text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  started_at      timestamptz,
  finished_at     timestamptz,
  UNIQUE (document_id, prompt_version)
);
CREATE INDEX agent_job_ready ON agent_job (priority DESC, created_at) WHERE state = 'queued';
CREATE INDEX agent_job_state ON agent_job (state, updated_at DESC);
CREATE INDEX agent_job_sha ON agent_job (sha256);

CREATE TABLE agent_call (
  id                  bigserial PRIMARY KEY,
  request_hash        text NOT NULL UNIQUE,
  agent               text NOT NULL,           -- 'router', 'extractor:<area>', 'verifier', 'linker'
  backend             text NOT NULL CHECK (backend IN ('api', 'cli', 'batch')),
  model               text NOT NULL,
  prompt_version      text NOT NULL,
  job_id              bigint REFERENCES agent_job(id) ON DELETE SET NULL,
  document_sha256     text,
  section             text,
  response            jsonb,                   -- the structured tool input only (never the prompt)
  stop_reason         text,
  input_tokens        integer NOT NULL DEFAULT 0,
  output_tokens       integer NOT NULL DEFAULT 0,
  cache_read_tokens   integer NOT NULL DEFAULT 0,
  cache_write_tokens  integer NOT NULL DEFAULT 0,
  cost_usd            numeric,
  batch_id            text,
  error               text,
  created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX agent_call_day ON agent_call (created_at);
CREATE INDEX agent_call_job ON agent_call (job_id);

CREATE TABLE agent_candidate (
  id              bigserial PRIMARY KEY,
  job_id          bigint NOT NULL REFERENCES agent_job(id) ON DELETE CASCADE,
  document_id     bigint NOT NULL REFERENCES document(id),
  area            text NOT NULL,
  section         text,
  subject_kind    text NOT NULL,
  subject_mention text NOT NULL,
  subject_type    text,                        -- resolved by the linker (null: no accepted link)
  subject_id      text,
  link_id         bigint,
  field           text NOT NULL,
  value           jsonb NOT NULL,
  unit            text,
  as_of           date,
  quote           text NOT NULL,
  quote_loc       text,
  page            integer,
  confidence      real,
  checks          jsonb NOT NULL DEFAULT '{}',  -- code validator results
  verdict         text CHECK (verdict IN ('agree', 'disagree', 'unsupported')),
  verdict_reason  text,
  material        boolean NOT NULL DEFAULT false,
  material_codes  text[] NOT NULL DEFAULT '{}',
  fact_id         bigint REFERENCES fact(id),
  outcome         text NOT NULL DEFAULT 'pending' CHECK (outcome IN (
                    'pending', 'published', 'review', 'rejected', 'duplicate')),
  outcome_reason  text,
  dedupe_key      text NOT NULL UNIQUE,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX agent_candidate_job ON agent_candidate (job_id);
CREATE INDEX agent_candidate_fact ON agent_candidate (fact_id) WHERE fact_id IS NOT NULL;

CREATE TABLE agent_link (
  id              bigserial PRIMARY KEY,
  document_id     bigint REFERENCES document(id),
  job_id          bigint REFERENCES agent_job(id) ON DELETE SET NULL,
  mention         text NOT NULL,
  mention_key     text NOT NULL,               -- normalized mention (lower case, punctuation folded)
  subject_type    text NOT NULL,
  subject_id      text NOT NULL,
  relation        text NOT NULL DEFAULT 'is',  -- 'is', 'spv_of', 'borrower_of', 'tenant_of', 'site_of', ...
  related_type    text,                        -- for relations: the other end (a site, entity, instrument)
  related_id      text,
  evidence        text,                        -- the exact passage that supports the link
  method          text NOT NULL CHECK (method IN ('identifier', 'upload', 'confirmed', 'name', 'fuzzy', 'agent')),
  score           real,
  status          text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'accepted', 'rejected')),
  decided_by      text,
  decided_at      timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE NULLS NOT DISTINCT (mention_key, subject_type, subject_id, relation, related_type, related_id)
);
CREATE INDEX agent_link_mention ON agent_link (mention_key) WHERE status = 'accepted';

CREATE TABLE agent_upload (
  id            bigserial PRIMARY KEY,
  filename      text NOT NULL,
  content_type  text NOT NULL,
  kind          text NOT NULL CHECK (kind IN ('pdf', 'docx', 'html', 'txt', 'xlsx')),
  size          bigint NOT NULL,
  sha256        text NOT NULL,
  data          bytea,                         -- cleared once stored in the raw store
  uploaded_by   text NOT NULL,
  org_id        text,
  links         jsonb NOT NULL DEFAULT '{}',   -- {site, instrument, entity}: the subjects staff tied it to
  filed         boolean NOT NULL DEFAULT false, -- an official filing or record (tag F) vs press / other (R)
  source_url    text,                          -- where the file came from, when known
  title         text,
  doc_date      date,
  status        text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'stored', 'failed')),
  document_id   bigint REFERENCES document(id),
  error         text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  stored_at     timestamptz
);
CREATE INDEX agent_upload_pending ON agent_upload (created_at) WHERE status = 'pending';

CREATE TABLE agent_decision (
  id              bigserial PRIMARY KEY,
  review_item_id  bigint NOT NULL REFERENCES review_item(id),
  action          text NOT NULL CHECK (action IN ('approve', 'reject', 'edit')),
  decided_by      text NOT NULL,
  reason          text,
  edit            jsonb,                       -- {value, unit, as_of, quote, quote_loc} for 'edit'
  created_at      timestamptz NOT NULL DEFAULT now(),
  applied_at      timestamptz,
  result          jsonb,
  error           text
);
CREATE INDEX agent_decision_pending ON agent_decision (created_at) WHERE applied_at IS NULL;
-- One open decision per review item: a second click waits for the first to be applied.
CREATE UNIQUE INDEX agent_decision_one_open ON agent_decision (review_item_id) WHERE applied_at IS NULL;

CREATE TABLE agent_batch (
  id            text PRIMARY KEY,              -- the Message Batches API id
  stage         text NOT NULL,                 -- 'route', 'extract', 'verify'
  request_count integer NOT NULL,
  custom_ids    jsonb NOT NULL,                -- custom_id -> {job_id, agent, section, request_hash}
  status        text NOT NULL DEFAULT 'in_progress' CHECK (status IN ('in_progress', 'ended', 'applied', 'failed')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  ended_at      timestamptz,
  applied_at    timestamptz,
  error         text
);

CREATE TABLE agent_inbox (
  key          text PRIMARY KEY,
  etag         text NOT NULL,
  document_id  bigint REFERENCES document(id),
  seen_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE agent_file (
  sha256       text PRIMARY KEY,
  file_id      text NOT NULL,
  bytes        bigint NOT NULL,
  uploaded_at  timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION agent_notify() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('marden_agents', TG_TABLE_NAME || ':' || NEW.id::text);
  RETURN NEW;
END $$;

CREATE TRIGGER document_agent_notify AFTER INSERT OR UPDATE OF extract_state ON document
  FOR EACH ROW WHEN (NEW.extract_state = 'queued') EXECUTE FUNCTION agent_notify();
CREATE TRIGGER agent_upload_notify AFTER INSERT ON agent_upload
  FOR EACH ROW EXECUTE FUNCTION agent_notify();
CREATE TRIGGER agent_decision_notify AFTER INSERT ON agent_decision
  FOR EACH ROW EXECUTE FUNCTION agent_notify();

-- migrate:down
DROP TRIGGER IF EXISTS document_agent_notify ON document;
DROP TABLE IF EXISTS agent_file, agent_inbox, agent_batch, agent_decision, agent_upload, agent_link,
  agent_candidate, agent_call, agent_job, agent_status CASCADE;
DROP FUNCTION IF EXISTS agent_notify();
