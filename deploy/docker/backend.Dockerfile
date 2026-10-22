# syntax=docker/dockerfile:1.7
#
# One backend image for every Python service. The service is chosen by the command:
#   docker run lorebase python -m lorebase_keeper      # Keeper, the REST API (default)
#   docker run lorebase lorebase-scribe                 # Scribe, the job worker and scheduler
#   docker run lorebase lorebase-herald                 # Herald, the MCP server
#
# Build from the repository root:
#   docker build -f deploy/docker/backend.Dockerfile -t lorebase .

# Base image: python:3.12-slim, pinned by its multi-arch index digest (see docs/release.md).
ARG PYTHON_IMAGE=python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2

# ---- build: resolve and install the locked, non-dev dependencies and the workspace ----
FROM ${PYTHON_IMAGE} AS build

ARG UV_VERSION=0.11.21
# Extra arguments for `uv sync` (every package and the `documents` parsers are always
# installed), for example to bake the local models into the image:
#   --build-arg UV_SYNC_ARGS="--extra fastembed --extra local-extractors"
ARG UV_SYNC_ARGS=""

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install "uv==${UV_VERSION}"

WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY packages ./packages

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --all-packages --extra documents ${UV_SYNC_ARGS}

# ---- runtime: interpreter, virtual environment and migration scripts only ----
FROM ${PYTHON_IMAGE} AS runtime

ARG VCS_URL=https://github.com/aantenore/lorebase
ARG VERSION=dev
ARG REVISION=unknown

LABEL org.opencontainers.image.title="lorebase" \
      org.opencontainers.image.source="${VCS_URL}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN groupadd --system --gid 10001 lorebase \
 && useradd --system --uid 10001 --gid lorebase --no-create-home --shell /usr/sbin/nologin lorebase

COPY --from=build /app/.venv /app/.venv
# Alembic configuration and revisions live beside the store sources, not in its wheel.
COPY packages/store /opt/lorebase/store

WORKDIR /app
USER 10001:10001
EXPOSE 8080

CMD ["python", "-m", "lorebase_keeper"]
