# Source: Karpathy-Style LLM Wiki Pattern

## Source

- Gist: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
- Summary reference: https://academy.dair.ai/blog/llm-knowledge-bases-karpathy

## Date Read

2026-06-14

## Why It Matters

Rubato is research-heavy and agent-driven. A curated Markdown docs directory
lets agents reuse compiled knowledge about papers, packages, score sources,
Yamaha setup, and rehearsal results.

## Key Facts

- The core pattern has three layers: raw sources, wiki/knowledge pages, and a
  schema file such as `AGENTS.md`.
- Raw sources are immutable inputs; the LLM reads them but does not rewrite
  them.
- Knowledge pages are compiled Markdown artifacts with summaries, entities,
  concepts, comparisons, overviews, and synthesis.
- The schema tells the agent how the knowledge base is structured and which
  workflows to follow.
- `index.md` is content-oriented: a catalog and routing file that agents read
  first before drilling into specific pages.
- `log.md` is chronological and append-only: ingests, queries, lint passes, and
  other maintenance events.
- Linting should find contradictions, stale claims, orphan pages, missing
  cross-references, missing concepts, and data gaps.
- The system compounds when agents update it after each meaningful discovery.
- Karpathy's gist intentionally leaves the exact directory structure and page
  schemas domain-specific.

## Caveats

- The docs knowledge base is only useful if maintained.
- It does not replace source code tests, run artifacts, or primary sources.

## Links

- Related guide: [Knowledge Guide](../KNOWLEDGE.md)
