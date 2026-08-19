"""Shared constants for core utilities."""

from __future__ import annotations

import os
from pathlib import Path


def _resolve_project_root() -> Path:
    override = os.environ.get("AIMUSIC_PROJECT_ROOT")
    if override:
        return Path(override).expanduser().resolve()

    candidate = Path(__file__).resolve()
    for parent in candidate.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    # Fallback to package-relative root (site-packages install)
    return candidate.parents[3]


PROJECT_ROOT = _resolve_project_root()


__all__ = ["PROJECT_ROOT"]
