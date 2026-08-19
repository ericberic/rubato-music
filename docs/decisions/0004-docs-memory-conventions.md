# Decision 0004: Keep A Lightweight Docs-As-Memory Wiki

## Status

Accepted on 2026-07-06.

## Context

Rubato is now developed as a long-running human-agent collaboration. Eric wants
future agents to reuse accumulated project knowledge instead of rediscovering
architecture, score-following behavior, Yamaha MIDI details, and experiment
results.

Local reference repos have stronger wiki conventions:

- Jesse & Eric has a full memory-wiki vault with schemas, structured claims,
  dashboards, generated links, and lint/compile tooling.
- `my-life` has a lightweight documentation ownership map and clear update
  protocol for avoiding doc drift (see
  [Local Wiki Conventions](../sources/local-wiki-conventions.md) and
  [Documentation Map](../DOCUMENTATION_MAP.md)).

Rubato already uses [INDEX](../INDEX.md) as an agent routing layer and `docs/`
as a Karpathy-style Markdown knowledge repo.

## Decision

Rubato will adopt the lightweight curation conventions, not the full
memory-wiki/Obsidian stack.

Adopt:

- Search before creating new pages.
- Merge over create when an existing page can own the knowledge.
- One durable fact should have one canonical home.
- Source notes are evidence and should remain source-specific.
- Every durable page needs a path of entry from [INDEX](../INDEX.md) or related
  pages.
- [Documentation Map](../DOCUMENTATION_MAP.md) defines owning docs and update
  protocol.
- [LOG](../LOG.md) records substantial research, grooms, decisions,
  experiments, and hardware findings.

Do not adopt for now:

- Obsidian Dataview dashboards.
- Required frontmatter claims schemas.
- Generated `Related` blocks or compile-managed sections.
- Generated indexes/reports inside `docs/`.
- OpenClaw memory-wiki tooling or transactions.

## Consequences

- Agents have a clearer memory recall and write workflow without extra tooling.
- Docs remain plain Markdown and easy to review in PRs.
- Some advanced benefits from the Jesse & Eric vault, such as claim health and
  contradiction reports, remain manual checks.
- Future agents should update [Documentation Map](../DOCUMENTATION_MAP.md)
  before adding new root docs or new top-level documentation categories.

## Revisit Trigger

Revisit if Rubato gains enough autonomous ingestion, generated docs, or
cross-agent write volume that plain Markdown conventions stop preventing drift.
