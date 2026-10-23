#!/bin/sh
# One-off production refresh (Oct 23 2026): load the chain-card, payer and deal-party facts, rerun the models
# whose methods changed (price v0.3 issuer recovery, capacity reasons, year-only cushion ranges), rebuild.
# Run on core-worker from /app/services:  sh marden/ops_scripts/prod_refresh_2026_10b.sh
set -x
M="python -m marden"
python marden/seed/chains_2026_10/load_chains.py marden/seed/chains_2026_10/facts.json --write
python marden/seed/payers_2026_10/load_payers.py --write
$M facilities parties marden/facilities/specs/instrument_parties_2026-10-22.json --write
for m in recovery price yield cushion capacity mark_gap payer_capacity status event_impact moves stress risk_list; do $M model $m; done
$M rebuild all
echo PROD_JOB_DONE
