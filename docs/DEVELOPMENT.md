# Development Workflow

## Branching And Worktrees

Use feature branches. In Codex Desktop, branches should use the `codex/` prefix.
Do not commit directly to `main`.

Keep the main branch clean. Agents and contributors should do active
work in sibling git worktrees, for example:

```bash
git worktree add ../rubato-worktrees/<task-name> <branch-name>
```

When a task is done and merged, remove or recycle the worktree. Do not create
long-lived `Rubato2`/`Rubato3` clones.

Each worktree is an independent DVC checkout that shares one external cache;
`git worktree add` brings the `.dvc` pointers, not the data, so run `make
dvc-sync` once in a new worktree. See
[the DVC + worktrees runbook](runbooks/dvc-worktrees-shared-cache.md).

## Local Setup

Use Python 3.11 or 3.12. The live score-following dependencies do not currently
support Python 3.13+ through this project.

Base development:

```bash
uv sync --extra dev
uv run pytest
```

Live MIDI/accompaniment work:

```bash
uv sync --extra dev --extra live
```

Running the actual rehearsal cockpit (as opposed to base test/dev work) is
one command, `./scripts/dev-server.sh`: it pulls DVC-managed score/data
artifacts, builds the webapp if stale, syncs `dev`+`live` extras, starts the
server as a background job, polls `GET /` until it responds, and opens the
app URL on macOS. `--skip-dvc` and `--skip-build` opt out of the
corresponding step (e.g. for repeated CI-style runs); Ctrl-C still stops the
server, via an `INT`/`TERM`/`EXIT` trap rather than a bare foreground exec
(rubato#99). See [PWA Rehearsal UI](concepts/pwa-rehearsal-ui.md) for the
cockpit itself.

The live PTHMM worker uses the prepared follower-reference MIDI directly. It
does not import the offline Partitura score loader, Matchmaker audio features,
Librosa, or Matplotlib. Matchmaker is pinned to 0.3.0 because this adapter uses
its internal pitch-HMM seam; review and remeasure startup before upgrading it.

Training/style-transfer extras are deferred and should not be installed unless a
future task explicitly reactivates them.

## Documentation Rule

Before substantial work, read `docs/INDEX.md` first. Use it as the context
router for the current task. Do not preload every docs page unless the task is a
full docs review or grooming pass.

Then read the relevant additional pages, usually from:

- `docs/PRD.md`
- `docs/SYSTEM_DESIGN.md`
- Relevant pages under `docs/concepts/`, `docs/sources/`, `docs/decisions/`, or
  `docs/runbooks/`

If new research, score-prep knowledge, device setup, or experiment results are
learned, compile them into `docs/` before ending the task.

When the user asks for a docs grooming pass, follow `docs/KNOWLEDGE.md`:

- Remove duplication.
- Link orphan pages.
- Separate source facts from synthesis.
- Update `docs/INDEX.md` last.

## DVC

DVC remains for data and run artifacts:

- `data/scores/`
- `data/recordings/`
- `data/processed/`
- `runs/`

Do not commit raw concerto data, generated recordings, or rendered outputs to
git unless they are tiny fixtures intended for tests.

## CI Expectations

CI should run base tests only. Hardware, VST, and live MIDI tests are local
manual smoke tests.

## High-Conflict Areas

Coordinate before changing:

- Score bundle schema.
- MIDI I/O runtime abstractions.
- Dependency groups.
- Run artifact layout.
- Server API contracts.
