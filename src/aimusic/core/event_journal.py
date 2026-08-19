"""Durable local journal for rehearsal lifecycle events and failures.

The WebSocket feed is intentionally ephemeral, but debugging a rehearsal must
not depend on a browser having been connected.  This module appends compact
JSON Lines envelopes to the local data root so event order and correlated
failures can be reconstructed after the fact.
"""

from __future__ import annotations

import json
import logging
import threading
import traceback
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from aimusic.core import paths
from aimusic.core.time import utc_now

_APPEND_LOCK = threading.Lock()
LOGGER = logging.getLogger(__name__)


def event_journal_path() -> Path:
    """Return the process-wide local rehearsal journal path."""

    return paths.data_root() / "logs" / "rehearsal-events.jsonl"


def append_event(event: BaseModel) -> None:
    """Persist a typed WebSocket event in an ordered diagnostic envelope."""

    payload = event.model_dump(mode="json")
    status = payload.get("status")
    nested_status = status if isinstance(status, dict) else {}
    _append(
        {
            **_envelope(payload.get("type", event.__class__.__name__)),
            **_present(
                piece_id=payload.get("piece_id"),
                movement=payload.get("movement"),
                take_id=payload.get("take_id"),
                session_id=payload.get("session_id") or nested_status.get("session_id"),
                run_id=payload.get("run_id") or nested_status.get("run_id"),
                job_id=payload.get("job_id"),
            ),
            "payload": payload,
        }
    )


def append_exception(event_type: str, exc: BaseException, **context: Any) -> None:
    """Persist an exception, traceback, and correlation context."""

    _append(
        {
            **_envelope(event_type),
            **_present(**context),
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                ),
            },
        }
    )


def append_diagnostic(event_type: str, *, payload: dict[str, Any], **context: Any) -> None:
    """Append a non-WebSocket diagnostic event with correlation context."""

    _append(
        {
            **_envelope(event_type),
            **_present(**context),
            "payload": payload,
        }
    )


def journal_exception(event_type: str, exc: BaseException, **context: Any) -> None:
    """Best-effort exception journaling for critical worker error paths."""

    try:
        append_exception(event_type, exc, **context)
    except Exception:
        LOGGER.exception("Failed to append diagnostic event %s", event_type)


def _envelope(event_type: str) -> dict[str, str]:
    return {
        "recorded_at": utc_now().isoformat(),
        "event_id": str(uuid4()),
        "event_type": event_type,
    }


def _present(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _append(record: dict[str, Any]) -> None:
    path = event_journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, default=str, separators=(",", ":")) + "\n"
    with _APPEND_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)


__all__ = [
    "append_event",
    "append_diagnostic",
    "append_exception",
    "event_journal_path",
    "journal_exception",
]
