# AI Agent Guide - Rubato

## Active Mission

Rubato is now scoped around one MVP:

> Provide real-time orchestral accompaniment for Chopin Piano Concerto No. 1 in
> E minor while Eric plays the solo piano part on a Yamaha MIDI-capable piano.

The project is local-first and symbolic-first. MIDI, MusicXML, score following,
tempo modeling, section policy, accompaniment scheduling, and rehearsal feedback
are the core work.

Style transfer, ERIC-vs-OTHER generation, and raw audio are deferred.

## Required Reading

Before substantial work, read [`docs/INDEX.md`](docs/INDEX.md) into context.
Treat it as the documentation router: it tells you which additional docs to fetch for the
current task. Do not preload the whole `docs/` directory unless the user asks
for a full documentation review or grooming pass.

The `docs/` directory itself is the Karpathy-style LLM wiki/knowledge repo:
https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f

For broad or architectural work, follow links from
[`docs/INDEX.md`](docs/INDEX.md) to the relevant core docs, concept pages,
source notes, decisions, or runbooks.

The docs are durable project memory. If you learn something future agents should
reuse, add or update the relevant Markdown page under `docs/`.

When the user asks to "groom", "compile", or "update the wiki", follow the
read/write/groom workflow in [`docs/KNOWLEDGE.md`](docs/KNOWLEDGE.md).

When adding or reorganizing durable docs, also check
[`docs/DOCUMENTATION_MAP.md`](docs/DOCUMENTATION_MAP.md) for the owning doc and
update protocol.

Eric may review `docs/` locally in Obsidian. Keep committed docs
Obsidian-friendly but not Obsidian-dependent: use standard relative Markdown
links, avoid committed wikilinks, and do not rely on Dataview or vault plugins.
See [`docs/OBSIDIAN.md`](docs/OBSIDIAN.md).

Repo-local skills live under [`.agents/skills`](.agents/skills). Claude should
discover the same files through the [`.claude/skills`](.claude/skills) symlink.
Keep skill instructions agent-neutral and use repo-relative paths.

## Knowledge Repo Duties

Agents must treat docs maintenance as part of the work, not a separate chore.

- Read [`docs/INDEX.md`](docs/INDEX.md) first, then dynamically load only
  needed linked pages.
- Search existing docs before creating a new page.
- Merge over create when an existing page can own the knowledge cleanly.
- Keep one durable fact in one canonical place; link summaries back to it.
- Prefer primary sources for technical claims and record them in
  [`docs/sources/`](docs/sources/).
- Compile repeated or cross-cutting knowledge into
  [`docs/concepts/`](docs/concepts/).
- Record durable scope or architecture choices in
  [`docs/decisions/`](docs/decisions/).
- Put repeatable human/hardware procedures in [`docs/runbooks/`](docs/runbooks/).
- Append substantial ingests, grooms, and decisions to [`docs/LOG.md`](docs/LOG.md).
- Keep [`docs/INDEX.md`](docs/INDEX.md) short, current, and useful as the routing layer.
- Use [`docs/DOCUMENTATION_MAP.md`](docs/DOCUMENTATION_MAP.md) before adding
  new root docs or changing doc ownership boundaries.
- When docs are edited, check for broken/stale links and contradictory guidance.
- Run `make docs-health` before finishing documentation-heavy work.
- For larger grooming passes, follow the health checklist in
  [`docs/KNOWLEDGE.md`](docs/KNOWLEDGE.md).

## Current Architecture

```text
Yamaha MIDI Piano
  -> MIDI input adapter
  -> score follower
  -> tempo model
  -> section policy
  -> accompaniment scheduler
  -> MIDI output adapter
  -> Yamaha synth / IAC / Logic / Kontakt
```

## MVP Build Order

1. Prepare a score bundle for a short Chopin excerpt.
2. Align recorded solo MIDI to the score offline.
3. Render accompaniment offline from the solo timing map.
4. Replay recorded MIDI through the causal runtime path.
5. Read live Yamaha MIDI input.
6. Emit live accompaniment MIDI output.
7. Iterate from traces and Eric's feedback.

## Constraints

- No raw audio pipeline for the MVP.
- No cloud runtime dependency.
- No large binaries in git.
- Use DVC for score bundles, recordings, processed data, and runs.
- Keep hardware tests opt-in.
- Keep base tests deterministic.

## Git Workflow

- Do not commit directly to `main`.
- Keep `/Users/ehuang/code/Rubato` on `main`; treat it as the clean anchor checkout.
- Do implementation work in git worktrees under `/Users/ehuang/code/Rubato-worktrees/`.
- Create task worktrees with `git worktree add /Users/ehuang/code/Rubato-worktrees/<task> <branch>`.
- Reuse or remove worktrees after branches merge; do not maintain Rubato2/Rubato3 clones.
- Use `codex/` branch names in Codex Desktop.

### DVC across worktrees

Large artifacts are DVC-tracked, so each worktree is an independent DVC checkout:
`git worktree add` brings the `.dvc` pointers, not the data. All worktrees share
one external cache (`dvc config --global cache.dir`); run `make dvc-sync`
(`dvc checkout`) once in a new worktree to materialize it. The `localstore` remote
is authoritative — keep it complete with `dvc push`; a pushed artifact evicted by
gc returns with `dvc pull`, but a freshly derived artifact you have **not pushed
yet** exists only in the local cache and gc would lose it permanently, so push
before you gc. Never run a bare `dvc gc` against the shared cache (its default
scope cannot see other worktrees' referenced objects); use `make dvc-gc`. See
[the runbook](docs/runbooks/dvc-worktrees-shared-cache.md).

## Human Loop

Eric supplies:

- Score/excerpt choices.
- Yamaha MIDI hardware access.
- Solo MIDI recordings.
- Listening/playability feedback.
- Musical annotations when the score alone is ambiguous.

Agents supply:

- Code.
- Tests.
- Research.
- Experiment design.
- Run analysis.
- Documentation updates.
