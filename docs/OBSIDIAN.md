# Obsidian Review

Rubato docs should be comfortable to review in Obsidian on the soloist's MacBook while
remaining plain Markdown that works in GitHub, terminals, and agent context.

## Recommendation

Open the repository root or the `docs/` directory as an Obsidian vault. Do not
commit `.obsidian/` workspace settings unless the team later decides to share a
specific vault configuration.

## Markdown Conventions

- Use standard relative Markdown links like `[System Design](SYSTEM_DESIGN.md)`.
- Do not use Obsidian-only wikilinks in committed docs.
- Use stable descriptive filenames; avoid renaming docs unless links are updated.
- Use headings as the main outline. Obsidian's outline pane should make each
  page reviewable without custom plugins.
- Use fenced `mermaid` diagrams when diagrams are useful; they still render on
  GitHub and in many Markdown tools.
- Avoid Dataview queries, embedded canvases, plugin-specific callouts, and
  generated blocks in committed docs.
- Keep docs readable as source text. Agents should not need Obsidian to use the
  knowledge base.

## Review Workflow

1. Open [INDEX](INDEX.md) first.
2. Follow links to the design, concept, source, decision, or runbook page under
   review.
3. Leave feedback in GitHub PR review, a direct prompt, or a follow-up note that
   an agent can compile into the owning doc.

## Related

- [Knowledge Guide](KNOWLEDGE.md)
- [Documentation Map](DOCUMENTATION_MAP.md)
- [Decision 0004](decisions/0004-docs-memory-conventions.md)
