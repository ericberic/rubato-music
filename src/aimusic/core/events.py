"""In-process pub/sub for `WS /api/events` (design doc §2.7).

The design doc specifies the socket carries "take status transitions,
coverage updates" and that recording must never be blocked by analysis. The
alignment worker (`aimusic.takes.aligner.AlignmentWorker`) runs its work on a
background thread, so publishing must be safe to call from any thread, not
just the asyncio event loop the FastAPI app runs on.

Deliberately minimal: no external broker or WebSocket replay, matching the
existing single-process `LiveControl`/`AlignmentWorker` philosophy. A dropped
connection just stops receiving events; a reconnecting client re-subscribes
and gets a fresh feed (the frontend re-fetches take/coverage state via REST).
Every publication is also appended to the local diagnostic event journal,
including when no browser is connected.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from pydantic import BaseModel

from aimusic.core.event_journal import append_event

LOGGER = logging.getLogger(__name__)


class EventBroadcaster:
    """Broadcasts JSON-serializable events to all connected WebSocket clients."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: set[asyncio.Queue[str | None]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Record the event loop that owns the connected clients' queues.

        Called once at app startup. `publish` uses this to hand events to the
        loop from whatever thread it's called on.
        """

        with self._lock:
            self._loop = loop

    def subscribe(self) -> asyncio.Queue[str | None]:
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        with self._lock:
            self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str | None]) -> None:
        with self._lock:
            self._queues.discard(queue)

    def publish(self, event: BaseModel) -> None:
        """Send `event` to every connected client. Safe to call from any thread.

        `event` is a typed Pydantic model (e.g. `aimusic.server.schemas.
        TakeEvent`'s members), not a hand-built dict -- design doc
        docs/design/SCHEMA_VALIDATION_ARCH.md §2.1 (issue #85): the WS wire
        contract is the model's own JSON serialization, not a re-derived
        dict literal at each publish site.

        WebSocket delivery is a no-op if no event loop is bound, but the event
        is still journaled. Journal failures are logged and never allowed to
        block recording or analysis.
        """

        try:
            append_event(event)
        except Exception:
            LOGGER.exception("Failed to append rehearsal event %s", event.__class__.__name__)

        with self._lock:
            loop = self._loop
            queues = list(self._queues)
        if loop is None or not queues:
            return
        payload = event.model_dump_json()
        for queue in queues:
            loop.call_soon_threadsafe(queue.put_nowait, payload)

    def shutdown(self) -> None:
        """Wake every socket handler so application shutdown can complete."""

        with self._lock:
            loop = self._loop
            queues = list(self._queues)
            self._loop = None
        if loop is None:
            return
        for queue in queues:
            loop.call_soon_threadsafe(queue.put_nowait, None)


events = EventBroadcaster()


__all__ = ["EventBroadcaster", "events"]
