.DEFAULT_GOAL := help

UV ?= uv
UVX ?= uvx
PNPM ?= pnpm
WEB_DIR ?= apps/web
AUDIT_DIR ?= build/audit
PIP_AUDIT ?= pip-audit@2.10.1
WEB_AUDIT_LEVEL ?= high
PY_PATHS := packages scripts tests
STRICT_SRC := packages/core/src packages/store/src packages/keeper/src packages/herald/src \
	packages/client/src packages/cli/src packages/scribe/src packages/guide/src
OTHER_SRC := packages/platform/src packages/inference/src
PROFILES ?=
# Extra compose files (space separated, from the repository root) for up, logs, down and
# clean-stack, e.g. COMPOSE_OVERLAYS=deploy/compose/hosted-model.yaml; empty: the value in .env.
COMPOSE_OVERLAYS ?=
SERVICES ?=
export COMPOSE_OVERLAYS
PERF_COMPOSE ?= docker compose -p lorebase-perf -f tests/perf/compose.yaml
PERF_PG_PORT ?= 55410
PERF_PG_PASSWORD ?= perf
PERF_URL ?= postgresql://perf:$(PERF_PG_PASSWORD)@127.0.0.1:$(PERF_PG_PORT)/postgres
PERF_SCALE_1m := 5406
PERF_SCALE_10m := 54060
PERF_REPLICAS_1m := 1
PERF_REPLICAS_10m := 10
PERF_FLAGS_1m :=
PERF_FLAGS_10m := --copy-batches 200 --no-pair-copy
PERF_ITERATIONS ?= 1000
PERF_WARMUP ?= 100
PERF_OUT ?= build/perf
# Text of the generated Records: zipf (realistic term frequencies) or demo (the 695 words of the
# example, seeded as perf_seed_1m and perf_seed_10m before the option existed).
PERF_VOCABULARY ?= zipf
PERF_SEED_SUFFIX_zipf := _zipf
PERF_SEED_SUFFIX_demo :=
# Postgres memory of the perf stack for the Docker VM's memory (PERF_VM=8g or 16g); each
# PERF_PG_* value can still be set on its own. Harness settings only, not product defaults.
PERF_VM ?= 8g
PERF_PG_SHARED_BUFFERS_8g := 2GB
PERF_PG_EFFECTIVE_CACHE_SIZE_8g := 6GB
PERF_PG_MAINTENANCE_WORK_MEM_8g := 512MB
PERF_PG_SHM_8g := 3g
PERF_PG_SHARED_BUFFERS_16g := 6GB
PERF_PG_EFFECTIVE_CACHE_SIZE_16g := 12GB
PERF_PG_MAINTENANCE_WORK_MEM_16g := 1GB
PERF_PG_SHM_16g := 7g
PERF_PG_SHARED_BUFFERS ?= $(PERF_PG_SHARED_BUFFERS_$(PERF_VM))
PERF_PG_EFFECTIVE_CACHE_SIZE ?= $(PERF_PG_EFFECTIVE_CACHE_SIZE_$(PERF_VM))
PERF_PG_MAINTENANCE_WORK_MEM ?= $(PERF_PG_MAINTENANCE_WORK_MEM_$(PERF_VM))
PERF_PG_SHM ?= $(PERF_PG_SHM_$(PERF_VM))
PERF_PG_WORK_MEM ?= 8MB
export PERF_PG_PORT PERF_PG_PASSWORD PERF_PG_SHARED_BUFFERS PERF_PG_EFFECTIVE_CACHE_SIZE \
	PERF_PG_MAINTENANCE_WORK_MEM PERF_PG_SHM PERF_PG_WORK_MEM

.PHONY: help sync fmt lint type openapi docs-generate docs docs-serve test-unit test-int test-changed \
	test-perf perf-up perf-1m perf-10m perf-down security security-python security-web demo demo-down \
	env up up-assistant logs example-data example-documents down clean-stack

help: ## List available targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

sync: ## Install all workspace packages, the document parsers and dev tools
	$(UV) sync --all-packages --extra documents

fmt: ## Format and apply safe lint fixes
	$(UV) run ruff format $(PY_PATHS)
	$(UV) run ruff check --fix $(PY_PATHS)

lint: ## Ruff (check and format check) and import-linter contracts
	$(UV) run ruff check $(PY_PATHS)
	$(UV) run ruff format --check $(PY_PATHS)
	$(UV) run lint-imports

type: ## mypy: strict for core, store, keeper, herald, client, cli, scribe and guide; default settings for the rest
	$(UV) run mypy --strict $(STRICT_SRC)
	$(UV) run mypy $(OTHER_SRC)

openapi: ## Regenerate docs/openapi.json from the Keeper application
	$(UV) run python scripts/export_openapi.py

docs-generate: ## Regenerate the CLI and configuration reference pages in docs/reference
	$(UV) run python scripts/docs/generate.py

docs: docs-generate ## Build the documentation site into build/site (strict: a warning fails)
	$(UV) run --group docs mkdocs build --strict

docs-serve: docs-generate ## Serve the documentation site at http://127.0.0.1:8000 with live reload
	$(UV) run --group docs mkdocs serve

test-unit: ## Unit tests of all packages and repository tooling (no external services)
	$(UV) run pytest packages tests/tooling -m "not integration"

test-int: ## Integration tests (need Docker for Postgres)
	$(UV) run pytest tests/integration packages -m integration || [ $$? -eq 5 ]

test-perf: ## Performance harness at a small scale (needs Docker); bench.py writes the baseline
	$(UV) run pytest tests/perf -m perf

perf-up: ## Start the perf Postgres (PERF_VM=16g for a Docker VM of 16 GiB)
	$(PERF_COMPOSE) up -d --wait

perf-1m: perf-run-1m ## Benchmark at 1M Records on the perf Postgres (seeds once, then measures in place)

perf-10m: perf-run-10m ## Benchmark at 10M Records on the perf Postgres (seeds once, then measures in place)

perf-run-%:
	$(PERF_COMPOSE) up -d --wait
	@mkdir -p $(PERF_OUT)
	LOREBASE_TEST_DATABASE_URL=$(PERF_URL) $(UV) run python tests/perf/bench.py \
		--scale $(PERF_SCALE_$*) --replicas $(PERF_REPLICAS_$*) --extra-links 4 --iterations $(PERF_ITERATIONS) --warmup $(PERF_WARMUP) \
		--branch-iterations 1 $(PERF_FLAGS_$*) --vocabulary $(PERF_VOCABULARY) \
		--seed-database perf_seed_$*$(PERF_SEED_SUFFIX_$(PERF_VOCABULARY)) --in-place \
		--out $(PERF_OUT)/perf-$*.md

perf-down: ## Stop the perf Postgres and delete its seeded datasets
	$(PERF_COMPOSE) down -v

test-changed: ## Run only the tests impacted by the current diff
	$(UV) run python scripts/test_changed.py

demo: ## Five-day demo on its own compose stack (project lorebase-demo; KEEP=1 leaves it running)
	@examples/five_day_demo/stack.sh demo

demo-down: ## Stop the demo stack and delete its data
	@examples/five_day_demo/stack.sh down

env: ## Create .env from .env.example with generated passwords and webhook key (keeps an existing one)
	@scripts/quickstart.sh env

up: ## Build the images and start Postgres, migrations, Keeper and the web app (PROFILES=assistant adds Guide and Scribe); waits until healthy, prints the web URL
	@PROFILES="$(PROFILES)" scripts/quickstart.sh up

up-assistant: ## Same as `make up PROFILES=assistant`: also Guide and Scribe on the backend-ml image (embeddings, semantic search)
	@PROFILES=assistant scripts/quickstart.sh up

logs: ## Follow the logs of the quickstart stack (SERVICES="scribe guide" narrows them)
	@PROFILES="$(PROFILES)" scripts/quickstart.sh logs $(SERVICES)

example-data: ## Load the supply sample into the running Keeper (Domain supply, branches main and work)
	@scripts/quickstart.sh data

example-documents: ## Extract the example documents (rules tier; needs Scribe, so `make up PROFILES=assistant`) and list the schema suggestions
	@scripts/quickstart.sh documents

down: ## Stop the quickstart stack, whatever profiles it started, and keep its data
	@scripts/quickstart.sh down

clean-stack: ## Stop the quickstart stack and delete its volumes (data and pulled models)
	@scripts/quickstart.sh clean

# Both checks fail on any advisory that is not in .github/audit-allowlist.yaml and on expired entries.
security: security-python security-web ## Advisories: pip-audit on the uv lock, pnpm audit (high) in apps/web

security-python: ## pip-audit on the locked Python runtime dependencies
	@mkdir -p $(AUDIT_DIR)
	@rm -f $(AUDIT_DIR)/pip-audit.json
	$(UV) export --frozen --all-packages --no-dev --no-emit-workspace --quiet -o $(AUDIT_DIR)/requirements.txt
	-$(UVX) $(PIP_AUDIT) -r $(AUDIT_DIR)/requirements.txt --no-deps --disable-pip --progress-spinner off -f json -o $(AUDIT_DIR)/pip-audit.json
	$(UV) run python scripts/audit_check.py python $(AUDIT_DIR)/pip-audit.json

security-web: ## pnpm audit of the production dependencies of apps/web
	@mkdir -p $(AUDIT_DIR)
	@rm -f $(AUDIT_DIR)/pnpm-audit.json
	-$(PNPM) --dir $(WEB_DIR) audit --prod --audit-level=$(WEB_AUDIT_LEVEL) --json > $(AUDIT_DIR)/pnpm-audit.json
	$(UV) run python scripts/audit_check.py npm $(AUDIT_DIR)/pnpm-audit.json --min-severity $(WEB_AUDIT_LEVEL)
