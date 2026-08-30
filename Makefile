.PHONY: docs-health check-types dvc-sync dvc-gc secrets-scan verify-bundles

docs-health:
	python3 scripts/docs_health.py

# Verify every score bundle's artifacts against the sha256 hashes recorded in
# its bundle.yaml -- catches a drifted source-of-truth (e.g. a beat map rebuilt
# without updating its recorded hash) with one command instead of forensics.
verify-bundles:
	uv run python scripts/verify_score_bundle.py

# Full-history secret scan (API keys, tokens, private keys). Run before any push
# to a public remote. Requires gitleaks (`brew install gitleaks`); CI runs the
# same tool via .github/workflows/gitleaks.yml, and .pre-commit-config.yaml runs
# it on every commit once `pre-commit install` has been run.
secrets-scan:
	gitleaks detect --source . --redact --verbose

# Materialize this worktree's DVC artifacts from the shared cache. Run once per
# new worktree -- `git worktree add` brings the `.dvc` pointers, not the data.
dvc-sync:
	uv run dvc checkout

# Sanctioned garbage collection for the shared cache. Scoped to every commit in
# the shared `.git`, so it never evicts an object another worktree still
# references. Recoverable regardless: the `localstore` remote is authoritative.
# Never run a bare `dvc gc` against a shared cache -- use this.
dvc-gc:
	uv run dvc gc --all-commits --workspace

# Design doc docs/design/SCHEMA_VALIDATION_ARCH.md §2.2 (issue #83): the
# frontend contract (webapp/openapi.json + webapp/src/generated/) is
# generated, committed, and must never drift from the backend Pydantic
# models that produce it. Regenerate both stages and fail if that changes
# anything the CI checkout doesn't already have -- a schema change that
# wasn't regenerated (or a regenerated output that wasn't committed) fails
# here instead of silently shipping stale types.
check-types:
	uv run --extra dev python scripts/export_openapi.py
	cd webapp && npm run generate
	git diff --exit-code webapp/openapi.json webapp/src/generated
