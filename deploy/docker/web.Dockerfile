# syntax=docker/dockerfile:1.7
#
# The web app (apps/web): the production build served by an unprivileged nginx on port 8080.
#
#   docker build -f deploy/docker/web.Dockerfile -t lorebase-web .
#   docker run --rm -p 8080:8080 lorebase-web
#
# Configuration. The app reads its settings from Vite variables when it is built (VITE_API_MODE,
# VITE_KEEPER_BASE_URL, ...), so they are build arguments here:
#
#   docker build -f deploy/docker/web.Dockerfile -t lorebase-web \
#     --build-arg VITE_API_MODE=live --build-arg VITE_KEEPER_BASE_URL=https://keeper.example.com .
#
# The default is the mock API (sample Domains in memory), the only mode that starts without a
# Keeper. The image also serves /config.js, written at start from the VITE_* variables of the
# container environment (or mounted over it, as the Helm chart does), so that one image can work
# against any Keeper. apps/web does not read window.__LOREBASE_CONFIG__ yet: until it does, the
# file is served but has no effect (docs/release.md, "Known gaps").
#
# The build context is the repository root; web.Dockerfile.dockerignore keeps it to apps/web and
# deploy/docker/web.

ARG NODE_TAG=24-alpine
ARG NGINX_TAG=1.27-alpine

# ---- build: install the locked dependencies, type check and bundle ----
FROM node:${NODE_TAG} AS build

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
FROM nginxinc/nginx-unprivileged:${NGINX_TAG} AS runtime

ARG VCS_URL=https://github.com/aantenore/lorebase
ARG VERSION=dev
ARG REVISION=unknown

LABEL org.opencontainers.image.title="lorebase-web" \
      org.opencontainers.image.source="${VCS_URL}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.licenses="Apache-2.0"

COPY --from=build --chown=101:101 /src/dist /usr/share/nginx/html
COPY --chown=101:101 deploy/docker/web/config.js /usr/share/nginx/html/config.js
COPY deploy/docker/web/default.conf /etc/nginx/conf.d/default.conf
COPY --chmod=755 deploy/docker/web/40-lorebase-config.sh /docker-entrypoint.d/40-lorebase-config.sh

EXPOSE 8080
