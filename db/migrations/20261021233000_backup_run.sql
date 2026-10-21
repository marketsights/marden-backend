-- migrate:up
-- One row per database backup attempt (marden.core.backup, task ops.backup_db, nightly). The dump
-- itself is an object in the R2 bucket R2_BUCKET_BACKUPS (default core-backups); this table is the
-- index and the record of what failed or was skipped. Rows are never updated after they finish.
--   status  running         dump/upload in progress (a row left running by a killed worker is closed
--                           as 'failed' by the next run)
--           ok              object_key holds a complete pg_dump custom-format archive of `bytes` bytes
--           failed          detail says why (secrets redacted); no object was written
--           not_configured  R2 credentials are missing; nothing was dumped (detail names the variables)
CREATE TABLE backup_run (
  id               bigserial PRIMARY KEY,
  started_at       timestamptz NOT NULL DEFAULT now(),
  finished_at      timestamptz,
  status           text NOT NULL CHECK (status IN ('running', 'ok', 'failed', 'not_configured')),
  database         text,                       -- the dumped database's name (never the URL)
  bucket           text,
  object_key       text,
  bytes            bigint CHECK (bytes IS NULL OR bytes >= 0),
  sha256           text CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'),
  pg_dump_version  text,
  detail           text,
  CHECK (status <> 'ok' OR (object_key IS NOT NULL AND bytes IS NOT NULL AND sha256 IS NOT NULL AND finished_at IS NOT NULL))
);
CREATE INDEX backup_run_started_idx ON backup_run (started_at DESC);
CREATE UNIQUE INDEX backup_run_object_key_idx ON backup_run (bucket, object_key) WHERE object_key IS NOT NULL;

-- migrate:down
DROP TABLE IF EXISTS backup_run;
