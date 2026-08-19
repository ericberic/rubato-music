# Source: Local Wiki Conventions

## Source

- `/Users/ehuang/code/jesse-and-eric/AGENTS.md`
- `/Users/ehuang/code/jesse-and-eric/wiki/AGENTS.md`
- `/Users/ehuang/code/jesse-and-eric/wiki/SCHEMA.md`
- `/Users/ehuang/code/jesse-and-eric/.agents/skills/wiki-curator/SKILL.md`
- `/Users/ehuang/code/jesse-and-eric/.agents/skills/wiki-lint/SKILL.md`
- `/Users/ehuang/code/my-life/AGENTS.md`
- `/Users/ehuang/code/my-life/docs/DOCUMENTATION_MAP.md`
- `/Users/ehuang/code/my-life/docs/WIKI_INGEST_BOUNDARY.md`
- `/Users/ehuang/code/my-life/docs/SELF_IMPROVEMENT.md`

## Date Read

2026-07-06

## Why It Matters

Eric asked whether Rubato should borrow conventions from the local Jesse & Eric
wiki and `my-life` documentation systems so Rubato's `docs/` directory can act
as compounding agent memory during ML/accompaniment development.

## Key Facts

- Jesse & Eric uses a full memory-wiki/Obsidian-style vault with `entities/`,
  `concepts/`, `syntheses/`, `sources/`, `reports/`, and `views/`.
- Its strongest reusable curation rules are: merge over create, search before
  creating, one fact in one place, source immutability, path-of-entry linkage,
  and cross-link density.
- Jesse & Eric also uses structured frontmatter claims, entity schemas,
  Dataview dashboards, generated related blocks, and compile output.
- `my-life` keeps a lightweight `DOCUMENTATION_MAP.md` that defines which doc
  owns which kind of knowledge and says to update the owning doc first.
- `my-life` separates runtime wiki mutation boundaries from repo-development
  edits. That matters less for Rubato because Rubato has no OpenClaw wiki
  transaction layer.
- `my-life`'s self-improvement docs frame durable memory as layered evidence:
  raw episodes, reviewed semantic truth, and project/system state.

## Caveats

- The full Jesse & Eric memory-wiki stack is too heavy for Rubato right now.
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
