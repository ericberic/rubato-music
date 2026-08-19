"""Lightweight configuration loader for live-accompanist modules.

The config loader reads ``configs/params.yaml`` once and serves copies so
callers can make local modifications without mutating the shared cache.
This keeps local simulations deterministic and gives DVC-compatible tooling one
stable parameter source when experiment stages are reintroduced.
"""

from __future__ import annotations

from copy import deepcopy
from threading import Lock
from typing import Any, Dict

import yaml

from aimusic.core.constants import PROJECT_ROOT

_PARAMS_PATH = PROJECT_ROOT / "configs" / "params.yaml"
_PARAMS_CACHE: Dict[str, Any] | None = None
_CACHE_LOCK = Lock()


def _load_raw_params() -> Dict[str, Any]:
    """Read and cache the YAML params file from disk."""

    global _PARAMS_CACHE
    if _PARAMS_CACHE is None:
        with _CACHE_LOCK:
            if _PARAMS_CACHE is None:
                if not _PARAMS_PATH.exists():  # pragma: no cover - defensive
                    raise FileNotFoundError(f"Expected params file at {_PARAMS_PATH}")
                with _PARAMS_PATH.open("r", encoding="utf-8") as handle:
                    _PARAMS_CACHE = yaml.safe_load(handle) or {}
    return _PARAMS_CACHE


def load_params() -> Dict[str, Any]:
    """Return a deepcopy of the cached params dictionary."""

    return deepcopy(_load_raw_params())


def resolve_session_id(explicit: str | None = None) -> str:
    """Determine which session_id to use for the current invocation."""

    if explicit and explicit.strip():
        return explicit.strip()
    params = load_params()
    return params.get("session", {}).get("session_id", "default")


__all__ = ["load_params", "resolve_session_id"]
