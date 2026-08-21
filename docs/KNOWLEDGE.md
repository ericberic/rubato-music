# Knowledge Guide

The `docs/` directory is the long-lived project memory for Rubato agents.

The pattern follows the Karpathy-style LLM wiki idea: raw material is compiled
into structured Markdown pages that agents can read quickly in future sessions.
The docs are not a dump of links. They are curated, cross-referenced working
knowledge.

Reference pattern:
https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f

Rubato also borrows lightweight conventions from local project wiki
and `my-life` docs: search before creating, merge over create, one fact in one
place, source immutability, path-of-entry links, and explicit documentation
ownership. See [Documentation Map](DOCUMENTATION_MAP.md),
[Local Wiki Conventions](sources/local-wiki-conventions.md), and
[Decision 0004](decisions/0004-docs-memory-conventions.md).

Rubato does not currently use Obsidian Dataview dashboards, structured
frontmatter claims, generated related blocks, or OpenClaw memory-wiki compile
tooling. Treat `docs/` as plain Markdown plus git.

Users may open `docs/` in Obsidian for design review. Keep committed docs
Obsidian-friendly but not Obsidian-dependent. Use standard relative Markdown
links, avoid wikilinks, and do not require vault plugins. See
[Obsidian Review](OBSIDIAN.md).

## Layout

- `INDEX.md`: required agent entry point and routing table.
- `LOG.md`: chronological record of ingests, queries, lint/groom passes, and
  major docs changes.
- `OBSIDIAN.md`: local Obsidian review conventions.
- `sources/`: source notes from papers, repos, docs, score sources, and user
  decisions.
- `concepts/`: synthesized reusable knowledge.
- `decisions/`: durable architecture/product decisions.
- `runbooks/`: operational procedures.

## Taxonomy

Use the smallest page type that fits the knowledge.

- `sources/`: factual notes tied to one external or user-provided source.
- `concepts/`: reusable synthesis across sources or tasks.
- `decisions/`: durable choices with tradeoffs and revisit triggers.
- `runbooks/`: repeatable procedures, especially human/hardware workflows.
- Root docs: current project contract, architecture, process, and routing.

Avoid creating new top-level directories until at least three pages clearly need
the same new category.

## Rules

- Prefer primary sources for technical claims.
- Search existing docs before creating a new page.
- Merge over create: update the existing owner page when it can hold the
  knowledge without becoming muddled.
- Keep one durable fact in one canonical place; link summaries back to it
  instead of copying it.
- Keep source notes factual and dated.
- Treat source notes as evidence. Do not mix broad synthesis into a source page.
- Put synthesis in concept pages, not source pages.
- Use [Documentation Map](DOCUMENTATION_MAP.md) to find the owning doc before
  editing root docs or adding new categories.
- Ensure every durable page has a path of entry from [INDEX](INDEX.md) or a
  related page.
- Record contradictions explicitly.
- Link related pages.
- Use standard relative Markdown links. Do not commit Obsidian-only wikilinks.
- Update docs when a task teaches us something future agents should reuse.
- Keep pages atomic: one page should answer one durable question or capture one
  source/procedure/decision. Split pages when unrelated concerns start sharing a
  file.
- Optimize for selective context loading. A normal task should usually need
  `docs/INDEX.md` plus one to three linked pages.
- There is no hard Karpathy page-length rule. As a Rubato convention, target
  about 500-900 words per atomic page. Consider splitting pages above roughly
  1,000 words or 150-200 lines unless they are root overview docs.
- Rough token mapping: English prose is often about 0.75 tokens per word in the
  other direction, or 100 words is roughly 130 tokens. A 700-word Markdown page
  is roughly 900-1,100 tokens depending on lists, links, and code blocks.

## Read Workflow

Agents should not blindly load the entire docs directory. Use `docs/INDEX.md` as the
router, then fetch the smallest set of pages needed for the task.

Default sequence:

1. Read `docs/INDEX.md`.
2. Search likely docs with `rg` before assuming knowledge is absent.
3. Read the core doc for the task area.
4. Read the linked concept page if the task depends on compiled knowledge.
5. Read source notes only when checking claims, versions, caveats, or provenance.
6. Read decision records when changing scope, architecture, process, or data contracts.
7. Check `docs/LOG.md` when recent docs work or research context matters.

Examples:

- Implement score following: read `SYSTEM_DESIGN.md`,
  `concepts/score-following.md`, and the Matchmaker/ACCompanion source
  notes.
- Add a hardware runbook: read `runbooks/yamaha-midi.md` and relevant source
  notes.
- Change MVP scope: read `PRD.md` and decision records first.

## Write Workflow

Add to docs when a task produces reusable knowledge, not every time a file is
edited.

Write a source note when:

- A paper, repo, package, score source, dataset, or user-provided report is
  researched.
- A source has caveats future agents should not rediscover.
- A tool version or API behavior affects implementation.
- The page can link to at least one concept, decision, runbook, or core doc.

Before creating the source note, search for an existing source page for the same
paper, repo, package, device, score source, or user-provided report. Extend that
page unless the new source is genuinely distinct.

Write a concept page when:

- Multiple sources need synthesis.
- The project needs a stable mental model or design vocabulary.
- A topic is referenced by more than one task.
- The page can link back to its key sources.

Write a decision record when:

- Scope changes.
- Architecture or data contracts change.
- A dependency or major package is accepted or rejected.
- A future agent would otherwise need to infer why a path was chosen.

Write a runbook when:

- There is a repeatable operational procedure.
- Human/hardware steps are involved.
- The task will be repeated during rehearsals.

Update [Documentation Map](DOCUMENTATION_MAP.md) when adding a new root doc or
changing which page owns a category of knowledge.

## Groom Workflow

Grooming means turning accumulated notes into a smaller, more useful knowledge
base. It should happen when the user asks, after major research, or when the
index stops routing well.

Grooming checklist:

- Read `docs/INDEX.md`.
- Review recent entries in `docs/LOG.md`.
- List docs pages and identify stale, duplicate, or orphaned pages.
- Merge repeated facts into concept pages.
- Move source-specific details back into source notes.
- Add missing links between index, concepts, sources, decisions, and runbooks.
- Mark unresolved contradictions as open questions instead of hiding them.
- Archive or delete obsolete pages only when their content is captured elsewhere.
- Append a `lint` or `groom` entry to `docs/LOG.md`.
- Update `docs/INDEX.md` last so it reflects the groomed structure.

## Health Checks

Run lightweight health checks after documentation-heavy work:

- Search for stale paths after moving docs.
- Search for old project scope terms that might contradict the current MVP.
- Confirm new pages are reachable from `docs/INDEX.md` or from a linked page.
- Confirm source notes link to related concepts or decisions.
- Confirm concept pages link back to their sources.
- Confirm runbooks link to the concepts or design docs they operationalize.
- Confirm `docs/LOG.md` records substantial ingests/grooms.
- Run `make docs-health`.

Useful commands:

```bash
make docs-health
rg -n "docs/wiki|wiki/README|SOLOIST vs OTHER|style transfer" docs README.md AGENTS.md
wc -w -l docs/*.md docs/concepts/*.md docs/sources/*.md docs/decisions/*.md docs/runbooks/*.md
git diff --check
```

Do not force these exact searches every time; adapt them to the docs changed.

## Index Contract

`docs/INDEX.md` is the entry point for agent context. Keep it short enough to
read every time and specific enough to route agents to deeper pages.

The index should contain:

- Current MVP.
- Deferred scope.
- Core docs.
- Knowledge routing by topic.
- Runbooks.
- Knowledge operations.
- Any urgent caveats future agents must see immediately.

## Helper Script

`make docs-health` runs the repo's lightweight docs checks. It fails on broken
local Markdown links, docs pages unreachable from `docs/INDEX.md`, committed
Obsidian wikilinks, and `git diff --check` errors. It also prints non-blocking
warnings for long pages and stale-scope terms that may need manual review.

## Page Types

### Source Note

Use for a paper, repo, dataset, package, score source, or user-provided report.

Required sections:

- Source
- Date Read
- Why It Matters
- Key Facts
- Caveats
- Links

### Concept Page

Use for reusable synthesized knowledge.

Required sections:

- Summary
- Current Recommendation
- Details
- Open Questions
- Related

### Decision Record

Use for scope and architecture decisions.

Required sections:

- Status
- Context
- Decision
- Consequences
- Revisit Trigger
