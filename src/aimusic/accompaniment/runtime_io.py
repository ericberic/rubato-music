"""Clock, replay-input, output, and trace I/O seams for the live runtime."""

from __future__ import annotations

import dataclasses
import json
import queue
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from aimusic.accompaniment.following import PerformedNote
from aimusic.accompaniment.scheduler import ScheduledAccompanimentEvent


class MonotonicClock(Protocol):
    def now(self) -> float: ...

    def wait_until(self, deadline: float) -> None: ...


class SystemMonotonicClock:
    def now(self) -> float:
        return time.monotonic()

    def wait_until(self, deadline: float) -> None:
        delay = deadline - self.now()
        if delay > 0:
            time.sleep(delay)


class ManualClock:
    """Deterministic monotonic clock for replay and tests."""

    def __init__(self, initial_time: float = 0.0) -> None:
        if initial_time < 0:
            raise ValueError("initial_time must be non-negative")
        self._time = initial_time

    def now(self) -> float:
        return self._time

    def wait_until(self, deadline: float) -> None:
        self.advance_to(deadline)

    def advance_to(self, deadline: float) -> None:
        if deadline < self._time:
            raise ValueError("monotonic clock cannot move backward")
        self._time = deadline


class NoteInput(Protocol):
    """Source of timestamped note observations (recording or live adapter)."""

    def __iter__(self) -> Iterator[PerformedNote]: ...


class RecordedNoteReplay:
    """Replay recorded observations against any monotonic clock."""

    def __init__(self, notes: Iterable[PerformedNote]) -> None:
        self._notes = tuple(sorted(notes, key=lambda note: note.perf_time))

    def __iter__(self) -> Iterator[PerformedNote]:
        return iter(self._notes)

    def run(
        self,
        consume: Callable[[PerformedNote], None],
        *,
        clock: MonotonicClock,
        origin: float | None = None,
    ) -> None:
        if not self._notes:
            return
        start = clock.now() if origin is None else origin
        first_perf_time = self._notes[0].perf_time
        for note in self._notes:
            deadline = start + note.perf_time - first_perf_time
            clock.wait_until(deadline)
            # Recorded file timestamps are relative to that take. Translate
            # them into the runtime clock domain exactly as a live input
            # adapter would timestamp arrival.
            consume(dataclasses.replace(note, perf_time=deadline))


class CapturingOutput:
    """Deterministic output used by replay, CI, and engine integration tests."""

    def __init__(self, *, output_advance_ms: float = 0.0) -> None:
        self.sent: list[tuple[float, ScheduledAccompanimentEvent]] = []
        self.panics: list[tuple[float, str]] = []
        self.master_volumes: list[tuple[float, float]] = []
        self.output_advance_changes: list[float] = []
        self.release_retimes: list[tuple[str, float]] = []
        self._output_advance_seconds = output_advance_ms / 1000.0

    def send(self, event: ScheduledAccompanimentEvent, *, sent_at: float) -> None:
        _ = sent_at
        self.sent.append((event.perf_time - self._output_advance_seconds, event))

    def cancel_pending(self, event_ids: Iterable[str]) -> bool:
        """Captured sends model already-delivered output, not a deadline queue."""

        return not tuple(event_ids)

    def retime_release(self, event_id: str, *, scheduled_at: float) -> bool:
        if not any(event.event.event_id == event_id for _, event in self.sent):
            return False
        self.release_retimes.append((event_id, scheduled_at))
        return True

    def panic(self, *, sent_at: float, reason: str) -> None:
        self.panics.append((sent_at, reason))

    def set_master_volume(self, volume: float, *, sent_at: float) -> None:
        self.master_volumes.append((sent_at, volume))

    def set_output_advance(self, output_advance_ms: float) -> None:
        if output_advance_ms < 0:
            raise ValueError("output_advance_ms must be non-negative")
        self.output_advance_changes.append(output_advance_ms)
        self._output_advance_seconds = output_advance_ms / 1000.0


class TraceSink(Protocol):
    def write(self, record: BaseModel) -> None: ...


class NullTraceSink:
    def write(self, record: BaseModel) -> None:
        _ = record


class MemoryTraceSink:
    def __init__(self) -> None:
        self.records: list[BaseModel] = []

    def write(self, record: BaseModel) -> None:
        self.records.append(record)


class JsonlTraceSink:
    """Write validated trace rows on a bounded, non-real-time worker.

    Runtime threads only enqueue immutable Pydantic models.  JSON encoding and
    filesystem I/O happen on the writer thread, so a slow disk cannot hold a
    score-follower or MIDI-output lock.  ``close`` is the durability boundary:
    it drains all accepted rows and reports any writer failure or overload.
    """

    _SENTINEL = object()

    def __init__(self, path: Path | str, *, max_queue_size: int = 32_768) -> None:
        if max_queue_size <= 0:
            raise ValueError("max_queue_size must be positive")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue[BaseModel | object] = queue.Queue(max_queue_size)
        self._state_lock = threading.Lock()
        self._closed = False
        self._error: BaseException | None = None
        self._dropped_rows = 0
        self._worker = threading.Thread(
            target=self._writer,
            name="rubato-trace-writer",
            daemon=True,
        )
        self._worker.start()

    def write(self, record: BaseModel) -> None:
        with self._state_lock:
            self._raise_if_unavailable()
            try:
                self._queue.put_nowait(record)
            except queue.Full:
                # Blocking here would let trace I/O become part of the
                # performance deadline path.  Surface overload at close.
                self._dropped_rows += 1

    @property
    def dropped_rows(self) -> int:
        with self._state_lock:
            return self._dropped_rows

    def flush(self) -> None:
        """Wait until every accepted row has reached the writer."""

        self._queue.join()
        with self._state_lock:
            self._raise_if_failed()

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                self._raise_if_failed()
                return
            self._closed = True
        # Shutdown may block; it is deliberately outside the performance path.
        self._queue.put(self._SENTINEL)
        self._worker.join()
        with self._state_lock:
            self._raise_if_failed()
            if self._dropped_rows:
                raise RuntimeError(f"Trace queue overflowed; dropped {self._dropped_rows} row(s)")

    def _writer(self) -> None:
        try:
            with self.path.open("a", encoding="utf-8", buffering=1) as handle:
                while True:
                    item = self._queue.get()
                    try:
                        if item is self._SENTINEL:
                            return
                        assert isinstance(item, BaseModel)
                        line = json.dumps(
                            item.model_dump(mode="json"),
                            separators=(",", ":"),
                        )
                        handle.write(line + "\n")
                    finally:
                        self._queue.task_done()
        except BaseException as exc:
            with self._state_lock:
                self._error = exc
            # Unblock flush/close even after a filesystem or serialization
            # failure.  The original exception is re-raised by the caller.
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
                else:
                    self._queue.task_done()

    def _raise_if_unavailable(self) -> None:
        self._raise_if_failed()
        if self._closed:
            raise RuntimeError("Trace sink is closed")

    def _raise_if_failed(self) -> None:
        if self._error is not None:
            raise RuntimeError("Trace writer failed") from self._error
