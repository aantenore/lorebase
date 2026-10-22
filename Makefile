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
	packages/client/src packages/cli/src packages/scribe/src
OTHER_SRC := packages/platform/src packages/inference/src

.PHONY: help sync fmt lint type openapi docs-generate docs docs-serve test-unit test-int test-changed \
	test-perf security security-python security-web demo demo-down

help: ## List available targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

sync: ## Install all workspace packages, the document parsers and dev tools
	$(UV) sync --all-packages --extra documents

fmt: ## Format and apply safe lint fixes
	$(UV) run ruff format $(PY_PATHS)
	$(UV) run ruff check --fix $(PY_PATHS)

lint: ## Ruff (check and format check) and import-linter contracts
	$(UV) run ruff check $(PY_PATHS)
	$(UV) run ruff format --check $(PY_PATHS)
	$(UV) run lint-imports

type: ## mypy: strict for core, store, keeper, herald, client, cli and scribe; default settings for the rest
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

test-changed: ## Run only the tests impacted by the current diff
	$(UV) run python scripts/test_changed.py

demo: ## Five-day demo on its own compose stack (project lorebase-demo; KEEP=1 leaves it running)
	@examples/five_day_demo/stack.sh demo

demo-down: ## Stop the demo stack and delete its data
	@examples/five_day_demo/stack.sh down

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
