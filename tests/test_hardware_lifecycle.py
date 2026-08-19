"""Hardware lifecycle and websocket event contract tests."""

from __future__ import annotations

import threading

import pytest
from pydantic import TypeAdapter

from aimusic.accompaniment.live_midi import MidiPlaybackSummary
from aimusic.accompaniment.oguri import oguri_movement
from aimusic.server.live_control import LiveControl, LiveJobStatus
from aimusic.server.schemas import HardwareJobPhase, HardwareStatusEvent, TakeEvent


def test_managed_hardware_job_publishes_each_real_lifecycle_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[HardwareStatusEvent] = []
    monkeypatch.setattr("aimusic.server.live_control.events.publish", published.append)
    entered = threading.Event()

    def managed(stop_event: threading.Event) -> str:
        entered.set()
        assert stop_event.wait(timeout=2.0)
        return "Stopped cleanly"

    control = LiveControl()
    started = control.start_managed(
        kind="live_follow",
        message="Following performer",
        target=managed,
        session_id="session-1",
    )
    assert entered.wait(timeout=2.0)

    stopping = control.stop()
    completed = control.wait_until_idle(timeout=2.0)

    assert started.phase is HardwareJobPhase.RUNNING
    assert stopping.phase is HardwareJobPhase.STOPPING
    assert completed.phase is HardwareJobPhase.COMPLETED
    assert [event.status.phase for event in published] == [
        HardwareJobPhase.RUNNING,
        HardwareJobPhase.STOPPING,
        HardwareJobPhase.COMPLETED,
    ]
    assert published[0].status.kind == "live_follow"
    assert published[0].status.running is True
    assert published[-1].status.kind == "idle"
    assert published[-1].status.running is False
    assert published[-1].status.session_id == "session-1"


def test_hardware_transition_rejects_an_illegal_phase_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[HardwareStatusEvent] = []
    monkeypatch.setattr("aimusic.server.live_control.events.publish", published.append)
    control = LiveControl()

    with control._lock, pytest.raises(
        RuntimeError, match="Illegal hardware job transition: idle -> stopping"
    ):
        control._transition_locked(
            phase=HardwareJobPhase.STOPPING,
            kind="stopping",
            running=True,
            message="Stopping",
        )

    assert control.status().phase is HardwareJobPhase.IDLE
    assert published == []


def test_repeating_identical_hardware_status_does_not_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[HardwareStatusEvent] = []
    monkeypatch.setattr("aimusic.server.live_control.events.publish", published.append)
    control = LiveControl()

    with control._lock:
        status = control._transition_locked(
            phase=HardwareJobPhase.IDLE,
            kind="idle",
            running=False,
            message="Idle",
        )

    assert status.phase is HardwareJobPhase.IDLE
    assert published == []


def test_hardware_status_event_is_a_take_event_union_member() -> None:
    event = TypeAdapter(TakeEvent).validate_python(
        {
            "type": "hardware:status",
            "status": {
                "phase": "running",
                "kind": "record",
                "running": True,
                "message": "Recording",
                "session_id": "take-1",
            },
        }
    )

    assert isinstance(event, HardwareStatusEvent)
    assert event.status.phase is HardwareJobPhase.RUNNING
    assert event.status.kind == "record"
    assert event.status.running is True


def test_orchestra_from_top_starts_sound_and_cursor_on_measure_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    played = threading.Event()

    def fake_play_midi(source_path, output_name, **kwargs):
        captured.update(kwargs)
        played.set()
        return MidiPlaybackSummary(
            source_path=source_path,
            event_count=1,
            duration_seconds=0.0,
            volume=float(kwargs["volume"]),
            output_name=output_name,
        )

    monkeypatch.setattr("aimusic.server.live_control.play_midi", fake_play_midi)
    control = LiveControl()
    started = control.start_play_oguri(
        movement=2,
        output_name="Clavinova",
        duration_seconds=75.0,
    )

    assert played.wait(timeout=2.0)
    movement = oguri_movement(2)
    assert captured["start_seconds"] == movement.first_orchestra_entry_seconds
    assert started.score_transport is not None
    first = started.score_transport.anchors[0]
    assert first.elapsed_seconds == 0.0
    assert first.position.measure_label == "1"
    assert first.position.beat_in_measure == 0.0
    control.wait_until_idle(timeout=2.0)


def test_panic_timeout_remains_nonterminal_until_worker_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeOutputPort:
        def send(self, _message: object) -> None:
            pass

        def __enter__(self) -> "FakeOutputPort":
            return self

        def __exit__(self, *_exc_info: object) -> None:
            return None

    class StuckThread:
        def is_alive(self) -> bool:
            return True

        def join(self, timeout: float | None = None) -> None:
            assert timeout == 5.0

    monkeypatch.setattr("mido.open_output", lambda _name: FakeOutputPort())
    published: list[HardwareStatusEvent] = []
    monkeypatch.setattr("aimusic.server.live_control.events.publish", published.append)
    control = LiveControl()
    control._thread = StuckThread()  # type: ignore[assignment]
    control._stop_event = threading.Event()
    control._status = LiveJobStatus(
        phase=HardwareJobPhase.RUNNING,
        kind="playback",
        running=True,
        message="Playing",
    )

    status = control.panic("Clavinova")

    assert status.phase is HardwareJobPhase.STOPPING
    assert status.running is True
    assert "failed to stop" in status.message
    assert published[-1].status.phase is HardwareJobPhase.STOPPING
