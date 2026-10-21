"""Models (spec 22 "The models"): Python computes every number; outputs are versioned rows.

Shared framework for every model module in this package (imported by `marden.plugins`):

- runs: `model_run` + `model_output` writes (`start_run`, `finish_run`, `write_output`), stored-output reads
- pit: point-in-time inputs (`fact_rows`, `event_rows`): what was public on the run's as-of day, with the
  publication lags of the sources and the unlicensed-price switch applied
- triggers: the model dependency table (`model_depends`), `model_dirty` marks made when a published fact or
  event touches a model's inputs, and the `models.run_dirty` task that runs the marked keys
- sourced: Sourced<T> values for model outputs ('model v0' label for uncalibrated values) and
  `run_outputs`, one stored run that marks `@model:<name>` and the model's own views on a live run
  (used by mark_gap, recovery and the GPU models)

A model module registers its CLI action (`python -m marden model <name>`), its procrastinate tasks (queue
'models') and its input dependencies at import time.
"""
