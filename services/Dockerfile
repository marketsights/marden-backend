# syntax=docker/dockerfile:1
# Marden worker image (services/): the Python package `marden` with its locked dependencies, run as a
# non-root user. One image, two commands:
#   python -m marden worker   (default) Procrastinate worker on every queue (collect, extract, models,
#                             readmodels, alerts, default) plus the periodic scheduler
#   marden-migrate            one-shot: dbmate applies db/migrations (Railway pre-deploy command)
# The build context is the repository root (db/migrations and scripts/ live there):
#   docker build -f services/Dockerfile -t marden-worker .

ARG PYTHON_IMAGE=python:3.13.16-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32

FROM ${UV_IMAGE} AS uv

# ---------------------------------------------------------------- locked virtualenv
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app/services
COPY services/pyproject.toml services/uv.lock ./
# Dependencies first (cached while only code changes), then the project itself. --frozen: the lockfile
# is the contract; a pyproject change without `uv lock` fails the build instead of resolving anew.
RUN uv sync --frozen --no-dev --no-install-project
COPY services/marden ./marden
RUN uv sync --frozen --no-dev && /app/.venv/bin/python -m compileall -q marden

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime
# libexpat1: the rasterio wheel's bundled GDAL links it (the slim image does not ship it).
# pg_dump/pg_restore for the nightly backup (marden.core.backup). The client must be at least the
# server's major version: Neon project `core` runs Postgres 18. PGDG's signing key is checked by
# fingerprint before apt trusts it.
ARG PG_CLIENT_MAJOR=18
RUN set -eux; \
    pgdg_fpr=B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg; \
    install -d /usr/share/postgresql-common/pgdg; \
    curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc; \
    gpg --show-keys --with-colons /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
      | grep -q "^fpr:::::::::${pgdg_fpr}:"; \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends libexpat1 "postgresql-client-${PG_CLIENT_MAJOR}"; \
    apt-get purge -y --auto-remove curl gnupg; \
    rm -rf /var/lib/apt/lists/*; \
    pg_dump --version

# dbmate 2.37.0 (linux amd64), checksum pinned: the GitHub release asset and the npm
# @dbmate/linux-x64 binary are byte-identical.
ADD --checksum=sha256:f855e99a0f0a81322014bf686b1cb820d2b4d8074e4eadafd64b851b80ecb15a --chmod=755 \
    https://github.com/amacneil/dbmate/releases/download/v2.37.0/dbmate-linux-amd64 /usr/local/bin/dbmate
COPY --chmod=755 scripts/migrate.sh /usr/local/bin/marden-migrate

RUN useradd --system --uid 10001 --user-group --create-home --home-dir /home/marden marden

# Same layout as the repository (/app = repo root) so marden.core.config.REPO_ROOT resolves.
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/services /app/services
COPY db/migrations /app/db/migrations

# MARDEN_LOG_FORMAT=json: one JSON object per log line, secrets redacted (marden.core.logs).
# MARDEN_RAW_DIR is only a scratch path: on Railway the raw store refuses local disk and needs R2.
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MARDEN_LOG_FORMAT=json \
    MARDEN_MIGRATIONS_DIR=/app/db/migrations \
    MARDEN_RAW_DIR=/tmp/marden-raw

USER marden
WORKDIR /app/services

# Exec form: python is PID 1 and gets SIGTERM; Procrastinate stops fetching, lets running jobs finish
# (MARDEN_WORKER_SHUTDOWN_TIMEOUT) and exits.
CMD ["python", "-m", "marden", "worker"]
