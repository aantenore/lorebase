.DEFAULT_GOAL := help

UV ?= uv
PY_PATHS := packages scripts tests
STRICT_SRC := packages/core/src packages/store/src packages/keeper/src packages/client/src packages/cli/src
OTHER_SRC := packages/platform/src packages/inference/src

.PHONY: help sync fmt lint type openapi test-unit test-int test-changed

help: ## List available targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

sync: ## Install all workspace packages and dev tools
	$(UV) sync --all-packages

fmt: ## Format and apply safe lint fixes
	$(UV) run ruff format $(PY_PATHS)
	$(UV) run ruff check --fix $(PY_PATHS)

lint: ## Ruff (check and format check) and import-linter contracts
	$(UV) run ruff check $(PY_PATHS)
	$(UV) run ruff format --check $(PY_PATHS)
	$(UV) run lint-imports

type: ## mypy: strict for core, store, keeper, client and cli; default settings for the rest
	$(UV) run mypy --strict $(STRICT_SRC)
	$(UV) run mypy $(OTHER_SRC)

openapi: ## Regenerate docs/openapi.json from the Keeper application
	$(UV) run python scripts/export_openapi.py

test-unit: ## Unit tests of all packages and repository tooling (no external services)
	$(UV) run pytest packages tests/tooling -m "not integration"

test-int: ## Integration tests (need Docker for Postgres)
	$(UV) run pytest tests/integration packages -m integration || [ $$? -eq 5 ]

test-changed: ## Run only the tests impacted by the current diff
	$(UV) run python scripts/test_changed.py
