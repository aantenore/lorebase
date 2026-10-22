# syntax=docker/dockerfile:1.7
#
# One backend image for every Python service. The service is chosen by the command:
#   docker run lorebase python -m lorebase_keeper      # Keeper, the REST API (default)
#   docker run lorebase lorebase-scribe                 # Scribe, the job worker and scheduler
#   docker run lorebase lorebase-herald                 # Herald, the MCP server
#   docker run lorebase lorebase-guide                  # Guide, the exploration assistant
#
# Build from the repository root:
#   docker build -f deploy/docker/backend.Dockerfile -t lorebase .
#
# A second target adds the in-process embedding model (fastembed on ONNX Runtime, no PyTorch) and
# a pre-downloaded small model; Guide (`/embed`) and Scribe (embeddings, semantic search) use it:
#   docker build -f deploy/docker/backend.Dockerfile --target backend-ml -t lorebase-ml .
# The last stage, `runtime`, is the default target and stays free of those libraries.

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

# ---- build-ml: the same environment plus the embedding extra and a pre-downloaded model ----
FROM build AS build-ml

# Model fetched into the image at build time; empty skips the download (the model is then
# downloaded on first use into the cache directory, which needs a writable path and network).
ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
ENV FASTEMBED_CACHE_PATH=/opt/lorebase/models

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --all-packages --extra documents --extra fastembed ${UV_SYNC_ARGS}
RUN mkdir -p "${FASTEMBED_CACHE_PATH}" \
 && if [ -n "${EMBEDDING_MODEL}" ]; then \
      /app/.venv/bin/python -c "import sys; from fastembed import TextEmbedding; TextEmbedding(model_name=sys.argv[1], cache_dir=sys.argv[2])" \
        "${EMBEDDING_MODEL}" "${FASTEMBED_CACHE_PATH}"; \
    fi

# ---- base: interpreter, user and migration scripts shared by both runtime targets ----
FROM ${PYTHON_IMAGE} AS base

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
 && useradd --system --uid 10001 --gid lorebase --no-create-home --shell /usr/sbin/nologin lorebase \
 && install -d -o 10001 -g 10001 /var/lib/lorebase/guide

# Alembic configuration and revisions live beside the store sources, not in its wheel.
COPY packages/store /opt/lorebase/store

WORKDIR /app
EXPOSE 8080

CMD ["python", "-m", "lorebase_keeper"]

# ---- backend-ml: base plus the embedding libraries and the model cache ----
# The cache directory belongs to the service user, so a named volume mounted on it is writable
# (and is filled from the image on first use) while the root file system stays read-only.
FROM base AS backend-ml

ENV FASTEMBED_CACHE_PATH=/opt/lorebase/models

COPY --from=build-ml /app/.venv /app/.venv
COPY --from=build-ml --chown=10001:10001 /opt/lorebase/models /opt/lorebase/models
USER 10001:10001

# ---- runtime: the default target, without the embedding libraries ----
FROM base AS runtime

COPY --from=build /app/.venv /app/.venv
USER 10001:10001
