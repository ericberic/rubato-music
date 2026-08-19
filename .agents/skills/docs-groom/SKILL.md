# Docs Groom Skill

Use this skill when the user asks to groom, lint, compile, or update Rubato's
docs-as-memory knowledge base.

## Workflow

1. Read `docs/INDEX.md` first.
2. Read `docs/KNOWLEDGE.md` and `docs/DOCUMENTATION_MAP.md`.
3. Search existing docs with `rg` before creating new pages.
4. Merge duplicated knowledge into the owning doc rather than creating near
   duplicates.
5. Keep source-specific facts in `docs/sources/`, synthesis in
   `docs/concepts/`, durable choices in `docs/decisions/`, and repeated human
   procedures in `docs/runbooks/`.
6. Keep committed docs Obsidian-friendly but not Obsidian-dependent. Use
   standard relative Markdown links, not wikilinks.
7. Run `make docs-health` before finishing documentation-heavy work.
8. Append a concise entry to `docs/LOG.md` for substantial grooms, research
   ingests, or convention changes.

## Agent Discovery

This skill's canonical path is `.agents/skills/docs-groom/SKILL.md`.
`.claude/skills` is a symlink to `.agents/skills` so Claude and Codex-style
agents share the same instructions. Keep paths repo-relative and avoid
agent-specific assumptions.

## Checks

`make docs-health` verifies:

- broken local Markdown links
- docs pages unreachable from `docs/INDEX.md`
- committed Obsidian wikilinks
- `git diff --check`

It also reports non-blocking page-size and stale-scope warnings for manual
review.
