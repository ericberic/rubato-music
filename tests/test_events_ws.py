"""`WS /api/events` contract tests (design doc §2.7, roadmap item 5).

Covers the in-process broadcaster in isolation, plus the take-record start/
stop routes actually publishing onto a connected socket via FastAPI's
`TestClient` WebSocket support. `take:alignment_done`'s emission from the
alignment pipeline itself is covered separately in
tests/takes/test_aligner_events.py, since that doesn't need a live socket.
"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mido import Message, MidiFile, MidiTrack

import aimusic.server.routes as routes
from aimusic.core import paths
from aimusic.core.event_journal import append_exception, event_journal_path, journal_exception
from aimusic.core.events import EventBroadcaster
from aimusic.core.events import events as shared_events
from aimusic.server.app import create_app
from aimusic.server.live_control import LiveJobStatus
from aimusic.server.schemas import TakeRecordingStarted


@pytest.fixture(autouse=True)
def _reset_shared_event_broadcaster():
    """The app wires up the process-wide `aimusic.core.events.events`
    singleton, not a fresh instance per app (design doc §2.7: one in-process
    broadcaster). Reset it after every test so a leftover subscriber or loop
    binding from one test can't affect another.
    """

    yield
    with shared_events._lock:
        shared_events._queues.clear()
        shared_events._loop = None


def test_broadcaster_delivers_published_event_to_subscribed_queue() -> None:
    broadcaster = EventBroadcaster()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        broadcaster.bind_loop(loop)
        queue = broadcaster.subscribe()

        broadcaster.publish(
            TakeRecordingStarted(
                type="take:recording_started", take_id="t1", piece_id="chopin_op11", movement=2
            )
        )

        received = asyncio.run_coroutine_threadsafe(queue.get(), loop).result(timeout=2)
        assert json.loads(received) == {
            "type": "take:recording_started",
            "take_id": "t1",
            "piece_id": "chopin_op11",
            "movement": 2,
        }
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_broadcaster_publish_without_bound_loop_is_still_journaled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    broadcaster = EventBroadcaster()
    broadcaster.publish(
        TakeRecordingStarted(
            type="take:recording_started", take_id="t1", piece_id="chopin_op11", movement=2
        )
    )

    records = [json.loads(line) for line in event_journal_path().read_text().splitlines()]
    assert records[0]["event_type"] == "take:recording_started"
    assert records[0]["take_id"] == "t1"
    assert records[0]["piece_id"] == "chopin_op11"
    assert records[0]["movement"] == 2
    assert records[0]["event_id"]
    assert records[0]["recorded_at"].endswith("+00:00")


def test_exception_journal_captures_traceback_and_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    try:
        raise RuntimeError("MIDI port disappeared")
    except RuntimeError as exc:
        append_exception("hardware:record_failed", exc, session_id="take-1")

    record = json.loads(event_journal_path().read_text().splitlines()[0])
    assert record["event_type"] == "hardware:record_failed"
    assert record["session_id"] == "take-1"
    assert record["error"]["type"] == "RuntimeError"
    assert record["error"]["message"] == "MIDI port disappeared"
    assert "raise RuntimeError" in record["error"]["traceback"]


def test_journal_failure_never_escapes_worker_error_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "aimusic.core.event_journal.append_exception", lambda *args, **kwargs: 1 / 0
    )

    journal_exception("hardware:record_failed", RuntimeError("original failure"))


def test_broadcaster_unsubscribe_stops_delivery() -> None:
    broadcaster = EventBroadcaster()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        broadcaster.bind_loop(loop)
        queue = broadcaster.subscribe()
        broadcaster.unsubscribe(queue)

        broadcaster.publish(
            TakeRecordingStarted(
                type="take:recording_started", take_id="t1", piece_id="chopin_op11", movement=2
            )
        )

        assert queue.empty()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


class FakeLiveControl:
    """Minimal stand-in for `LiveControl`, matching the pattern in
    tests/test_takes_api.py's `FakeLiveControl` (a real recording thread
    finishing synchronously)."""

    def __init__(self) -> None:
        self.current = LiveJobStatus(kind="idle", running=False, message="Idle")

    def start_record(self, *, input_name: str, session_id: str, duration_seconds):
        _write_fixture_midi(paths.session_dir(session_id) / "solo.mid")
        self.current = LiveJobStatus(
            kind="record",
            running=True,
            message=f"Recording from {input_name}",
            session_id=session_id,
        )
        return self.current

    def stop(self) -> LiveJobStatus:
        self.current = LiveJobStatus(
            kind="idle", running=False, message="Idle", session_id=self.current.session_id
        )
        return self.current

    def wait_until_idle(self, timeout: float = 10.0) -> LiveJobStatus:
        return self.current


class FakeAlignmentWorker:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, int, str]] = []

    def enqueue(self, piece_id: str, movement: int, take_id: str) -> None:
        self.enqueued.append((piece_id, movement, take_id))


def _write_fixture_midi(path: Path) -> None:
    midi = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    midi.tracks.append(track)
    track.append(Message("note_on", note=60, velocity=64, time=0))
    track.append(Message("note_off", note=60, velocity=0, time=240))
    path.parent.mkdir(parents=True, exist_ok=True)
    midi.save(path)


@pytest.fixture()
def fake_live(monkeypatch: pytest.MonkeyPatch) -> FakeLiveControl:
    fake = FakeLiveControl()
    monkeypatch.setattr(routes, "live_control", fake)
    return fake


@pytest.fixture()
def fake_alignment_worker(monkeypatch: pytest.MonkeyPatch) -> FakeAlignmentWorker:
    fake = FakeAlignmentWorker()
    monkeypatch.setattr(routes, "alignment_worker", fake)
    return fake


@pytest.fixture()
def client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_live: FakeLiveControl,
    fake_alignment_worker: FakeAlignmentWorker,
):
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))
    # Used as a context manager (unlike the plain `TestClient(create_app())`
    # in tests/test_takes_api.py) so the app's lifespan actually runs and
    # binds the event broadcaster's loop -- without it, `TestClient` gives
    # every request its own throwaway event loop and skips lifespan
    # entirely, so `events.publish` would silently have nothing bound to
    # call `call_soon_threadsafe` on and every websocket-based test here
    # would hang waiting for a message that never arrives.
    with TestClient(create_app()) as test_client:
        yield test_client


def test_take_record_start_and_stop_publish_events_over_the_socket(
    client: TestClient, fake_live: FakeLiveControl
) -> None:
    with client.websocket_connect("/api/events") as websocket:
        start_response = client.post(
            "/api/takes/record",
            json={"input_name": "CLP-795GP USB", "piece_id": "chopin_op11", "movement": 2},
        )
        take_id = start_response.json()["take_id"]

        started_event = websocket.receive_json()
        assert started_event == {
            "type": "take:recording_started",
            "take_id": take_id,
            "piece_id": "chopin_op11",
            "movement": 2,
        }

        client.post(f"/api/takes/{take_id}/stop")

        stopped_event = websocket.receive_json()
        assert stopped_event["type"] == "take:recording_stopped"
        assert stopped_event["take_id"] == take_id
        assert stopped_event["piece_id"] == "chopin_op11"
        assert stopped_event["movement"] == 2
        assert stopped_event["duration_seconds"] > 0


def test_multiple_subscribers_all_receive_the_same_event(
    client: TestClient, fake_live: FakeLiveControl
) -> None:
    with (
        client.websocket_connect("/api/events") as first,
        client.websocket_connect("/api/events") as second,
    ):
        client.post(
            "/api/takes/record",
            json={"input_name": "CLP-795GP USB", "piece_id": "chopin_op11", "movement": 2},
        )

        assert first.receive_json()["type"] == "take:recording_started"
        assert second.receive_json()["type"] == "take:recording_started"


def test_disconnected_subscriber_does_not_block_or_error_other_publishes(
    client: TestClient, fake_live: FakeLiveControl
) -> None:
    with client.websocket_connect("/api/events") as websocket:
        pass  # connect then immediately disconnect

    # A publish after a subscriber has disconnected must not raise, and a
    # freshly connected subscriber must still get events normally.
    with client.websocket_connect("/api/events") as websocket:
        client.post(
            "/api/takes/record",
            json={"input_name": "CLP-795GP USB", "piece_id": "chopin_op11", "movement": 2},
        )
        assert websocket.receive_json()["type"] == "take:recording_started"


def test_socket_waits_on_event_and_disconnect_without_timeout_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeWebSocket:
        def __init__(self) -> None:
            self.accepted = False
            self.receive_calls = 0
            self.sent: list[str] = []
            self._never_disconnects = asyncio.Event()

        async def accept(self) -> None:
            self.accepted = True

        async def receive(self) -> dict[str, str]:
            self.receive_calls += 1
            await self._never_disconnects.wait()
            return {"type": "websocket.disconnect"}

        async def send_text(self, message: str) -> None:
            self.sent.append(message)

    class FakeEvents:
        def __init__(self) -> None:
            self.queue: asyncio.Queue[str | None] = asyncio.Queue()
            self.unsubscribed = False

        def subscribe(self) -> asyncio.Queue[str | None]:
            self.queue.put_nowait("first")
            self.queue.put_nowait(None)
            return self.queue

        def unsubscribe(self, queue: asyncio.Queue[str | None]) -> None:
            assert queue is self.queue
            self.unsubscribed = True

    websocket = FakeWebSocket()
    fake_events = FakeEvents()
    monkeypatch.setattr(routes, "events", fake_events)

    # Playwright's sync harness can leave an event loop active on the pytest
    # thread when the whole suite runs. Exercise this async route on its own
    # loop/thread so the test is isolated from unrelated browser machinery.
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(
            asyncio.run,
            routes.take_events_ws(websocket),  # type: ignore[arg-type]
        ).result(timeout=2)

    assert websocket.accepted is True
    assert websocket.sent == ["first"]
    assert websocket.receive_calls == 1
    assert fake_events.unsubscribed is True
