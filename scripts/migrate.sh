#!/bin/sh
# Apply db/migrations with dbmate: the one-shot `migrate` step. It is the Railway pre-deploy command
# of the api and worker services (installed as /usr/local/bin/marden-migrate in both images) and the
# way to migrate by hand. Safe to run repeatedly and concurrently:
#   - --migration-lock: a Postgres advisory lock, so a second run waits for the first and then finds
#     nothing to apply (the api and worker pre-deploys of one push may start together);
#   - runs on the direct endpoint, DATABASE_URL_UNPOOLED (else DATABASE_URL): PgBouncer's transaction
#     mode cannot hold the session lock that migrations need;
#   - strips channel_binding from the URL: Neon's strings carry channel_binding=require, which libpq
#     knows but dbmate's Go driver forwards to the server as a setting, and the connection fails;
#   - never prints the URL (it holds the password); dbmate gets it through its environment.
#
#   marden-migrate              apply pending migrations (dbmate migrate)
#   marden-migrate status       list applied and pending migrations
#   scripts/migrate.sh          the same from a checkout (migrations dir found next to this script)
#
# MARDEN_MIGRATIONS_DIR overrides the migrations directory (the images set /app/db/migrations).
set -eu

url="${DATABASE_URL_UNPOOLED:-${DATABASE_URL:-}}"
if [ -z "$url" ]; then
  echo "migrate: DATABASE_URL_UNPOOLED or DATABASE_URL must be set" >&2
  exit 2
fi

if [ -n "${MARDEN_MIGRATIONS_DIR:-}" ]; then
  dir="$MARDEN_MIGRATIONS_DIR"
else
  dir="$(cd "$(dirname "$0")/.." && pwd)/db/migrations"
fi
if [ ! -d "$dir" ]; then
  echo "migrate: migrations directory not found: $dir" >&2
  exit 2
fi

if ! command -v dbmate >/dev/null 2>&1; then
  echo "migrate: dbmate is not on PATH" >&2
  exit 2
fi

# Drop channel_binding=<value> wherever it sits in the query string, then a dangling ? or &.
url="$(printf '%s' "$url" | sed -E 's/([?&])channel_binding=[^&]*(&|$)/\1/g; s/[?&]$//')"

if [ "$#" -eq 0 ]; then
  set -- migrate
fi

DATABASE_URL="$url"
export DATABASE_URL
unset DATABASE_URL_UNPOOLED

echo "migrate: dbmate $* ($(ls "$dir" | grep -c '\.sql$') migration files in $dir)"
exec dbmate \
  --env DATABASE_URL \
  --migrations-dir "$dir" \
  --no-dump-schema \
  --wait --wait-timeout "${MARDEN_MIGRATE_WAIT:-60s}" \
  --migration-lock --migration-lock-timeout "${MARDEN_MIGRATE_LOCK_TIMEOUT:-10m}" \
  "$@"
