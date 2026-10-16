"""Out-of-process vital-signs sampler.

Why its own process and not a thread: the failures worth measuring *are*
stalls. An in-process sampler is descheduled by the same GIL/GC event it is
supposed to record, so it goes blind exactly when its data matters and the
stall shows up as a gap in samples -- indistinguishable from the sampler simply
not running. Sampling from outside also detects a fully wedged conductor
(heartbeats stop advancing), which no in-process observer can report.
See ``docs/decisions/0011-realtime-multiprocess-architecture.md``.

The real-time path never knows this exists: workers do plain stores into the
shared :class:`GaugeBlock` (~0.4 us) and this process reads the block on its own
clock. Every gauge is a monotonic counter, so a slow, restarted, or entirely
absent sampler costs resolution, never information.

Output is a separate ``vitals.jsonl`` rather than the shared ``runtime.jsonl``:
two processes appending to one file interleave partial lines. Join the two on
``monotonic_time`` -- ``time.monotonic()`` is system-wide on macOS and Linux.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from aimusic.realtime.gauges import GaugeBlock, VitalsSnapshot

DEFAULT_CADENCE_SECONDS = 0.1


def snapshot_row(snapshot: VitalsSnapshot) -> dict[str, Any]:
    """One sampling instant as a flat, trace-shaped record."""

    return {
        "type": "vitals",
        "monotonic_time": snapshot.monotonic_time,
        "workers": {w.worker: asdict(w) for w in snapshot.workers},
    }


def _monitor_main(
    gauges: GaugeBlock,
    out_path: str,
    cadence_seconds: float,
    stop: Any,
) -> None:
    """Child entry point: sample the block until told to stop."""

    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Truncate rather than append: a retried take reuses its run directory, and
    # appending would interleave two attempts into one file with a backwards
    # jump in monotonic_time, which downstream analysis reads as one take.
    # Line-buffered so a crashed run still leaves every completed sample behind.
    with path.open("w", buffering=1) as handle:
        parent = mp.parent_process()
        next_at = time.monotonic()
        while not stop.is_set():
            # A hard native crash in the parent (CoreMIDI, CoreAudio, the BBCSO
            # host) never sets the stop event, and this is a daemon of a process
            # that no longer exists. Without this check it would spin forever
            # writing samples to disk.
            if parent is not None and not parent.is_alive():
                break
            row = snapshot_row(gauges.sample())
            handle.write(json.dumps(row) + "\n")
            # Absolute schedule, so a slow write does not make the cadence drift.
            next_at += cadence_seconds
            delay = next_at - time.monotonic()
            if delay < 0:
                next_at = time.monotonic()
                delay = 0.0
            stop.wait(timeout=delay)
        handle.write(json.dumps(snapshot_row(gauges.sample())) + "\n")


class VitalsMonitor:
    """Parent-side handle for the sampler process.

    Best-effort by construction: failing to start or stop the monitor must never
    fail a run, because it is an observer and nothing depends on its output.
    """

    def __init__(
        self,
        gauges: GaugeBlock,
        out_path: Path | str,
        *,
        cadence_seconds: float = DEFAULT_CADENCE_SECONDS,
        context: Any | None = None,
    ) -> None:
        if cadence_seconds <= 0:
            raise ValueError("cadence_seconds must be positive")
        self._ctx = context or mp.get_context("spawn")
        self._stop = self._ctx.Event()
        self._process = self._ctx.Process(
            target=_monitor_main,
            args=(gauges, str(out_path), cadence_seconds, self._stop),
            name="rubato-vitals-monitor",
            daemon=True,
        )
        self._closed = False
        self._process.start()

    @property
    def is_alive(self) -> bool:
        return self._process.is_alive()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        # Short: an observer must not hold up teardown of a live run. It has
        # nothing to flush -- the file is line-buffered.
        self._process.join(timeout=0.5)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=0.5)


__all__ = ["DEFAULT_CADENCE_SECONDS", "VitalsMonitor", "snapshot_row"]
