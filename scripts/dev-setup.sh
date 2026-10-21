#!/usr/bin/env bash
# Idempotent local setup: Postgres 16 + PostGIS, the marden role and database, migrations,
# the Python environment and (if present) the API's node modules. Safe to run repeatedly.
#
#   scripts/dev-setup.sh [--skip-node]
#
# Uses DATABASE_URL when set; otherwise the local development database
#   postgres://marden:marden@localhost:5432/marden?sslmode=disable
# (local-only credentials, documented in docs/ARCHITECTURE.md; not a secret).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_URL="postgres://marden:marden@localhost:5432/marden?sslmode=disable"
export DATABASE_URL="${DATABASE_URL:-$LOCAL_URL}"
PG_VERSION="${PG_VERSION:-16}"
PG_CLUSTER="${PG_CLUSTER:-main}"
DB_ROLE="marden"
DB_NAME="marden"

SKIP_NODE=0
for arg in "$@"; do
  case "$arg" in
    --skip-node) SKIP_NODE=1 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '==> %s\n' "$*"; }

as_postgres() {
  if [ "$(id -u)" = "0" ]; then
    runuser -u postgres -- "$@"
  elif [ "$(id -un)" = "postgres" ]; then
    "$@"
  else
    sudo -u postgres "$@"
  fi
}

is_local_db() {
  case "$DATABASE_URL" in
    *@localhost:*|*@localhost/*|*@127.0.0.1:*|*@127.0.0.1/*) return 0 ;;
    *) return 1 ;;
  esac
}

if is_local_db; then
  # ---------------------------------------------------------------- Postgres running
  if ! pg_isready -h localhost -p 5432 -q; then
    log "starting Postgres $PG_VERSION/$PG_CLUSTER"
    if command -v pg_ctlcluster >/dev/null 2>&1; then
      setsid pg_ctlcluster "$PG_VERSION" "$PG_CLUSTER" start
    else
      echo "Postgres is not running and pg_ctlcluster is not available; start Postgres $PG_VERSION manually." >&2
      exit 1
    fi
    for _ in $(seq 1 30); do
      pg_isready -h localhost -p 5432 -q && break
      sleep 1
    done
    pg_isready -h localhost -p 5432 -q || { echo "Postgres did not become ready" >&2; exit 1; }
  else
    log "Postgres is running"
  fi

  # ---------------------------------------------------------------- role and database
  if [ "$(as_postgres psql -XtAc "SELECT 1 FROM pg_roles WHERE rolname = '$DB_ROLE'")" != "1" ]; then
    # Superuser locally: the test suite creates throwaway databases with PostGIS (an untrusted extension).
    log "creating role $DB_ROLE"
    as_postgres psql -Xq -c "CREATE ROLE $DB_ROLE LOGIN SUPERUSER PASSWORD 'marden'"
  else
    log "role $DB_ROLE exists"
  fi
  if [ "$(as_postgres psql -XtAc "SELECT 1 FROM pg_database WHERE datname = '$DB_NAME'")" != "1" ]; then
    log "creating database $DB_NAME"
    as_postgres createdb -O "$DB_ROLE" "$DB_NAME"
  else
    log "database $DB_NAME exists"
  fi
  as_postgres psql -Xq -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS postgis"
  log "postgis extension present"
else
  log "DATABASE_URL is not local; skipping Postgres, role and database setup"
fi

# ---------------------------------------------------------------- migrations
if ! command -v dbmate >/dev/null 2>&1; then
  echo "dbmate is not on PATH (https://github.com/amacneil/dbmate)" >&2
  exit 1
fi
log "applying migrations"
# Migrations need a session (no PgBouncer), so on Neon they run on the direct endpoint.
(cd "$ROOT" && dbmate --url "${DATABASE_URL_UNPOOLED:-$DATABASE_URL}" \
  --migrations-dir db/migrations --schema-file db/schema.sql up)

# ---------------------------------------------------------------- Python services
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not on PATH (https://docs.astral.sh/uv/)" >&2
  exit 1
fi
log "uv sync (services)"
(cd "$ROOT/services" && uv sync)

# ---------------------------------------------------------------- API
if [ "$SKIP_NODE" = "1" ]; then
  log "skipping node install (--skip-node)"
elif [ -f "$ROOT/apps/api/package.json" ]; then
  if command -v pnpm >/dev/null 2>&1; then
    log "pnpm install (apps/api)"
    (cd "$ROOT/apps/api" && pnpm install)
  else
    echo "apps/api exists but pnpm is not on PATH; skipping node install" >&2
  fi
fi

log "done. export DATABASE_URL=\"$DATABASE_URL\" then: cd services && uv run python -m marden sources"
