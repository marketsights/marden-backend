#!/bin/sh
# One-off production refresh (Oct 2026): load the curated facts, collect, backfill daily price/yield since Jul 1, run models, rebuild.
# Run on core-worker from /app/services:  sh marden/ops_scripts/prod_refresh_2026_10.sh
set -x
M="python -m marden"
$M facilities proceedings marden/facilities/specs/court_cases_2026-10-22.json
$M facilities terms marden/facilities/specs/instrument_terms_2026-10-22.json
for b in 1 2 3; do python marden/seed/leases_2026_10/load_leases.py marden/seed/leases_2026_10/batch$b.json --write; done
$M collect boerse_frankfurt --force
$M collect sec_nport_terms
$M collect naip
$M collect sentinel2-buildings
$M model recovery --no-mark
for d in $(python -c "import datetime as t
d=t.date(2026,7,1); e=t.date.today()
while d<=e:
    print(d) if d.weekday()<5 else None; d+=t.timedelta(1)"); do $M model price --as-of $d --no-mark && $M model yield --as-of $d --no-mark; done
for m in leases capacity mark_gap obligations obligation_company payer_capacity gpu_collateral status stress risk_list event_impact moves; do $M model $m; done
$M rebuild all
echo PROD_JOB_DONE
