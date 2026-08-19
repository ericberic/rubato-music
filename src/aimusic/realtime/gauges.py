"""Consolidated vital-signs gauges for the real-time runtime.

Every worker publishes into **one** shared block instead of scattering logs
through its own data structures. Writers only do plain stores (no allocation,
no lock, no I/O), so publishing is safe on the hot path; a separate monitor
samples the whole block on its own clock and stamps each snapshot with a single
timestamp, which is what makes cross-worker trend analysis meaningful.

Counters are monotonic: the monitor differences successive samples to get rates,
so no worker ever resets shared state. See
``docs/decisions/0011-realtime-multiprocess-architecture.md``.
"""

from __future__ import annotations

import time
from ctypes import Structure, c_double, c_int64
from dataclasses import dataclass
from multiprocessing import Value
from typing import Any

# One slot per worker. Fixed layout keeps the block picklable across a process
# boundary and makes a snapshot a plain field read.
WORKERS = ("input", "follower", "conductor", "output")


class _WorkerGauges(Structure):
    _fields_ = [
        ("heartbeat", c_double),  # monotonic time of the worker's last iteration
        ("iterations", c_int64),  # monotonic count of loop iterations
        ("events", c_int64),  # monotonic count of domain events handled
        ("queue_depth", c_int64),  # current inbound backlog
        ("queue_high_watermark", c_int64),  # max backlog ever seen
        ("dropped", c_int64),  # events shed under backpressure
        ("busy_seconds", c_double),  # cumulative work time (for duty cycle)
        ("last_work_seconds", c_double),  # most recent iteration's work time
    ]


@dataclass(frozen=True)
class WorkerSample:
    """One worker's vitals at a single sampling instant."""

    worker: str
    heartbeat_age_ms: float | None
    iterations: int
    events: int
    queue_depth: int
    queue_high_watermark: int
    dropped: int
    busy_seconds: float
    last_work_ms: float


@dataclass(frozen=True)
class VitalsSnapshot:
    """All workers sampled under one timestamp (a near-coherent cut)."""

    monotonic_time: float
    workers: tuple[WorkerSample, ...]

    def worker(self, name: str) -> WorkerSample | None:
        return next((w for w in self.workers if w.worker == name), None)


class GaugeBlock:
    """Shared vitals for all workers; safe to pass to a child process."""

    def __init__(self) -> None:
        # lock=False: stores are plain writes. Torn reads are acceptable at a
        # 50-100 ms sampling cadence (they self-correct on the next tick) and
        # avoid putting a lock anywhere near the hot path.
        self._slots = {name: Value(_WorkerGauges, lock=False) for name in WORKERS}

    def publisher(self, worker: str) -> "GaugePublisher":
        if worker not in self._slots:
            raise KeyError(f"unknown worker {worker!r}; expected one of {WORKERS}")
        return GaugePublisher(self._slots[worker])

    def sample(self, now: float | None = None) -> VitalsSnapshot:
        """Read every worker's gauges under one timestamp."""

        now = time.monotonic() if now is None else now
        samples = []
        for name in WORKERS:
            slot = self._slots[name]
            heartbeat = slot.heartbeat
            samples.append(
                WorkerSample(
                    worker=name,
                    heartbeat_age_ms=((now - heartbeat) * 1000 if heartbeat else None),
                    iterations=slot.iterations,
                    events=slot.events,
                    queue_depth=slot.queue_depth,
                    queue_high_watermark=slot.queue_high_watermark,
                    dropped=slot.dropped,
                    busy_seconds=slot.busy_seconds,
                    last_work_ms=slot.last_work_seconds * 1000,
                )
            )
        return VitalsSnapshot(monotonic_time=now, workers=tuple(samples))


class GaugePublisher:
    """One worker's write handle. Every method is a plain store."""

    def __init__(self, slot: Any) -> None:
        self._slot = slot

    def beat(self, *, now: float, work_seconds: float = 0.0, events: int = 0) -> None:
        """Record one loop iteration."""

        slot = self._slot
        slot.heartbeat = now
        slot.iterations += 1
        slot.last_work_seconds = work_seconds
        slot.busy_seconds += work_seconds
        if events:
            slot.events += events

    def set_queue_depth(self, depth: int) -> None:
        slot = self._slot
        slot.queue_depth = depth
        if depth > slot.queue_high_watermark:
            slot.queue_high_watermark = depth

    def drop(self, count: int = 1) -> None:
        self._slot.dropped += count


__all__ = [
    "GaugeBlock",
    "GaugePublisher",
    "VitalsSnapshot",
    "WORKERS",
    "WorkerSample",
]
