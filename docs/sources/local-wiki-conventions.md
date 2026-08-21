# Source: Local Wiki Conventions

## Source

- `~/code/project-wiki/AGENTS.md`
- `~/code/project-wiki/wiki/AGENTS.md`
- `~/code/project-wiki/wiki/SCHEMA.md`
- `~/code/project-wiki/.agents/skills/wiki-curator/SKILL.md`
- `~/code/project-wiki/.agents/skills/wiki-lint/SKILL.md`
- `~/code/personal-docs/AGENTS.md`
- `~/code/personal-docs/docs/DOCUMENTATION_MAP.md`
- `~/code/personal-docs/docs/WIKI_INGEST_BOUNDARY.md`
- `~/code/personal-docs/docs/SELF_IMPROVEMENT.md`

## Date Read

2026-07-06

## Why It Matters

The soloist asked whether Rubato should borrow conventions from local project
wiki and `my-life` documentation systems so Rubato's `docs/` directory can act
as compounding agent memory during ML/accompaniment development.

## Key Facts

- The local project wiki uses a full memory-wiki/Obsidian-style vault with `entities/`,
  `concepts/`, `syntheses/`, `sources/`, `reports/`, and `views/`.
- Its strongest reusable curation rules are: merge over create, search before
  creating, one fact in one place, source immutability, path-of-entry linkage,
  and cross-link density.
- The local project wiki also uses structured frontmatter claims, entity schemas,
  Dataview dashboards, generated related blocks, and compile output.
- `my-life` keeps a lightweight `DOCUMENTATION_MAP.md` that defines which doc
  owns which kind of knowledge and says to update the owning doc first.
- `my-life` separates runtime wiki mutation boundaries from repo-development
  edits. That matters less for Rubato because Rubato has no OpenClaw wiki
  transaction layer.
- `my-life`'s self-improvement docs frame durable memory as layered evidence:
  raw episodes, reviewed semantic truth, and project/system state.

## Caveats

- The full local project wiki stack is too heavy for Rubato right now.
  Rubato does not need Obsidian dashboards, managed compile blocks, structured
  claims, or generated reports for the Chopin accompanist MVP.
- Rubato's `docs/` directory is project memory, not a personal knowledge vault.
  The taxonomy should stay oriented around product, architecture, sources,
  decisions, runbooks, and experiments.
- If Rubato later develops many autonomous agents or generated docs, this
  decision should be revisited.

## Links

- Related guide: [Knowledge Guide](../KNOWLEDGE.md)
- Related map: [Documentation Map](../DOCUMENTATION_MAP.md)
- Related decision: [Decision 0004](../decisions/0004-docs-memory-conventions.md)
