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
PERF_COMPOSE ?= docker compose -p lorebase-perf -f tests/perf/compose.yaml
PERF_PG_PORT ?= 55410
PERF_PG_PASSWORD ?= perf
PERF_URL ?= postgresql://perf:$(PERF_PG_PASSWORD)@127.0.0.1:$(PERF_PG_PORT)/postgres
PERF_SCALE_1m := 5406
PERF_SCALE_10m := 54060
PERF_REPLICAS_1m := 1
PERF_REPLICAS_10m := 10
PERF_ITERATIONS ?= 1000
PERF_WARMUP ?= 100
PERF_OUT ?= build/perf
export PERF_PG_PORT PERF_PG_PASSWORD

.PHONY: help sync fmt lint type openapi docs-generate docs docs-serve test-unit test-int test-changed \
	test-perf perf-1m perf-10m perf-down security security-python security-web demo demo-down \
	env up example-data down clean-stack

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

perf-1m: perf-run-1m ## Benchmark at 1M Records on the perf Postgres (seeds once, then measures in place)

perf-10m: perf-run-10m ## Benchmark at 10M Records on the perf Postgres (seeds once, then measures in place)

perf-run-%:
	$(PERF_COMPOSE) up -d --wait
	@mkdir -p $(PERF_OUT)
	LOREBASE_TEST_DATABASE_URL=$(PERF_URL) $(UV) run python tests/perf/bench.py \
		--scale $(PERF_SCALE_$*) --replicas $(PERF_REPLICAS_$*) --extra-links 4 --iterations $(PERF_ITERATIONS) --warmup $(PERF_WARMUP) \
		--branch-iterations 1 --seed-database perf_seed_$* --in-place --out $(PERF_OUT)/perf-$*.md

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

up: ## Build the images and start Postgres, migrations, Keeper and the web app; waits until healthy, prints the web URL
	@scripts/quickstart.sh up

example-data: ## Load the supply sample into the running Keeper (Domain supply, branches main and work)
	@scripts/quickstart.sh data

down: ## Stop the quickstart stack and keep its data
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
