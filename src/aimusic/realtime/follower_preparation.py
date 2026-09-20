"""One disposable, prepared follower; no MIDI ports or previous-take state."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import Callable, Literal

from pydantic import BaseModel, Field

from aimusic.realtime.follower_process import FollowerSpec, ProcessFollower


class FollowerPreparationStatus(BaseModel):
    state: Literal["not_loaded", "preparing", "ready", "in_use", "failed"] = "not_loaded"
    message: str = "Follower preparation has not started"
    preparation_id: str | None = None
    elapsed_seconds: float = 0.0
    timings_ms: dict[str, float] = Field(default_factory=dict)


class FollowerPreparation:
    """Single owner, generation-guarded preparation and one-shot checkout.

    Construction never holds the lock. Superseded/cancelled builders dispose
    their own child. A claimed child is never returned to the ready slot.
    """

    def __init__(
        self, factory: Callable = ProcessFollower, observer: Callable = lambda status: None
    ):
        self._factory = factory
        self._observer = observer
        self._condition = threading.Condition(threading.RLock())
        self._status = FollowerPreparationStatus()
        self._spec: FollowerSpec | None = None
        self._identity: object = None
        self._child = None
        self._cancel = threading.Event()
        self._failure: Exception | None = None
        self._closed = False
        self._leased = False
        self._started = 0.0
        self._thread: threading.Thread | None = None

    def status(self) -> FollowerPreparationStatus:
        with self._condition:
            if self._status.state == "ready" and not self._child.is_alive:
                self._status = self._status.model_copy(
                    update={
                        "state": "failed",
                        "message": "Prepared follower exited; retry preparation",
                    }
                )
                self._observer(self._status)
            result = self._status.model_copy(deep=True)
            if result.state == "preparing":
                result.elapsed_seconds = time.monotonic() - self._started
            return result

    def prepare(self, spec: FollowerSpec, identity: object, *, force: bool = False):
        # Position and lock threshold are cheap, applied only at checkout.
        spec = replace(spec, initial_reference_beat=None, minimum_lock_updates=3)
        with self._condition:
            if self._closed:
                raise RuntimeError("Follower preparation is shut down")
            same = spec == self._spec and identity == self._identity
            self.status()
            if self._leased:
                self._spec, self._identity = spec, identity
                return self.status()
            if same and (
                self._status.state in {"preparing", "ready"}
                or (self._status.state == "failed" and not force)
            ):
                return self.status()
            self._failure = None
            self._cancel.set()
            old = self._child
            self._child = None
            self._spec, self._identity = spec, identity
            cancel = self._cancel = threading.Event()
            self._started = time.monotonic()
            self._status = FollowerPreparationStatus(
                state="preparing",
                message="Preparing the score follower",
                preparation_id=f"follower-preload-{time.time_ns()}",
            )
            self._observer(self._status)
            self._thread = threading.Thread(
                target=self._build,
                args=(spec, cancel, old),
                daemon=True,
                name="rubato-follower-preparation",
            )
            self._thread.start()
            return self.status()

    def _build(self, spec, cancel, old):
        child = None
        try:
            if old is not None:
                old.close()
            child = self._factory(spec, cancel_event=cancel)
            with self._condition:
                if cancel.is_set() or self._closed:
                    return
                self._child = child
                child = None
                self._status = self._status.model_copy(
                    update={
                        "state": "ready",
                        "message": "Score follower ready",
                        "elapsed_seconds": time.monotonic() - self._started,
                        "timings_ms": self._child.startup_timings_ms,
                    }
                )
                self._observer(self._status)
                self._condition.notify_all()
        except Exception as exc:
            with self._condition:
                if not cancel.is_set() and not self._closed:
                    self._failure = exc
                    self._status = self._status.model_copy(
                        update={
                            "state": "failed",
                            "message": f"Follower preparation failed: {exc}",
                            "elapsed_seconds": time.monotonic() - self._started,
                        }
                    )
                    self._observer(self._status)
                    self._condition.notify_all()
        finally:
            if child is not None:
                child.close()

    def claim(self, spec, identity, stop_event):
        self.prepare(spec, identity)
        with self._condition:
            while self._status.state == "preparing":
                if stop_event.is_set() or self._closed:
                    raise InterruptedError("Follower startup cancelled")
                self._condition.wait(timeout=0.05)
            if stop_event.is_set() or self._closed:
                raise InterruptedError("Follower startup cancelled")
            if self._leased or self.status().state != "ready":
                raise RuntimeError(self._status.message) from self._failure
            expected = replace(spec, initial_reference_beat=None, minimum_lock_updates=3)
            if self._spec != expected or self._identity != identity:
                raise RuntimeError("Follower preparation changed; retry Go live")
            child, self._child = self._child, None
            self._leased = True
            self._status = self._status.model_copy(
                update={
                    "state": "in_use",
                    "message": "Score follower in use",
                }
            )
            self._observer(self._status)
        try:
            child.configure_entry(spec.initial_reference_beat, spec.minimum_lock_updates)
        except BaseException:
            child.close()
            self.release()
            raise
        return child

    def release(self):
        with self._condition:
            self._leased = False
            if not self._closed and self._spec is not None:
                self.prepare(self._spec, self._identity, force=True)

    def close(self):
        with self._condition:
            self._closed = True
            self._status = FollowerPreparationStatus(message="Follower preparation is shut down")
            self._cancel.set()
            child, self._child = self._child, None
            self._condition.notify_all()
        if child is not None:
            child.close()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
