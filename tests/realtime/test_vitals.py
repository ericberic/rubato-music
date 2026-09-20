"""Gauges and the out-of-process vitals monitor.

These are deterministic and hardware-free: the monitor is exercised against a
gauge block written by the test itself, so no follower or MIDI device is needed.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import time
from pathlib import Path

import pytest

from aimusic.realtime.gauges import WORKERS, GaugeBlock
from aimusic.realtime.monitor import VitalsMonitor, snapshot_row


def test_queue_depth_is_derived_from_counters_not_qsize() -> None:
    """Depth must not depend on ``mp.Queue.qsize()``.

    qsize raises NotImplementedError on macOS, which silently pinned the old
    gauge to zero on the only machine that runs performances.
    """

    gauges = GaugeBlock()
    publisher = gauges.publisher("follower")
    for _ in range(5):
        publisher.enqueued()
    publisher.dequeued()
    publisher.dequeued()

    follower = gauges.sample().worker("follower")
    assert follower is not None
    assert follower.queue_depth == 3
    assert follower.queue_high_watermark == 4  # depth seen at the first dequeue


def test_queue_depth_never_reports_negative() -> None:
    gauges = GaugeBlock()
    publisher = gauges.publisher("follower")
    publisher.dequeued()  # a sentinel is consumed without a matching enqueue
    follower = gauges.sample().worker("follower")
    assert follower is not None
    assert follower.queue_depth == 0


def test_beat_accumulates_work_and_heartbeat() -> None:
    gauges = GaugeBlock()
    publisher = gauges.publisher("follower")
    publisher.beat(now=time.monotonic(), work_seconds=0.010, events=1)
    publisher.beat(now=time.monotonic(), work_seconds=0.030, events=1)

    follower = gauges.sample().worker("follower")
    assert follower is not None
    assert follower.iterations == 2
    assert follower.events == 2
    assert follower.last_work_ms == pytest.approx(30.0, abs=1e-6)
    assert follower.busy_seconds == pytest.approx(0.040, abs=1e-9)
    assert follower.heartbeat_age_ms is not None


def test_unknown_worker_is_rejected() -> None:
    with pytest.raises(KeyError):
        GaugeBlock().publisher("nope")


def test_snapshot_row_covers_every_worker() -> None:
    row = snapshot_row(GaugeBlock().sample())
    assert row["type"] == "vitals"
    assert set(row["workers"]) == set(WORKERS)


def _publish_from_child(gauges: GaugeBlock, count: int) -> None:
    publisher = gauges.publisher("follower")
    for _ in range(count):
        publisher.beat(now=time.monotonic(), work_seconds=0.001, events=1)
        publisher.enqueued()


def test_monitor_samples_a_block_written_by_another_process(tmp_path: Path) -> None:
    """The whole point: an external observer sees a worker's stores."""

    ctx = mp.get_context("spawn")
    gauges = GaugeBlock()
    out = tmp_path / "vitals.jsonl"
    monitor = VitalsMonitor(gauges, out, cadence_seconds=0.02, context=ctx)
    try:
        child = ctx.Process(target=_publish_from_child, args=(gauges, 20))
        child.start()
        child.join(timeout=30)
        assert child.exitcode == 0
        time.sleep(0.1)  # let at least one sample land after the child's writes
    finally:
        monitor.close()

    rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert rows, "monitor produced no samples"
    assert all(r["type"] == "vitals" for r in rows)
    # Monotonic counters mean the final sample must reflect all of the child's work.
    final = rows[-1]["workers"]["follower"]
    assert final["iterations"] == 20
    assert final["events"] == 20
    assert final["queue_depth"] == 20
    # Samples are stamped in order on a shared monotonic clock.
    times = [r["monotonic_time"] for r in rows]
    assert times == sorted(times)


def test_monitor_close_is_idempotent(tmp_path: Path) -> None:
    monitor = VitalsMonitor(GaugeBlock(), tmp_path / "v.jsonl", cadence_seconds=0.02)
    monitor.close()
    monitor.close()
    assert not monitor.is_alive


def test_monitor_rejects_nonpositive_cadence(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        VitalsMonitor(GaugeBlock(), tmp_path / "v.jsonl", cadence_seconds=0)
