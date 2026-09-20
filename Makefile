.DEFAULT_GOAL := help
.PHONY: help run test check build clean check-types verify-bundles dvc-sync dvc-gc docs-health secrets-scan

help: ## Show this help message
	@echo "Usage: make [target] [ARGS=\"...\"]"
	@echo ""
	@echo "Targets:"
	@awk '/^[a-zA-Z_-]+:.*##/ { target = $$1; sub(/:.*/, "", target); desc = $$0; sub(/^.*##[ \t]*/, "", desc); printf "  \033[36m%-16s\033[0m %s\n", target, desc; }' $(MAKEFILE_LIST)

# Interactive foreground target; do not invoke concurrently or under 'make -j'.
run: ## Start FastAPI server & rehearsal cockpit (options via ARGS="--skip-dvc")
	./scripts/dev-server.sh $(ARGS)

test: ## Run tests (default: pytest + vitest; pass ARGS="..." for pytest options)
ifeq ($(strip $(ARGS)),)
	uv run --extra dev pytest
	npm test --prefix webapp
else
	uv run --extra dev pytest $(ARGS)
endif

check: ## Run all static quality gates (check-types, svelte-check, docs-health)
	$(MAKE) check-types
	npm run check --prefix webapp
	uv run python scripts/docs_health.py

build: ## Build production webapp assets into static/
	npm run build --prefix webapp

clean: ## Remove build artifacts, bytecode, and test/cache files
	rm -rf webapp/dist .pytest_cache .ruff_cache .coverage htmlcov
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +

docs-health: ## Run lightweight markdown link and documentation checks
	uv run python scripts/docs_health.py

# Full-history secret scan (API keys, tokens, private keys). Run before any push
# to a public remote. Requires gitleaks (`brew install gitleaks`); CI runs the
# same tool via .github/workflows/gitleaks.yml, and .pre-commit-config.yaml runs
# it on every commit once `pre-commit install` has been run.
secrets-scan: ## Full-history secret scan via gitleaks
	gitleaks detect --source . --redact --verbose

# Verify every score bundle's artifacts against the sha256 hashes recorded in
# its bundle.yaml -- catches a drifted source-of-truth (e.g. a beat map rebuilt
# without updating its recorded hash) with one command instead of forensics.
verify-bundles: ## Verify score bundle artifacts against sha256 hashes in bundle.yaml
	uv run python scripts/verify_score_bundle.py

# Materialize this worktree's DVC artifacts from the shared cache. Run once per
# new worktree -- `git worktree add` brings the `.dvc` pointers, not the data.
dvc-sync: ## Materialize worktree DVC artifacts from shared cache
	uv run dvc checkout

# Sanctioned garbage collection for the shared cache. Scoped to every commit in
# the shared `.git`, so it never evicts an object another worktree still
# references. Recoverable regardless: the `localstore` remote is authoritative.
# Never run a bare `dvc gc` against a shared cache -- use this.
dvc-gc: ## Sanctioned garbage collection for the shared DVC cache
	uv run dvc gc --all-commits --workspace

# Design doc docs/design/SCHEMA_VALIDATION_ARCH.md §2.2 (issue #83): the
# frontend contract (webapp/openapi.json + webapp/src/generated/) is
# generated, committed, and must never drift from the backend Pydantic
# models that produce it. Regenerate both stages and fail if that changes
# anything the CI checkout doesn't already have -- a schema change that
# wasn't regenerated (or a regenerated output that wasn't committed) fails
# here instead of silently shipping stale types.
check-types: ## Verify OpenAPI spec and generated TypeScript client are up-to-date
	uv run --extra dev python scripts/export_openapi.py
	cd webapp && npm run generate
	git diff --exit-code webapp/openapi.json webapp/src/generated
