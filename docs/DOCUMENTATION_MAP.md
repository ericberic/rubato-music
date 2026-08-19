# Documentation Map

This map keeps Rubato docs lean and prevents duplicated project memory.

## Ownership

- [`AGENTS.md`](../AGENTS.md): agent behavior, required reading, worktree
  policy, and high-level mission.
- [`INDEX.md`](INDEX.md): required docs entry point and routing table. Keep it
  short.
- [`KNOWLEDGE.md`](KNOWLEDGE.md): how agents read, write, groom, and lint the
  docs-as-memory system.
- [`LOG.md`](LOG.md): chronological record of substantial ingests, decisions,
  grooms, experiments, and hardware findings.
- [`PRD.md`](PRD.md): product scope, success criteria, users, and non-goals.
- [`VISION_AND_UX_DESIGN.md`](VISION_AND_UX_DESIGN.md): product vision,
  rehearsal/performance loop intent, interpretation-profile learning model,
  user workflow, UI/interaction design, aesthetic language, and build roadmap.
- [`ARCHITECTURE_BRIEF.md`](ARCHITECTURE_BRIEF.md): five-minute system intent and architecture overview.
- [`SYSTEM_DESIGN.md`](SYSTEM_DESIGN.md): runtime architecture, data contracts, and component boundaries.
- [`ML_STRATEGY.md`](ML_STRATEGY.md): score-following, tempo, accompaniment, and MIR strategy.
- [`ML_EXPERIMENTATION.md`](ML_EXPERIMENTATION.md): experiment design, simulation, traces, and rehearsal feedback workflow.
- [`TESTING.md`](TESTING.md): deterministic, optional, hardware, and CI test strategy.
- [`DEVELOPMENT.md`](DEVELOPMENT.md): local setup, dependencies, branching,
  multi-agent worktree coordination, CI, and DVC workflow.
- [`OBSIDIAN.md`](OBSIDIAN.md): local Obsidian review conventions that remain
  compatible with plain Markdown and GitHub.
- [`.agents/skills`](../.agents/skills): canonical repo-local skills for agent workflows.
- [`.claude/skills`](../.claude/skills): symlink to the canonical repo-local skills for Claude discovery.
- [`concepts/`](concepts/): reusable synthesis and mental models across sources/tasks.
- [`sources/`](sources/): source-specific factual notes from papers, repos,
  Yamaha docs, score files, user reports, or local repo inspections.
- [`decisions/`](decisions/): durable product, architecture, dependency, and process decisions.
- [`runbooks/`](runbooks/): repeatable operational procedures, especially hardware/human workflows.
- [`design/`](design/): standalone feature/UI design docs (proposal → implemented),
  including lifecycle/artifact state-machine contracts that refine the core
  system data model without duplicating it, and
  [`REHEARSAL_TAKE_COVERAGE_DESIGN.md`](design/REHEARSAL_TAKE_COVERAGE_DESIGN.md)
  (take capture, offline alignment/fusion, coverage overlay), and
  [`MIX_AUTHORING_MODE.md`](design/MIX_AUTHORING_MODE.md) (the separate
  score-centered modality for spatial/mix program editing). Each page is
  scoped to one surface or flow rather than the whole product (that's
  [Vision and UX Design](VISION_AND_UX_DESIGN.md)'s job). Keep each page's
  `Status` line current (`proposed` / `implemented`) instead of moving or
  deleting it once built. Use `ALL_CAPS.md`, matching the root-doc
  convention, since these are full standalone documents; `design/` filenames
  must not collide with root `docs/*.md` filenames — the three-workflow star
  diagram wrapper is named
  [`THREE_WORKFLOW_OVERVIEW.md`](design/THREE_WORKFLOW_OVERVIEW.md) rather
  than `SYSTEM_DESIGN.md` for exactly this reason.

## Overlap Rules

- Product docs may summarize architecture, but data contracts live in
  [System Design](SYSTEM_DESIGN.md).
- Strategy docs may summarize source claims, but source-specific facts stay in [sources](sources/).
- Runbooks may include commands, but do not redefine architecture or product scope.
- Decisions explain why a path was chosen; do not hide active instructions only inside decisions.
- [INDEX](INDEX.md) links to pages; it should not duplicate their content.
- [LOG](LOG.md) records what changed; it is not the source of truth for the changed content.

## Update Protocol

1. Search existing docs before creating a page.
2. Update the owning doc first.
3. Put one durable fact in one canonical place, then link to it from summaries.
4. Prefer updating an existing atomic page over creating a near-duplicate.
5. If a new page is needed, add path-of-entry links from [INDEX](INDEX.md) or a
   related page.
6. Append a concise [LOG](LOG.md) entry for substantial research, design,
   hardware, or grooming work.
7. Run `make docs-health` before finishing documentation-heavy work.

## Borrowed Convention

Rubato borrows the useful parts of the Jesse & Eric wiki and `my-life` docs
(see [Local Wiki Conventions](sources/local-wiki-conventions.md) and
[Decision 0004](decisions/0004-docs-memory-conventions.md)):
search-before-create, merge-over-create, one-fact-one-place, source immutability,
path-of-entry linkage, and explicit doc ownership.

Rubato does not currently adopt Obsidian Dataview dashboards, frontmatter claims,
managed compile blocks, generated indexes, or OpenClaw memory-wiki tooling. This
repo's memory system is plain Markdown plus git, routed through
[INDEX](INDEX.md).
Committed docs should still be comfortable to review in Obsidian; see
[Obsidian Review](OBSIDIAN.md).
