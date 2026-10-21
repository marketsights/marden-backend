# 12 · Track record and methods (`/track-record`)

**Job:** why a fund should trust Marden: when each repricing was flagged vs when the price moved, the fixed rules behind every status, model versions, and source health.
**Reference:** screens/12-track-record.html · **Endpoints:** `GET /api/track-record`, `GET /api/sources/health`, `GET /api/methods`

## Layout

1. Header with "Download method PDF".
2. **Lead-time chart:** one row per past repricing: first Marden flag (from the dated snapshot) → price below 95 or spread +200bp; label days. Cases still being backtested show a dashed line "backtest running". Header: "{n} of 15 past repricings published".
3. Chain status rules table (Link · Amber when · Red when) and "What counts as material" list.
4. Models table (output, version, built from, cadence) and Sources table (source, cadence, last run ET, status OK / delayed / down).

## Acceptance

- Flag dates come only from snapshots taken at the time; no backfilled dates.
- Sources table is live from the collector heartbeats.
