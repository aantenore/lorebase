# syntax=docker/dockerfile:1.7
#
# The web app (apps/web): the production build served by an unprivileged nginx on port 8080.
#
#   docker build -f deploy/docker/web.Dockerfile -t lorebase-web .
#   docker run --rm -p 8080:8080 lorebase-web
#
# Configuration. The app reads /config.js (window.__LOREBASE_CONFIG__) when it starts, over the
# Vite variables baked in at build time, so one image works against any Keeper:
#
#   docker run --rm -p 8080:8080 -e VITE_API_MODE=live \
#     -e VITE_KEEPER_BASE_URL=https://keeper.example.com lorebase-web
#
# The entrypoint script writes /config.js from the VITE_* variables of the container environment;
# a mounted config.js (as the Helm chart does) works too.
#
# The entrypoint script also writes the security headers. The Content-Security-Policy allows the
# origin of VITE_KEEPER_BASE_URL (read at run time, not the build argument below) for connections;
# LOREBASE_WEB_CSP_CONNECT_SRC adds sources, LOREBASE_WEB_CSP replaces the whole policy and
# LOREBASE_WEB_HSTS turns on Strict-Transport-Security (docs/deployment.md).
#
# The build arguments only set the defaults used when /config.js says nothing:
#
#   docker build -f deploy/docker/web.Dockerfile -t lorebase-web \
#     --build-arg VITE_API_MODE=live --build-arg VITE_KEEPER_BASE_URL=https://keeper.example.com .
#
# The default is the mock API (sample Domains in memory), the only mode that starts without a
# Keeper.
#
# The build context is the repository root; web.Dockerfile.dockerignore keeps it to apps/web and
# deploy/docker/web.

# Base images, pinned by their multi-arch index digests (see docs/release.md).
# node:24-alpine
ARG NODE_IMAGE=node:24-alpine@sha256:06d7adf3e0ef61a13f1d28682726366f97a15aa1888c934d3ecec86c33918b53
# nginxinc/nginx-unprivileged:1.27-alpine
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:1.27-alpine@sha256:65e3e85dbaed8ba248841d9d58a899b6197106c23cb0ff1a132b7bfe0547e4c0

# ---- build: install the locked dependencies, type check and bundle ----
FROM ${NODE_IMAGE} AS build

ARG VITE_API_MODE=mock
ARG VITE_KEEPER_BASE_URL=
ENV VITE_API_MODE=${VITE_API_MODE} \
    VITE_KEEPER_BASE_URL=${VITE_KEEPER_BASE_URL} \
    CI=true

WORKDIR /src
# The pnpm version is the one in the packageManager field of package.json.
RUN corepack enable
COPY apps/web/package.json apps/web/pnpm-lock.yaml ./
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile
COPY apps/web ./
RUN pnpm build

# ---- runtime: static files, the nginx configuration and the config.js writer ----
FROM ${NGINX_IMAGE} AS runtime

ARG VCS_URL=https://github.com/aantenore/lorebase
ARG VERSION=dev
ARG REVISION=unknown

LABEL org.opencontainers.image.title="lorebase-web" \
      org.opencontainers.image.source="${VCS_URL}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.licenses="Apache-2.0"

COPY --from=build --chown=101:101 /src/dist /usr/share/nginx/html
COPY deploy/docker/web/default.conf /etc/nginx/conf.d/default.conf
COPY --chmod=755 deploy/docker/web/40-lorebase-config.sh /docker-entrypoint.d/40-lorebase-config.sh
# The headers file default.conf includes; the script writes it again at every start.
RUN /docker-entrypoint.d/40-lorebase-config.sh

EXPOSE 8080
