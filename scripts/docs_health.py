#!/usr/bin/env python3
"""Lightweight docs-as-memory health checks for Rubato."""

from __future__ import annotations

import re
import subprocess
import sys
from collections import deque
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
INDEX = DOCS / "INDEX.md"

MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
URL_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
STALE_SCOPE_TERMS = [
    "docs/wiki",
    "wiki/README",
    "ERIC vs OTHER",
    "style transfer",
]


def strip_code_fences(text: str) -> str:
    return CODE_FENCE.sub("", text)


def markdown_files() -> list[Path]:
    return sorted(DOCS.rglob("*.md")) + [ROOT / "AGENTS.md"]


def is_external(link: str) -> bool:
    return bool(URL_SCHEME.match(link))


def local_link_target(source: Path, link: str) -> Path | None:
    target = unquote(link.split("#", 1)[0].strip())
    if not target or is_external(target):
        return None
    if target.startswith("/"):
        return Path(target)
    return (source.parent / target).resolve()


def local_doc_links(source: Path) -> list[Path]:
    text = strip_code_fences(source.read_text())
    links: list[Path] = []
    for match in MARKDOWN_LINK.finditer(text):
        target = local_link_target(source, match.group(1))
        if target is not None and DOCS in target.parents and target.suffix == ".md":
            links.append(target)
    return links


def check_links() -> list[str]:
    errors: list[str] = []
    for path in markdown_files():
        text = strip_code_fences(path.read_text())
        for match in WIKILINK.finditer(text):
            errors.append(
                f"{path.relative_to(ROOT)}: Obsidian wikilink {match.group(0)!r}; "
                "use a relative Markdown link instead"
            )
        for match in MARKDOWN_LINK.finditer(text):
            link = match.group(1).strip()
            target = local_link_target(path, link)
            if target is None:
                continue
            if not target.exists():
                errors.append(f"{path.relative_to(ROOT)}: missing link target {link!r}")
    return errors


def check_reachability() -> list[str]:
    docs = {path.resolve() for path in DOCS.rglob("*.md")}
    seen: set[Path] = set()
    queue: deque[Path] = deque([INDEX.resolve()])
    while queue:
        path = queue.popleft()
        if path in seen or path not in docs:
            continue
        seen.add(path)
        for target in local_doc_links(path):
            if target not in seen:
                queue.append(target)

    unreachable = sorted(docs - seen)
    return [
        f"{path.relative_to(ROOT)}: not reachable from docs/INDEX.md"
        for path in unreachable
    ]


def page_size_report() -> list[str]:
    warnings: list[str] = []
    for path in sorted(DOCS.rglob("*.md")):
        text = path.read_text()
        words = re.findall(r"\S+", strip_code_fences(text))
        lines = text.splitlines()
        if len(words) > 1000 or len(lines) > 200:
            warnings.append(
                f"{path.relative_to(ROOT)}: {len(words)} words, {len(lines)} lines; "
                "consider splitting if it is not a root overview"
            )
    return warnings


def stale_scope_report() -> list[str]:
    warnings: list[str] = []
    for path in markdown_files():
        text = strip_code_fences(path.read_text())
        for line_no, line in enumerate(text.splitlines(), start=1):
            for term in STALE_SCOPE_TERMS:
                if term in line:
                    warnings.append(f"{path.relative_to(ROOT)}:{line_no}: contains {term!r}")
    return warnings


def check_git_diff_whitespace() -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--check"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if proc.returncode == 0:
        return []
    return ["git diff --check failed:", proc.stdout.rstrip()]


def main() -> int:
    errors = check_links()
    errors.extend(check_reachability())
    errors.extend(check_git_diff_whitespace())
    warnings = page_size_report()
    warnings.extend(stale_scope_report())

    if warnings:
        print("docs-health warnings:")
        for warning in warnings:
            print(f"  - {warning}")

    if errors:
        print("docs-health errors:")
        for error in errors:
            print(f"  - {error}")
        return 1

    print("docs-health ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
