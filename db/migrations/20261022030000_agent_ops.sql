-- migrate:up
-- The operations agents (spec 22 "The agents": Researcher, Watchdog, Note writer; docs/agents.md).
--
-- agent_ops_run    one row per researcher / watchdog / note-writer run: what it did, as a report (searches,
--                  finds, queued documents; checks and issues; the draft), and whether its model calls were
--                  configured. Never a secret.
-- research_search  every search the researcher made: endpoint (EDGAR full-text search, FERC eLibrary search),
--                  the exact query and window, the subjects and the gaps it searched for, and the stored raw
--                  response (document_id). A blocked or failed search is recorded with its error.
-- research_find    each hit of a search and what happened to it: queued for the extractor (document stored
--                  raw, agent job), already stored or read, skipped (with why) or failed.
-- weekly_note      the weekly note drafts (RiskList.note): every figure filled by code (figures), the prose
--                  template with placeholders (template), the validator's results (checks), the filled note
--                  (note). A draft waits for a person (review_item kind 'note'); once approved it is the note
--                  GET /api/risk-list serves for its week. One approved and one open draft per week at most.
-- review_item.kind 'issue': a watchdog finding (a sum that does not add, a contradiction between sources or
--                  screens, a stale source, a number that moved without a cause, a read model older than its
--                  inputs) with its evidence; ref_type 'watchdog:<check>', ref_id the issue's key.

CREATE TABLE agent_ops_run (
  id           bigserial PRIMARY KEY,
  role         text NOT NULL CHECK (role IN ('researcher', 'watchdog', 'note_writer')),
  trigger      text NOT NULL DEFAULT 'manual',
  started_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz,
  status       text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'partial', 'failed')),
  model_status text,                         -- 'api' | 'cli' | 'not_configured' | 'not_used'
  detail       text,
  report       jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX agent_ops_run_role ON agent_ops_run (role, started_at DESC);

CREATE TABLE research_search (
  id           bigserial PRIMARY KEY,
  run_id       bigint REFERENCES agent_ops_run(id) ON DELETE SET NULL,
  endpoint     text NOT NULL CHECK (endpoint IN ('edgar_fts', 'ferc_elibrary')),
  query        text NOT NULL,
  window_from  date,
  window_to    date,
  subjects     jsonb NOT NULL DEFAULT '[]',   -- [{type, id}] the gaps belong to
  gaps         jsonb NOT NULL DEFAULT '[]',   -- the not-disclosed items it searched for
  document_id  bigint REFERENCES document(id), -- the raw response as stored
  hits         integer,
  status       text NOT NULL CHECK (status IN ('ok', 'failed', 'blocked')),
  error        text,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX research_search_run ON research_search (run_id);
CREATE INDEX research_search_query ON research_search (endpoint, query, created_at DESC);

CREATE TABLE research_find (
  id           bigserial PRIMARY KEY,
  search_id    bigint NOT NULL REFERENCES research_search(id) ON DELETE CASCADE,
  url          text NOT NULL,
  title        text,
  form         text,
  filed        date,
  outcome      text NOT NULL CHECK (outcome IN ('queued', 'already_stored', 'already_read', 'skipped', 'failed')),
  reason       text,
  document_id  bigint REFERENCES document(id),
  job_id       bigint REFERENCES agent_job(id) ON DELETE SET NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (search_id, url)
);
CREATE INDEX research_find_url ON research_find (url);

CREATE TABLE weekly_note (
  id               bigserial PRIMARY KEY,
  week             date NOT NULL,              -- the Monday of the risk list it was drafted from
  version          integer NOT NULL,
  status           text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'rejected', 'superseded')),
  writer           text NOT NULL CHECK (writer IN ('model', 'code')),
  note             jsonb NOT NULL,             -- RiskList.note: {title, paragraphs: [{lead, text, refs}]}
  figures          jsonb NOT NULL,             -- per paragraph: every figure as code formatted it, with its ref
  template         jsonb NOT NULL,             -- the prose with placeholders, before code filled the figures
  checks           jsonb NOT NULL DEFAULT '{}',
  source_output_id bigint REFERENCES model_output(id) ON DELETE SET NULL,
  inputs_hash      text NOT NULL,
  model            text,
  call_id          bigint REFERENCES agent_call(id) ON DELETE SET NULL,
  run_id           bigint REFERENCES agent_ops_run(id) ON DELETE SET NULL,
  review_item_id   bigint REFERENCES review_item(id),
  decided_by       text,
  decided_at       timestamptz,
  reason           text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (week, version)
);
CREATE UNIQUE INDEX weekly_note_one_approved ON weekly_note (week) WHERE status = 'approved';
CREATE UNIQUE INDEX weekly_note_one_draft ON weekly_note (week) WHERE status = 'draft';

ALTER TABLE review_item DROP CONSTRAINT review_item_kind_check;
ALTER TABLE review_item ADD CONSTRAINT review_item_kind_check
  CHECK (kind IN ('fact', 'event', 'link', 'merge', 'note', 'source', 'issue'));

-- migrate:down
DELETE FROM review_item WHERE kind = 'issue';
ALTER TABLE review_item DROP CONSTRAINT review_item_kind_check;
ALTER TABLE review_item ADD CONSTRAINT review_item_kind_check
  CHECK (kind IN ('fact', 'event', 'link', 'merge', 'note', 'source'));
DROP TABLE IF EXISTS weekly_note, research_find, research_search, agent_ops_run CASCADE;
