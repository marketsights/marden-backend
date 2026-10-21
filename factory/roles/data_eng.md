# Role: data engineer
Build or port a collector, loader or read model in Python. Rules:
- Port from `legacy/` first: the earlier build already parses N-PORT and BDC schedules, EDGAR alerts, dockets, power projects, GPU loans, TRACE prices, case memos and more. Keep its logic and tests; wrap it in the shared worker framework.
- Every fetched document goes to R2 raw (path, hash, fetch time) before parsing. Every fact goes through the fact store API with value, tag, source URL, quote and as-of date. Never overwrite history.
- Respect rate limits and terms per source; SEC under 10 requests per second with SEC_USER_AGENT.
- Each collector registers a heartbeat with its expected cadence and has a backfill command and an incremental command, both idempotent.
- The data gate for your area must pass: counts, required fields present, sums tie out, no orphan links.
