import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from aimusic.realtime.follower_preparation import FollowerPreparation
from aimusic.realtime.follower_process import FollowerSpec

SPEC = FollowerSpec(score_file="score.mid", tempo_bpm=68)


class Child:
    is_alive = True
    startup_timings_ms = {"imports": 1000, "score_and_model": 2000}
    configured = None

    def configure_entry(self, beat, minimum):
        assert self.configured is None
        self.configured = (beat, minimum)

    def close(self):
        self.is_alive = False


class Builder:
    def __init__(self):
        self.started = threading.Event()
        self.finish = threading.Event()
        self.children = []

    def __call__(self, spec, cancel_event):
        self.started.set()
        assert self.finish.wait(3)
        child = Child()
        self.children.append(child)
        return child


def test_cold_claim_waits_for_single_preparation_and_applies_entry():
    builder = Builder()
    ready = threading.Event()
    pool = FollowerPreparation(builder, lambda s: ready.set() if s.state == "ready" else None)
    try:
        with ThreadPoolExecutor() as executor:
            future = executor.submit(
                pool.claim,
                replace(SPEC, initial_reference_beat=12, minimum_lock_updates=2),
                "revision-1",
                threading.Event(),
            )
            assert builder.started.wait(3)
            assert pool.status().state == "preparing"
            assert not future.done()
            pool.prepare(SPEC, "revision-1")  # browser reconnect: no duplicate
            builder.finish.set()
            child = future.result(3)
            assert len(builder.children) == 1
            assert child.configured == (12, 2)
            assert pool.status().state == "in_use"
            child.close()
    finally:
        builder.finish.set()
        pool.close()


def test_ready_claim_has_no_construction_cost_and_next_take_is_fresh(monkeypatch):
    # Logical clock advances only when expensive construction occurs. This
    # timing assertion is deterministic, independent of CI machine speed.
    now = [0.0]
    monkeypatch.setattr("aimusic.realtime.follower_preparation.time.monotonic", lambda: now[0])
    ready = threading.Event()
    children = []

    def build(spec, cancel_event):
        now[0] += 36.0
        child = Child()
        children.append(child)
        return child

    pool = FollowerPreparation(build, lambda s: ready.set() if s.state == "ready" else None)
    try:
        pool.prepare(SPEC, "v1")
        assert ready.wait(3)
        assert pool.status().elapsed_seconds == 36.0
        before = now[0]
        child = pool.claim(SPEC, "v1", threading.Event())
        assert now[0] - before == 0
        assert len(children) == 1
        child.close()
        ready.clear()
        pool.release()
        assert ready.wait(3)
        second = pool.claim(SPEC, "v1", threading.Event())
        assert second is not child
        assert len(children) == 2
        second.close()
    finally:
        pool.close()


def test_cancel_cold_start_does_not_wait_for_constructor():
    builder = Builder()
    pool = FollowerPreparation(builder)
    stop = threading.Event()
    try:
        with ThreadPoolExecutor() as executor:
            future = executor.submit(pool.claim, SPEC, "v1", stop)
            assert builder.started.wait(3)
            stop.set()
            with pytest.raises(InterruptedError):
                future.result(3)
            assert not builder.finish.is_set()
    finally:
        builder.finish.set()
        pool.close()
    assert all(not child.is_alive for child in builder.children)


def test_failed_preparation_requires_explicit_retry():
    failed, ready = threading.Event(), threading.Event()
    calls = []

    def build(spec, cancel_event):
        calls.append(spec)
        if len(calls) == 1:
            raise ValueError("broken reference")
        return Child()

    def observe(status):
        if status.state == "failed":
            failed.set()
        if status.state == "ready":
            ready.set()

    pool = FollowerPreparation(build, observe)
    try:
        pool.prepare(SPEC, "v1")
        assert failed.wait(3)
        pool.prepare(SPEC, "v1")
        assert len(calls) == 1
        with pytest.raises(RuntimeError, match="broken reference"):
            pool.claim(SPEC, "v1", threading.Event())
        pool.prepare(SPEC, "v1", force=True)
        assert ready.wait(3)
        assert len(calls) == 2
    finally:
        pool.close()


@pytest.mark.parametrize("change", ["revision", "tempo"])
def test_stale_preparation_cannot_replace_new_selection(change):
    first_started, finish_first, ready = (threading.Event() for _ in range(3))
    stale_closed = threading.Event()

    class Stale(Child):
        def close(self):
            super().close()
            stale_closed.set()

    calls = []

    def build(spec, cancel_event):
        calls.append(spec)
        if len(calls) == 1:
            first_started.set()
            assert finish_first.wait(3)
            return Stale()
        return Child()

    pool = FollowerPreparation(build, lambda s: ready.set() if s.state == "ready" else None)
    try:
        pool.prepare(SPEC, "v1")
        assert first_started.wait(3)
        selected = replace(SPEC, tempo_bpm=80) if change == "tempo" else SPEC
        identity = "v2" if change == "revision" else "v1"
        pool.prepare(selected, identity)
        assert ready.wait(3)
        finish_first.set()
        assert stale_closed.wait(3)
        child = pool.claim(selected, identity, threading.Event())
        assert child.is_alive
        child.close()
    finally:
        finish_first.set()
        pool.close()


def test_shutdown_cancels_builder_and_rejects_new_work():
    started = threading.Event()
    exited = threading.Event()

    def build(spec, cancel_event):
        started.set()
        assert cancel_event.wait(3)
        exited.set()
        raise InterruptedError("cancelled")

    pool = FollowerPreparation(build)
    pool.prepare(SPEC, "v1")
    assert started.wait(3)
    pool.close()
    assert exited.is_set()
    assert pool.status().state == "not_loaded"
    with pytest.raises(RuntimeError, match="shut down"):
        pool.prepare(SPEC, "v1")


def test_dead_prepared_child_is_not_reported_ready():
    ready = threading.Event()
    child = Child()
    pool = FollowerPreparation(
        lambda *a, **k: child, lambda s: ready.set() if s.state == "ready" else None
    )
    try:
        pool.prepare(SPEC, "v1")
        assert ready.wait(3)
        child.is_alive = False
        assert pool.status().state == "failed"
        with pytest.raises(RuntimeError, match="exited"):
            pool.claim(SPEC, "v1", threading.Event())
    finally:
        pool.close()
