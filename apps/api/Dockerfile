# syntax=docker/dockerfile:1
# Marden API (apps/api): Fastify on Node 22, run as `node dist/server.js` by a non-root user.
# The build context is the repository root (the pnpm workspace lockfile and db/migrations live there):
#   docker build -f apps/api/Dockerfile -t marden-api .
# The image also carries dbmate and db/migrations, so Railway's pre-deploy command `marden-migrate`
# can apply migrations before a new API version takes traffic (docs/DEPLOY.md).

ARG NODE_IMAGE=node:22.23.4-bookworm-slim
ARG PNPM_VERSION=10.28.0

# ---------------------------------------------------------------- toolchain
FROM ${NODE_IMAGE} AS base
ARG PNPM_VERSION
ENV CI=true
RUN npm install -g "pnpm@${PNPM_VERSION}" && pnpm --version
WORKDIR /app
COPY pnpm-lock.yaml pnpm-workspace.yaml package.json ./
COPY apps/api/package.json apps/api/

# ---------------------------------------------------------------- compile TypeScript
FROM base AS build
RUN pnpm install --frozen-lockfile --filter @marden/api
COPY apps/api/tsconfig.json apps/api/tsconfig.build.json apps/api/
COPY apps/api/src apps/api/src
RUN pnpm --filter @marden/api build

# ---------------------------------------------------------------- production node_modules only
FROM base AS prod-deps
RUN pnpm install --frozen-lockfile --prod --filter @marden/api

# ---------------------------------------------------------------- runtime
FROM ${NODE_IMAGE} AS runtime
# dbmate 2.37.0 (linux amd64), checksum pinned: the GitHub release asset and the npm
# @dbmate/linux-x64 binary are byte-identical.
ADD --checksum=sha256:f855e99a0f0a81322014bf686b1cb820d2b4d8074e4eadafd64b851b80ecb15a --chmod=755 \
    https://github.com/amacneil/dbmate/releases/download/v2.37.0/dbmate-linux-amd64 /usr/local/bin/dbmate
COPY --chmod=755 scripts/migrate.sh /usr/local/bin/marden-migrate
COPY db/migrations /app/db/migrations

WORKDIR /app
COPY --from=prod-deps /app/node_modules /app/node_modules
COPY --from=prod-deps /app/apps/api/node_modules /app/apps/api/node_modules
COPY --from=build /app/apps/api/dist /app/apps/api/dist
COPY apps/api/package.json /app/apps/api/package.json

# HOST=:: listens on IPv6 and IPv4, so Railway's private network (web -> api.railway.internal) and
# its public edge both reach it. PORT is overridden by Railway.
ENV NODE_ENV=production \
    HOST=:: \
    PORT=4000 \
    MARDEN_MIGRATIONS_DIR=/app/db/migrations

# The image's `node` user (uid 1000) owns nothing here: the code is read-only to the process.
USER node
WORKDIR /app/apps/api
EXPOSE 4000

# Railway uses its own check (railway.json healthcheckPath); this one serves `docker run` and others.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:'+(process.env.PORT||4000)+'/healthz').then(r=>process.exit(r.ok?0:1),()=>process.exit(1))"]

# Exec form: node is PID 1 and receives SIGTERM itself (src/server.ts drains and exits).
CMD ["node", "--enable-source-maps", "dist/server.js"]
