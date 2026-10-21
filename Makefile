.DEFAULT_GOAL := help

UV ?= uv
UVX ?= uvx
PNPM ?= pnpm
WEB_DIR ?= apps/web
AUDIT_DIR ?= build/audit
PY_PATHS := packages scripts tests
STRICT_SRC := packages/core/src packages/store/src packages/keeper/src packages/herald/src \
	packages/client/src packages/cli/src packages/scribe/src
OTHER_SRC := packages/platform/src packages/inference/src

.PHONY: help sync fmt lint type openapi test-unit test-int test-changed test-perf security demo demo-down

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

# Fails only on advisories that have a fixed version; unfixable ones are listed as warnings.
define AUDIT_CHECK
import json, sys
deps = json.load(open(sys.argv[1]))["dependencies"]
found = [(d["name"], d["version"], v["id"], v["fix_versions"]) for d in deps for v in d.get("vulns", [])]
for name, version, vid, fixes in found:
    print(f"{name} {version} {vid} fix: {', '.join(fixes) or 'none yet'}")
sys.exit(1 if any(fixes for *_, fixes in found) else 0)
endef
export AUDIT_CHECK

security: ## Advisories: pip-audit on the uv lock, pnpm audit (high) in apps/web
	@mkdir -p $(AUDIT_DIR)
	@rm -f $(AUDIT_DIR)/pip-audit.json
	$(UV) export --frozen --all-packages --no-dev --no-emit-workspace --quiet -o $(AUDIT_DIR)/requirements.txt
	-$(UVX) pip-audit -r $(AUDIT_DIR)/requirements.txt --no-deps --disable-pip --progress-spinner off -f json -o $(AUDIT_DIR)/pip-audit.json
	$(UV) run --no-project python -c "$$AUDIT_CHECK" $(AUDIT_DIR)/pip-audit.json
	@# --ignore-unfixable makes pnpm write an empty pnpm-workspace.yaml; remove it unless it was already there
	@cd $(WEB_DIR) && keep=$$([ -e pnpm-workspace.yaml ] && echo 1); \
		echo "$(PNPM) audit --audit-level=high --ignore-unfixable"; \
		$(PNPM) audit --audit-level=high --ignore-unfixable; status=$$?; \
		[ -n "$$keep" ] || rm -f pnpm-workspace.yaml; exit $$status
