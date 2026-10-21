-- migrate:up
-- Alert engine and email delivery (docs/methods/alerts-v0.1.md, specs 00 "Materiality and alerts", 11, 13).
--
-- alert                  one row per (org, user, matter): the engine fans a material change out to every
--                        member of every firm watching the affected site or instrument (directly or through
--                        the chain) whose product access is live and whose settings take that alert type.
--                        Read state is per user. Title and body are built by code templates from the
--                        computed numbers; `facts` holds those numbers, `sources` the fact refs.
-- alert_trigger          one row per evaluated trigger (a published event, a rating fact, a model output
--                        change), so a rerun evaluates nothing twice.
-- alert_state            the last model output the engine evaluated per (model, subject): the "before" of
--                        the next change (model outputs of one day are replaced in place).
-- email_outbox           every email the engine means to send: instant (red), daily and weekly digests.
--                        Idempotent by key; queued -> sent | failed | not_configured | cancelled (the user
--                        switched that email off, or the firm's product lapsed, before it went), retried
--                        with backoff.
-- email_provider_state   what the worker last saw of its email configuration (no secrets), for the
--                        Account screen's honest "not configured" state.
-- stream_event.user_id   a live event for one user only (their alert); null = the whole org scope.

-- The alert table had no writer before this engine; per-user rows cannot be derived from org rows.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM alert) THEN
    RAISE EXCEPTION 'alert has rows written before the alert engine; move or delete them, then migrate';
  END IF;
END $$;

ALTER TABLE alert DROP CONSTRAINT IF EXISTS alert_org_id_kind_subject_type_subject_id_event_id_key;
ALTER TABLE alert
  ADD COLUMN user_id      text NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  ADD COLUMN body         text NOT NULL,
  ADD COLUMN facts        jsonb NOT NULL DEFAULT '{}',
  ADD COLUMN sources      jsonb NOT NULL DEFAULT '[]',
  ADD COLUMN occurred_on  date NOT NULL,
  ADD COLUMN trigger_key  text NOT NULL,
  ADD COLUMN dedupe_key   text NOT NULL,
  ADD COLUMN backfill     boolean NOT NULL DEFAULT false;
ALTER TABLE alert ADD CONSTRAINT alert_severity_check CHECK (severity IN ('red', 'amber', 'green'));
ALTER TABLE alert ALTER COLUMN severity SET NOT NULL;
ALTER TABLE alert ADD CONSTRAINT alert_user_dedupe UNIQUE (org_id, user_id, dedupe_key);
ALTER TABLE alert ADD CONSTRAINT alert_member FOREIGN KEY (org_id, user_id)
  REFERENCES membership(org_id, user_id) ON DELETE CASCADE;
DROP INDEX IF EXISTS alert_org;
CREATE INDEX alert_user ON alert (org_id, user_id, at DESC);
CREATE INDEX alert_matter ON alert (org_id, user_id, subject_type, subject_id, rule_code, occurred_on DESC);

CREATE TABLE alert_trigger (
  trigger_key   text PRIMARY KEY,               -- 'event:<id>', 'fact:<id>', 'model:<model>:<type>:<id>:<as_of>:<hash>'
  kind          text NOT NULL CHECK (kind IN ('event', 'fact', 'model')),
  subject_type  text NOT NULL,
  subject_id    text NOT NULL,
  occurred_on   date NOT NULL,
  material      boolean NOT NULL,
  codes         text[] NOT NULL DEFAULT '{}',
  alerts        integer NOT NULL DEFAULT 0,     -- alert rows created (all users)
  dry_run       boolean NOT NULL DEFAULT false,
  evaluated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX alert_trigger_subject ON alert_trigger (subject_type, subject_id, occurred_on DESC);

CREATE TABLE alert_state (
  model         text NOT NULL,
  subject_type  text NOT NULL,
  subject_id    text NOT NULL,
  as_of         date NOT NULL,
  output_hash   text NOT NULL,
  state         jsonb NOT NULL,
  evaluated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (model, subject_type, subject_id)
);

CREATE TABLE email_outbox (
  id                   bigserial PRIMARY KEY,
  org_id               text NOT NULL REFERENCES org(id) ON DELETE CASCADE,
  user_id              text NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  kind                 text NOT NULL CHECK (kind IN ('instant', 'daily_digest', 'weekly_digest')),
  idempotency_key      text NOT NULL UNIQUE,
  to_email             text NOT NULL,
  subject              text NOT NULL,
  html                 text,                    -- rendered when sent (links need the delivery settings)
  text_body            text,
  headers              jsonb NOT NULL DEFAULT '{}',
  alert_ids            bigint[] NOT NULL DEFAULT '{}',
  status               text NOT NULL DEFAULT 'queued'
                         CHECK (status IN ('queued', 'sent', 'failed', 'not_configured', 'cancelled')),
  provider             text,
  provider_message_id  text,
  attempts             integer NOT NULL DEFAULT 0,
  next_attempt_at      timestamptz NOT NULL DEFAULT now(),
  last_error           text,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  sent_at              timestamptz
);
CREATE INDEX email_outbox_due ON email_outbox (next_attempt_at) WHERE status = 'queued';
CREATE INDEX email_outbox_user ON email_outbox (org_id, user_id, created_at DESC);

CREATE TABLE email_provider_state (
  id           text PRIMARY KEY,                -- 'alerts'
  provider     text,                            -- 'resend' | 'postmark' | null
  configured   boolean NOT NULL,
  from_email   text,
  reason       text,                            -- what is missing, by variable name (never a value)
  checked_at   timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE stream_event ADD COLUMN user_id text;

-- migrate:down
ALTER TABLE stream_event DROP COLUMN IF EXISTS user_id;
DROP TABLE IF EXISTS email_provider_state, email_outbox, alert_state, alert_trigger;
DELETE FROM alert;
DROP INDEX IF EXISTS alert_matter;
DROP INDEX IF EXISTS alert_user;
ALTER TABLE alert DROP CONSTRAINT IF EXISTS alert_member;
ALTER TABLE alert DROP CONSTRAINT IF EXISTS alert_user_dedupe;
ALTER TABLE alert DROP CONSTRAINT IF EXISTS alert_severity_check;
ALTER TABLE alert ALTER COLUMN severity DROP NOT NULL;
ALTER TABLE alert
  DROP COLUMN IF EXISTS user_id, DROP COLUMN IF EXISTS body, DROP COLUMN IF EXISTS facts,
  DROP COLUMN IF EXISTS sources, DROP COLUMN IF EXISTS occurred_on, DROP COLUMN IF EXISTS trigger_key,
  DROP COLUMN IF EXISTS dedupe_key, DROP COLUMN IF EXISTS backfill;
ALTER TABLE alert ADD CONSTRAINT alert_org_id_kind_subject_type_subject_id_event_id_key
  UNIQUE (org_id, kind, subject_type, subject_id, event_id);
CREATE INDEX alert_org ON alert (org_id, at DESC);
