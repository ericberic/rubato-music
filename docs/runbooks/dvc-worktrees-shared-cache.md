# DVC + git worktrees: shared cache

## The problem this solves

We develop across several git worktrees (multiple AI agents at once). Git
worktrees share one `.git` but have **separate working trees**, and DVC's
materialization is per working tree:

- `git worktree add` brings the git-tracked `.dvc` **pointer** files but not the
  large binaries they name. Until `dvc checkout`/`dvc pull` runs *in that
  worktree*, the real artifacts are absent there.
- By default each worktree also has its **own** `.dvc/cache` (gitignored, so it
  starts near-empty). So one worktree's `dvc pull` does nothing for another, and
  a worktree started with `--skip-dvc` shows artifacts as "missing" even when
  main has them pulled.

Observed symptom: the app reported "N score artifacts haven't finished setting
up" and (formerly) hard-blocked Go Live, purely because the worktree had not
materialized DVC sources. See [the log](../LOG.md), 2026-08-11.

## The fix: one shared, user-global cache

A single content-addressed cache shared by every checkout on this machine.
Version control is unaffected — the version identity is the md5 in the
git-tracked `.dvc` file; the cache is only a content pool keyed by that hash, and
different branches/versions coexist safely (exactly like a DVC remote).

```bash
# One-time, machine-wide. Inherited by every current and future worktree.
mkdir -p ~/rubato_dvc_cache
# Migrate whatever any existing checkout already has (content-addressed = safe):
rsync -a ~/code/rubato-music/.dvc/cache/files/ ~/rubato_dvc_cache/files/
dvc config --global cache.dir ~/rubato_dvc_cache
```

`cache.dir` in the **user-global** DVC config (`~/Library/Application
Support/dvc/config`) is inherited by every worktree with nothing committed, so it
never breaks CI (which has no such config and skips unreachable artifacts).

Verify:

```bash
dvc cache dir          # -> ~/rubato_dvc_cache  (in main and every worktree)
```

## Per worktree

Worktree creation still does not check out DVC files, so once per new worktree:

```bash
dvc checkout           # instant: hardlink/copy from the shared cache, no re-download
```

`scripts/dev-server.sh` without `--skip-dvc` does this for you. With
`--skip-dvc` the derived artifacts the runtime performs from are already present
(they are what the engine needs); only re-derivation/OMR-validation needs the
sources, so `--skip-dvc` is fine for a performance.

## Cautions

- **`cache.type` stays `copy`** (the default). Working-tree files are independent
  copies, so editing one can never corrupt a cached version.
- **Garbage collection is recoverable only for *pushed* artifacts.** Anything in
  the `localstore` remote returns with `dvc pull`, so keep the remote complete
  with `dvc push` and the local cache is disposable. But a freshly derived or
  regenerated artifact that has **not been pushed yet** (an OMR output, a take
  projection, a new bundle) exists only in the local cache — `dvc gc` evicting it
  is permanent. So: **`dvc push` before you gc.** And even scoped, gc cannot see
  another worktree's *uncommitted* `.dvc` changes, so it may evict an artifact a
  sibling worktree is mid-edit on; commit-or-push those first too. Never run a
  **bare** `dvc gc` against a shared cache (its default workspace scope is the
  narrowest); use `make dvc-gc` (scoped `--all-commits`). This invariant also
  lives in `AGENTS.md`, which every agent loads — a runbook alone does not reach
  them.

## Related

- [Live Run Forensics](live-run-forensics.md)
- Performance must never be blocked by data quality: the score-artifact readiness
  signal is advisory only (App.svelte), and the live runtime degrades to no mix
  rather than aborting when a mix program is missing or stale
  (`LiveRuntimeManager._resolve_mix_policy`).
