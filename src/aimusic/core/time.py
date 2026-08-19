"""Shared UTC-now helper.

Collapses the three duplicate strftime-based `_utc_now()` copies
(`aimusic.takes.store`, `aimusic.server.live_control`, and
`aimusic.takes.profile.fit_interpretation_for`'s inline `updated` timestamp) into one
`datetime`-returning helper (design doc
docs/design/SCHEMA_VALIDATION_ARCH.md §2.1/§3). Every persisted or API
timestamp is now a timezone-aware `datetime` field end-to-end: Pydantic
serializes it as ISO-8601 and stamps `format: date-time` into the OpenAPI
spec, instead of the old unconstrained `type: string` that no generated
type or validator could ever check.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    """The current time, timezone-aware, in UTC."""

    return datetime.now(timezone.utc)


__all__ = ["utc_now"]
