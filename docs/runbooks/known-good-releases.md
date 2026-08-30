# Known-good releases

Mark states that are **verified working** so returning to one is a `git checkout`,
not a forensic investigation. A known-good marker pins two things at once:

- **the code** — the git commit; and
- **the source-of-truth artifacts** — because every score bundle's `bundle.yaml`
  records a `sha256` for each artifact, and those hashes live in the same commit.
  `make verify-bundles` confirms the on-disk bytes still match those hashes.

The markers are **annotated git tags** named `known-good/<date>[-<note>]`. Tags are
pushed to `origin`, so any machine can `git checkout <tag>` and be back on a
working version.

## Verified points

| Tag | Date | Evidence | Notes |
| --- | --- | --- | --- |
| `known-good/2026-08-15-live` | 2026-08-15 | Live performance, run `live-1786832232337`: started from the top, `LEAD → HOLD → FOLLOW`, clean follow throughout. | The beat map (`performance_beat_map.machine.json`) was already `ceb68d61…`; at this commit `bundle.yaml` still recorded the older `c5ef2609…`, so `make verify-bundles` reports one mismatch here. The **data** is identical to the current baseline — only the recorded hash was stale (corrected in `known-good/2026-08-29`). |
| `known-good/2026-08-29` | 2026-08-29 | `make verify-bundles` green (10/10); full accompaniment + live-runtime suite green. Same beat-map data as the 8/15 live take. | Baseline after: correcting the stale beat-map hash in `bundle.yaml`; fixing the orchestra-lead-in entry handoff (see below). |

## What was learned (so it isn't re-derived)

- **Two ways to start a run, one robust.** Starting from the top (no start
  measure) takes the `LEAD → HOLD → FOLLOW` path and is the empirically verified
  one. Selecting a **start measure** routes to the `orchestra_lead_in` path,
  which until 2026-08-29 could leave the orchestra leading autonomously because
  the pre-entry orchestra raced past the entry boundary and the handoff never
  certified (run `live-1788038221669`). Fixed by clamping the pre-entry orchestra
  at the authored entry boundary and certifying the soloist's confident entry
  against it — see `src/aimusic/accompaniment/live_engine.py`
  (`_orchestra_lead_in_state_at`, `_update_during_orchestra_entry`) and the
  regression test
  `tests/accompaniment/test_live_engine.py::test_orchestra_lead_in_hands_off_when_follower_locks_after_the_entry`.
- **The beat map is git-tracked** (not DVC); only the `source/` files are DVC.
  So a git tag pins the derived SOT directly and pins the `.dvc` pointers for
  sources. Full restore is `git checkout <tag>` then `make dvc-sync`.

## The ritual (after a successful live performance)

1. Note the run id (the cockpit's live-`<n>` id; preserve it before the next run).
2. `make verify-bundles` — must be green.
3. `uv run --extra dev pytest tests/accompaniment tests/server/test_live_runtime.py -q` — green.
4. Tag it, describing the evidence:
   ```bash
   git tag -a known-good/$(date +%Y-%m-%d) -m "verified: run live-<id>; verify-bundles green; suite green"
   git push origin known-good/$(date +%Y-%m-%d)
   ```
5. Add a row to the table above.

## Returning to a known-good version

```bash
git checkout known-good/<date>
make dvc-sync        # materialize the DVC-tracked source artifacts
make verify-bundles  # confirm the SOT matches what that release recorded
```
