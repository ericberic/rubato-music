"""Deterministic startup protocol tests: no MIDI, processes, or real sleeps."""

import json
import queue
import threading
from types import SimpleNamespace

import pytest

from aimusic.realtime import follower_process as module


class Channel:
    def __init__(self, context, responses=False):
        self.context = context
        self.responses = responses
        self.closed = False

    def put(self, item):
        if item is None and self.context.cooperative:
            self.context.process.alive = False

    def get(self, timeout):
        self.context.now += 1
        if self.context.messages:
            return self.context.messages.pop(0)
        if self.context.crash:
            self.context.process.alive = False
            self.context.process.exitcode = 7
        raise queue.Empty

    def close(self):
        self.closed = True

    def cancel_join_thread(self):
        pass


class Context:
    def __init__(self, messages=(), *, cooperative=True, crash=False):
        self.messages = list(messages)
        self.now = 0
        self.cooperative = cooperative
        self.crash = crash
        self.channels = []
        self.process = SimpleNamespace(pid=None, alive=False, exitcode=None)
        self.process.start = self.start
        self.process.is_alive = lambda: self.process.alive
        self.process.join = lambda timeout: None
        self.process.terminate = self.terminate
        self.process.kill = self.terminate
        self.terminated = False

    def start(self):
        self.process.pid = 123
        self.process.alive = True

    def terminate(self):
        self.terminated = True
        self.process.alive = False

    def Queue(self):  # noqa: N802 - multiprocessing context API
        channel = Channel(self)
        self.channels.append(channel)
        return channel

    def Process(self, **kwargs):  # noqa: N802 - multiprocessing context API
        return self.process


def milestone(stage):
    return "startup", {"stage": stage, "monotonic_time": 1, "elapsed_seconds": 1}


def create(monkeypatch, context, tmp_path, **kwargs):
    monkeypatch.setattr(module.time, "monotonic", lambda: context.now)
    return module.ProcessFollower(
        module.FollowerSpec("score.mid"),
        context=context,
        diagnostics_dir=str(tmp_path),
        **kwargs,
    )


def test_progress_extends_stage_budget_but_readiness_is_explicit(monkeypatch, tmp_path):
    context = Context([milestone("import"), milestone("score"), ("ready", None)])
    follower = create(monkeypatch, context, tmp_path, start_timeout_seconds=2)
    assert follower.is_alive
    follower.close()
    assert all(channel.closed for channel in context.channels)
    rows = [
        json.loads(line) for line in (tmp_path / "follower-startup.jsonl").read_text().splitlines()
    ]
    assert [row["event"] for row in rows] == ["started", "progress", "progress", "ready"]
    assert rows[-1]["elapsed_seconds"] == 3


def test_timeout_names_stage_and_reaps_uncooperative_child(monkeypatch, tmp_path):
    context = Context([milestone("import_pitch_hmm")], cooperative=False)
    with pytest.raises(TimeoutError, match="during import_pitch_hmm"):
        create(monkeypatch, context, tmp_path, start_timeout_seconds=2)
    assert context.terminated
    assert not context.process.alive
    assert all(channel.closed for channel in context.channels)
    last = json.loads((tmp_path / "follower-startup.jsonl").read_text().splitlines()[-1])
    assert last["error_type"] == "TimeoutError"
    assert last["traceback"]


def test_progress_cannot_extend_absolute_deadline(monkeypatch, tmp_path):
    context = Context([milestone(str(i)) for i in range(20)])
    with pytest.raises(TimeoutError, match="after 3.0s"):
        create(monkeypatch, context, tmp_path, startup_total_timeout_seconds=3)
    assert not context.process.alive


@pytest.mark.parametrize("kind", ["cancel", "crash", "error", "protocol"])
def test_failure_paths_cleanup_and_report(monkeypatch, tmp_path, kind):
    messages = [("error", "child traceback")] if kind == "error" else []
    if kind == "protocol":
        messages = [("unexpected", None)]
    context = Context(messages, crash=kind == "crash")
    cancel = threading.Event()
    if kind == "cancel":
        cancel.set()
    with pytest.raises(RuntimeError):
        create(monkeypatch, context, tmp_path, cancel_event=cancel)
    assert not context.process.alive
    assert all(channel.closed for channel in context.channels)
    assert '"event": "failed"' in (tmp_path / "follower-startup.jsonl").read_text()
