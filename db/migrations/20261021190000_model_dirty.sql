-- migrate:up
-- Model triggers (spec 22 "Model workers run when a published fact touches their inputs"): the fact,
-- event and price writers mark (model, key) here through marden.models.triggers when a published
-- value matches a model's input table; the `models.run_dirty` task runs the model for those keys and
-- deletes the marks it consumed (a mark newer than the run start stays for the next pass).
CREATE TABLE model_dirty (
  model      text NOT NULL,
  key        text NOT NULL,
  marked_at  timestamptz NOT NULL DEFAULT now(),
  reason     text,
  PRIMARY KEY (model, key)
);
CREATE INDEX model_dirty_marked ON model_dirty (model, marked_at);

-- migrate:down
DROP TABLE model_dirty;
