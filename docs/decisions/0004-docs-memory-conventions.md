# Decision 0004: Keep A Lightweight Docs-As-Memory Wiki

## Status

Accepted on 2026-07-06.

## Context

Rubato is now developed as a long-running human-agent collaboration. The team wants
a durable documentation structure that keeps knowledge in repo and avoids
re-researching the same core facts.

## Options Considered

- **Full memory-wiki vault inline**: Import the full schema/claim tooling from
  local project wiki.
- **Karpathy-style plain Markdown repo**: Maintain a compact, durable wiki of
  `docs/` as Markdown files with clear ownership and a central index.
- **Standard software docs directory**: Traditional user/API docs only.

## Decision

Adopt a **Karpathy-style durable Markdown repo** inside `docs/`, borrowing
lightweight memory conventions from local project wiki without forcing vault
tooling onto CI.col for avoiding doc drift (see
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
- Some advanced benefits from the local project wiki vault, such as claim health and
  contradiction reports, remain manual checks.
- Future agents should update [Documentation Map](../DOCUMENTATION_MAP.md)
  before adding new root docs or new top-level documentation categories.

## Revisit Trigger

Revisit if Rubato gains enough autonomous ingestion, generated docs, or
cross-agent write volume that plain Markdown conventions stop preventing drift.
