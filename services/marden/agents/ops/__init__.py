"""The operations agents (spec 22 "The agents"): Researcher, Watchdog and Note writer (docs/agents.md).

    researcher   weekly: the fields still not disclosed on watched and financed subjects (the read models'
                 n/d checklist items, the models' missing inputs, instrument terms without a value) are
                 searched for in EDGAR full-text search and FERC eLibrary search; new documents are stored
                 raw and handed to the intake for the extractor; every search, find and queued document is
                 recorded (research_search, research_find, agent_ops_run)
    watchdog     daily: code checks sums, contradictions between sources and between screens, stale sources,
                 numbers that moved without an input changing and read models older than their inputs; each
                 finding is a review item (kind 'issue') with its evidence; a model may only rephrase titles
    note_writer  weekly: the risk list's figures are filled by code into placeholders; a model writes the
                 words around them; a validator rejects any number, number word or name the figures do not
                 hold; the draft waits for a person; the approved note is what GET /api/risk-list serves

Modules: runs (agent_ops_run); researcher with gaps (the worklist), research_plan (queries, windows, the
relevance check), search_edgar / search_ferc (the endpoints), research_store, research_sources (collector
coverage); watchdog with watch_checks (sums, contradictions, screens), watch_staleness (sources, read models,
model backlogs), watch_moves (unexplained moves), watch_titles (the only model call: rewording titles); note
with note_prose and numbers (the prose validator); decisions (review-queue decisions on notes and issues);
tasks (schedules); commands (CLI).
"""
