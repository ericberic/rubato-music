"""Tests for hardware-control API contracts without touching real MIDI devices."""

from __future__ import annotations

import json
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import aimusic.server.routes as routes
from aimusic.accompaniment import midi_ports as midi_ports_module
from aimusic.accompaniment.live_midi import RecordedMidiSummary
from aimusic.server.app import create_app
from aimusic.server.live_control import (
    LiveControl,
    LiveJobStatus,
    _score_transport_for_source_window,
)
from tests.fake_hardware import FakeLiveControl, FakeLiveRuntime
from tests.oguri_guard import requires_oguri_derived


@pytest.fixture()
def fake_live(monkeypatch: pytest.MonkeyPatch) -> FakeLiveControl:
    fake = FakeLiveControl()
    monkeypatch.setattr(routes, "live_control", fake)
    return fake


@pytest.fixture()
def fake_runtime(monkeypatch: pytest.MonkeyPatch, fake_live: FakeLiveControl) -> FakeLiveRuntime:
    fake = FakeLiveRuntime(fake_live)
    monkeypatch.setattr(routes, "live_runtime", fake)
    return fake


@pytest.fixture()
def client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_live: FakeLiveControl,
    fake_runtime: FakeLiveRuntime,
):
    _ = fake_runtime
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))
    return TestClient(create_app())


def test_midi_devices_endpoint(client: TestClient):
    response = client.get("/api/midi/devices")
    assert response.status_code == 200
    assert response.json() == {
        "inputs": ["Fake CLP-795GP USB"],
        "outputs": ["Fake Synth"],
        "backend_available": True,
    }


def test_start_and_stop_hardware_record(client: TestClient, fake_live: FakeLiveControl):
    response = client.post(
        "/api/hardware/record/start",
        json={"input_name": "Clavinova", "session_id": "take_a", "duration_seconds": 5},
    )
    assert response.status_code == 200
    assert response.json()["kind"] == "record"
    assert response.json()["running"] is True
    assert fake_live.record_args == {
        "input_name": "Clavinova",
        "session_id": "take_a",
        "duration_seconds": 5.0,
    }

    stop = client.post("/api/hardware/record/stop")
    assert stop.status_code == 200
    assert stop.json()["running"] is False


def test_start_hardware_record_with_cue(client: TestClient, fake_runtime: FakeLiveRuntime):
    response = client.post(
        "/api/hardware/record-with-cue/start",
        json={
            "input_name": "Clavinova",
            "output_name": "Clavinova",
            "session_id": "take_with_cue",
            "volume": 0.75,
            "duration_seconds": 60,
        },
    )
    assert response.status_code == 200
    assert response.json()["kind"] == "live_follow"
    assert response.json()["running"] is True
    assert fake_runtime.start_args is not None
    assert fake_runtime.start_args["bundle_id"] == "chopin_op11_movement_2"
    assert fake_runtime.start_args["start_measure"] == 12
    assert "take_id" not in fake_runtime.start_args
    assert fake_runtime.start_args["duration_seconds"] == 60.0
    config = fake_runtime.start_args["config"]
    assert config.initial_tempo_bpm == 64.0
    assert config.orchestra_volume == 0.75
    assert config.output_advance_ms == 0.0


def test_live_performance_is_captured_ephemerally_until_promoted(
    client: TestClient,
    fake_runtime: FakeLiveRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = client.post(
        "/api/runtime/follow/start",
        json={
            "bundle_id": "chopin_op11_movement_2",
            "revision": None,
            "input_name": "Clavinova",
            "output_name": "Clavinova",
            "start_measure": 1,
            # Compatibility probe: old clients may still send this retired
            # field. Capture is now unconditional, not request-selectable.
            "keep_recording": True,
            "config": {
                "run_id": "live-public",
                "initial_tempo_bpm": 64,
            },
        },
    )

    assert response.status_code == 200
    assert fake_runtime.start_args is not None
    assert "take_id" not in fake_runtime.start_args
    assert fake_runtime.hardware.current.session_id == "live-public"
    assert (routes.paths.session_dir("live-public") / "solo.mid").is_file()
    assert client.get(
        "/api/takes",
        params={"piece_id": "chopin_op11", "movement": 2},
    ).json() == {"takes": []}

    stopped = client.post("/api/hardware/stop")
    assert stopped.status_code == 200
    assert client.get(
        "/api/takes",
        params={"piece_id": "chopin_op11", "movement": 2},
    ).json() == {"takes": []}
    monkeypatch.setattr(routes, "ON_TAKE_CAPTURED_HOOKS", [])
    kept = client.post(
        "/api/performances/live-public/keep",
        params={"piece_id": "chopin_op11", "movement": 2},
    )
    assert kept.status_code == 200
    assert kept.json()["take_id"] == "t-live-public"
    assert (
        len(
            client.get(
                "/api/takes",
                params={"piece_id": "chopin_op11", "movement": 2},
            ).json()["takes"]
        )
        == 1
    )


def test_cued_performance_metadata_moves_with_explicit_promotion(
    client: TestClient,
    fake_runtime: FakeLiveRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording_id = "performance-cued"
    started = client.post(
        "/api/hardware/record-with-cue/start",
        json={
            "input_name": "Clavinova",
            "output_name": "Clavinova",
            "session_id": recording_id,
        },
    )
    assert started.status_code == 200
    assert client.post("/api/hardware/stop").status_code == 200
    monkeypatch.setattr(routes, "ON_TAKE_CAPTURED_HOOKS", [])

    kept = client.post(f"/api/performances/{recording_id}/keep")

    assert kept.status_code == 200
    lifecycle = routes.take_store.get_take_v2(
        "chopin_op11",
        2,
        kept.json()["take_id"],
    )
    assert lifecycle.cue is not None
    assert lifecycle.placement_hint is not None
    assert lifecycle.placement_hint.target_score_tick == 47 * 960


def test_cue_uses_the_fitted_base_tempo_not_a_request_tempo(
    client: TestClient, fake_runtime: FakeLiveRuntime
) -> None:
    response = client.post(
        "/api/hardware/record-with-cue/start",
        json={
            "input_name": "Clavinova",
            "output_name": "Clavinova",
            "session_id": "take_at_fitted_tempo",
            # Kept as a raw compatibility probe: this field is no longer in the
            # schema and must not restore the old UI-slider clock.
            "tempo_bpm": 88,
        },
    )

    assert response.status_code == 200
    assert fake_runtime.start_args is not None
    assert fake_runtime.start_args["config"].initial_tempo_bpm == 64.0


def test_accompanied_record_transport_retains_dense_beat_anchors() -> None:
    from aimusic.accompaniment.rehearsal_position import score_projection

    projection = score_projection("chopin_op11", 2)
    start_seconds = projection.source_seconds_at_score_tick(projection.timeline.tick_at(9))
    transport = _score_transport_for_source_window(
        piece_id="chopin_op11",
        movement=2,
        start_seconds=start_seconds,
        duration_seconds=20.0,
    )

    assert len(transport.anchors) > 2
    assert transport.anchors[0].position.measure_label == "10"
    assert any(
        anchor.position.measure_label == "12" and anchor.position.beat_in_measure == 3.0
        for anchor in transport.anchors
    )


def test_accompanied_transport_and_cue_scale_the_same_wall_clock() -> None:
    from aimusic.accompaniment.rehearsal_position import score_projection

    projection = score_projection("chopin_op11", 2)
    start_seconds = projection.source_seconds_at_score_tick(projection.timeline.tick_at(9))
    normal = _score_transport_for_source_window(
        piece_id="chopin_op11", movement=2, start_seconds=start_seconds, duration_seconds=20
    )
    faster = _score_transport_for_source_window(
        piece_id="chopin_op11",
        movement=2,
        start_seconds=start_seconds,
        duration_seconds=20,
        tempo_scale=1.6,
    )

    assert faster.anchors[-1].elapsed_seconds == pytest.approx(
        normal.anchors[-1].elapsed_seconds / 1.6
    )


@requires_oguri_derived
def test_start_hardware_record_with_cue_from_selected_score_position(
    client: TestClient, fake_runtime: FakeLiveRuntime
) -> None:
    response = client.post(
        "/api/hardware/record-with-cue/start",
        json={
            "input_name": "Clavinova",
            "output_name": "Clavinova",
            "session_id": "take_from_measure_22",
            "target_score_beat": 84.0,
        },
    )

    assert response.status_code == 200
    assert fake_runtime.start_args is not None
    assert fake_runtime.start_args["start_measure"] == 22
    assert "take_id" not in fake_runtime.start_args
    cue = routes._pending_take_cues["take_from_measure_22"]
    assert cue["kind"] == "from_position"
    assert cue["target_beat"] == 84.0
    assert cue["cue_start_beat"] == 84.0
    # The orchestra starts on the selected measure, so there is no separate
    # lead-in window: the performer enters whenever they are ready after it.
    assert float(cue["cue_seconds"]) >= 0.0
    stopped = client.post("/api/takes/take_from_measure_22/stop")
    assert stopped.status_code == 200
    assert stopped.json()["cue"] == {
        "kind": "from_position",
        "target_beat": 84.0,
        "cue_start_beat": 84.0,
        "cue_seconds": pytest.approx(float(cue["cue_seconds"])),
        "output_name": "Clavinova",
    }


@requires_oguri_derived
def test_measure_53_cue_ends_at_the_piano_pickup_not_the_barline(
    client: TestClient, fake_runtime: FakeLiveRuntime
) -> None:
    response = client.post(
        "/api/hardware/record-with-cue/start",
        json={
            "input_name": "Clavinova",
            "output_name": "Clavinova",
            "session_id": "take_measure_53_pickup",
            "target_score_beat": 52 * 4,
        },
    )

    assert response.status_code == 200
    assert response.json()["cue_start_measure"] == 53
    assert response.json()["entry_measure"] == 53
    assert response.json()["entry_score_beat"] == 52 * 4 + 3
    assert fake_runtime.start_args is not None
    assert fake_runtime.start_args["start_measure"] == 53
    cue = routes._pending_take_cues["take_measure_53_pickup"]
    # Passage identity stays on the selected printed bar even though the
    # audible countdown resolves to the late pickup within it.
    assert cue["target_beat"] == 52 * 4 + 3
    assert cue["cue_start_beat"] == 52 * 4


def test_from_here_rejects_a_target_beyond_the_displayed_score(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/hardware/record-with-cue/start",
        json={
            "input_name": "Clavinova",
            "output_name": "Clavinova",
            "target_score_beat": 10_000,
        },
    )

    assert response.status_code == 422
    assert "beyond" in response.json()["detail"]


def test_stopping_a_cued_performance_preserves_metadata_until_the_user_decides(
    client: TestClient, fake_live: FakeLiveControl
):
    """Stop is the scratch-recording boundary, not an implicit discard.

    Cue/localization metadata must survive until the performer explicitly keeps
    the recording; stale scratch metadata is pruned by the existing TTL.
    """

    import aimusic.server.routes as routes_module

    client.post(
        "/api/hardware/record-with-cue/start",
        json={
            "input_name": "Clavinova",
            "output_name": "Clavinova",
            "session_id": "take_cancelled",
        },
    )
    assert "take_cancelled" in routes_module._pending_take_cues

    stop = client.post("/api/hardware/stop")
    assert stop.status_code == 200
    assert "take_cancelled" in routes_module._pending_take_cues


def test_panicking_a_cued_performance_preserves_metadata_until_the_user_decides(
    client: TestClient, fake_live: FakeLiveControl
):
    import aimusic.server.routes as routes_module

    client.post(
        "/api/hardware/record-with-cue/start",
        json={
            "input_name": "Clavinova",
            "output_name": "Clavinova",
            "session_id": "take_panicked",
        },
    )
    assert "take_panicked" in routes_module._pending_take_cues

    panic = client.post("/api/hardware/panic", json={"output_name": "Clavinova"})
    assert panic.status_code == 200
    assert "take_panicked" in routes_module._pending_take_cues


def test_starting_a_new_cued_take_prunes_a_stale_leftover_pending_cue(
    client: TestClient, fake_live: FakeLiveControl
):
    """A day-old cue belongs to a scratch session abandoned without a decision.

    Starting a new performance reclaims that process-local metadata instead of
    letting browser/server crash remnants accumulate forever.
    """

    import aimusic.server.routes as routes_module

    routes_module._pending_take_cues["abandoned_session"] = {
        "kind": "from_top",
        "target_beat": 0.0,
        "cue_seconds": 8.0,
        "output_name": "Clavinova",
        "created_at": 0.0,  # Unix epoch: always stale.
    }

    client.post(
        "/api/hardware/record-with-cue/start",
        json={
            "input_name": "Clavinova",
            "output_name": "Clavinova",
            "session_id": "take_fresh",
        },
    )

    assert "abandoned_session" not in routes_module._pending_take_cues
    fresh_cue = routes_module._pending_take_cues["take_fresh"]
    assert fresh_cue["kind"] == "from_top"
    assert fresh_cue["target_beat"] == 47.0
    assert fresh_cue["cue_start_beat"] == 44.0
    assert fresh_cue["cue_seconds"] > 0.0
    assert fresh_cue["output_name"] == "Clavinova"
    assert "created_at" in fresh_cue


def test_starting_a_new_cued_take_preserves_a_recent_pending_cue(
    client: TestClient, fake_live: FakeLiveControl
):
    """A recent scratch performance may still await the user's Keep decision.

    Starting another performance must not discard the earlier recording's cue
    and localization metadata.
    """

    import aimusic.server.routes as routes_module

    routes_module._pending_take_cues["still_finalizing"] = {
        "kind": "from_top",
        "target_beat": 0.0,
        "cue_seconds": 8.0,
        "output_name": "Clavinova",
        "created_at": datetime.now(timezone.utc).timestamp(),
    }

    client.post(
        "/api/hardware/record-with-cue/start",
        json={
            "input_name": "Clavinova",
            "output_name": "Clavinova",
            "session_id": "take_fresh_2",
        },
    )

    assert "still_finalizing" in routes_module._pending_take_cues
    assert "take_fresh_2" in routes_module._pending_take_cues


def test_play_oguri_uses_volume_default(client: TestClient, fake_live: FakeLiveControl):
    from aimusic.accompaniment.rehearsal_position import score_projection

    response = client.post("/api/hardware/play-oguri", json={"output_name": "Clavinova"})
    assert response.status_code == 200
    assert response.json()["kind"] == "playback"
    tempo_scale = score_projection("chopin_op11", 2).tempo_scale_for_canonical_bpm(120)
    assert fake_live.play_args == {
        "movement": 2,
        "output_name": "Clavinova",
        "volume": 0.75,
        "tempo_scale": tempo_scale,
        "duration_seconds": 75.0,
        "orchestra_only": True,
    }


def test_play_oguri_accepts_an_exact_quarter_note_tempo(
    client: TestClient, fake_live: FakeLiveControl
) -> None:
    from aimusic.accompaniment.rehearsal_position import score_projection

    response = client.post(
        "/api/hardware/play-oguri",
        json={"output_name": "Clavinova", "tempo_bpm": 88},
    )

    assert response.status_code == 200
    assert fake_live.play_args is not None
    projection = score_projection("chopin_op11", 2)
    assert fake_live.play_args["tempo_scale"] == pytest.approx(
        88 / projection.inferred_reference_quarter_bpm()
    )


def test_play_session_midi_variant(client: TestClient, fake_live: FakeLiveControl):
    client.post("/api/sessions", json={"session_id": "take_for_playback"})
    output_dir = routes.paths.run_output_dir("take_for_playback")
    accompaniment_path = output_dir / "accompaniment.mid"
    accompaniment_path.write_bytes(
        b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x00`MTrk\x00\x00\x00\x04\x00\xff/\x00"
    )

    response = client.post(
        "/api/sessions/take_for_playback/midi/accompaniment/play",
        json={"output_name": "Clavinova", "volume": 0.5, "duration_seconds": 10},
    )

    assert response.status_code == 200
    assert response.json()["kind"] == "playback"
    assert response.json()["session_id"] == "take_for_playback"
    assert fake_live.play_file_args == {
        "source_path": accompaniment_path,
        "output_name": "Clavinova",
        "volume": 0.5,
        "duration_seconds": 10.0,
        "session_id": "take_for_playback",
    }


def test_panic_endpoint(client: TestClient, fake_live: FakeLiveControl):
    response = client.post("/api/hardware/panic", json={"output_name": "Clavinova"})
    assert response.status_code == 200
    assert response.json()["message"] == "Panic sent"
    assert fake_live.panic_output == "Clavinova"


def test_midi_devices_endpoint_without_rtmidi(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The endpoint must degrade to empty lists, not 500, when python-rtmidi is missing."""

    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))

    # The out-of-process enumerator signals a missing MIDI backend with
    # ``ok: false``; the endpoint must degrade to empty lists, not 500.
    missing_backend = subprocess.CompletedProcess(
        args=(),
        returncode=0,
        stdout=json.dumps({"ok": False, "inputs": [], "outputs": []}),
    )
    monkeypatch.setattr(midi_ports_module.subprocess, "run", lambda *a, **k: missing_backend)

    # Exercise the real live_control -> list_midi_ports() path, not FakeLiveControl.
    real_client = TestClient(create_app())
    response = real_client.get("/api/midi/devices")
    assert response.status_code == 200
    assert response.json() == {"inputs": [], "outputs": [], "backend_available": False}


def test_panic_clears_running_job_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """panic() must reap a live job so a subsequent start call doesn't see it as running."""

    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))

    class FakeOutputPort:
        def send(self, message) -> None:
            pass

        def __enter__(self) -> "FakeOutputPort":
            return self

        def __exit__(self, *exc_info) -> None:
            return None

    monkeypatch.setattr("mido.open_output", lambda name: FakeOutputPort())

    def fake_record_midi(input_name, output_path, *, duration_seconds=None, stop_event=None):
        output_path.write_bytes(b"")
        return RecordedMidiSummary(path=output_path, message_count=0, duration_seconds=0.0)

    monkeypatch.setattr("aimusic.server.live_control.record_midi", fake_record_midi)

    control = LiveControl()
    stop_event = threading.Event()
    thread = threading.Thread(target=stop_event.wait, daemon=True)
    control._thread = thread
    control._stop_event = stop_event
    control._status = LiveJobStatus(kind="playback", running=True, message="Playing")
    thread.start()

    status = control.panic("Clavinova")

    assert status.kind == "idle"
    assert status.running is False
    assert control._thread is None
    assert control._stop_event is None

    started = control.start_record(
        input_name="Clavinova", session_id="after_panic", duration_seconds=None
    )
    assert started.kind == "record"
    assert started.running is True

    with control._lock:
        followup_thread = control._thread
    if followup_thread is not None:
        followup_thread.join(timeout=2.0)


def test_panic_reports_error_when_output_open_fails(monkeypatch: pytest.MonkeyPatch):
    """panic() must not swallow a failed MIDI open, e.g. a disconnected device."""

    def _raise(name: str) -> None:
        raise OSError(f"unknown port {name!r}")

    monkeypatch.setattr("mido.open_output", _raise)

    control = LiveControl()
    status = control.panic("Clavinova")

    assert status.kind == "error"
    assert status.running is False
    assert "unknown port" in status.message


def test_silencing_a_cued_take_keeps_the_recording(tmp_path, monkeypatch) -> None:
    """Silence must not discard a performance that is already on disk.

    Regression for a lost 58-second take: Silence/panic/stop only dropped the
    pending cue, so a fully captured recording was never registered, never
    aligned, and never fed the model -- with no error shown.
    """

    from aimusic.server import routes

    take_id = "t20260729T031627Z-test"
    session_dir = tmp_path / take_id
    session_dir.mkdir(parents=True)
    _write_midi(session_dir / "solo.mid", (60, 64, 67))

    monkeypatch.setattr(routes.paths, "session_dir", lambda _sid: session_dir)
    calls: list[dict] = []
    monkeypatch.setattr(
        routes.take_store,
        "finalize_take",
        lambda *a, **k: (calls.append(k) or (_FakeSavedTake(a[2]), True)),
    )
    aligned: list[object] = []
    monkeypatch.setattr(routes, "on_take_captured", lambda *a, **k: aligned.append(a))

    routes._keep_recording_from_aborted_job(take_id)

    assert calls, "an aborted take with real notes must still be kept"
    assert calls[0]["aborted"] is True, "it must be tagged as aborted"
    assert aligned, "the kept take must still be sent for alignment"


def test_scratch_performance_identity_survives_process_memory_loss(
    tmp_path, monkeypatch
) -> None:
    """A restart may not turn scratch MIDI into an implicit durable take."""

    from aimusic.server import routes

    recording_id = "performance-restart-test"
    session_dir = tmp_path / recording_id
    monkeypatch.setattr(routes.paths, "session_dir", lambda _sid: session_dir)
    routes._register_ephemeral_performance(recording_id)
    _write_midi(session_dir / "solo.mid", (60, 64, 67))

    # Simulate a new server process: only the on-disk identity remains.
    with routes._ephemeral_performance_ids_lock:
        routes._ephemeral_performance_ids.discard(recording_id)
    calls: list[object] = []
    monkeypatch.setattr(
        routes.take_store,
        "finalize_take",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    routes._keep_recording_from_aborted_job(recording_id)

    assert not calls
    assert (
        session_dir / routes._EPHEMERAL_PERFORMANCE_MARKER
    ).is_file(), "scratch ownership must be durable beside the recording"


def test_an_already_finalized_take_is_not_finalized_twice(tmp_path, monkeypatch) -> None:
    """A normal stop racing an abort must not double-register or re-align.

    finalize_take reports whether *this* call performed the work; the loser must
    publish nothing and enqueue no second alignment job.
    """

    from aimusic.server import routes

    take_id = "t-race"
    session_dir = tmp_path / take_id
    session_dir.mkdir(parents=True)
    _write_midi(session_dir / "solo.mid", (60,))

    monkeypatch.setattr(routes.paths, "session_dir", lambda _sid: session_dir)
    monkeypatch.setattr(
        routes.take_store,
        "finalize_take",
        lambda *a, **k: (_FakeSavedTake(a[2]), False),  # someone else won
    )
    aligned: list[object] = []
    monkeypatch.setattr(routes, "on_take_captured", lambda *a, **k: aligned.append(a))
    published: list[object] = []
    monkeypatch.setattr(routes.events, "publish", published.append)

    routes._keep_recording_from_aborted_job(take_id)

    assert not aligned, "a losing finalizer must not enqueue a second alignment"
    assert not published, "a losing finalizer must not publish a duplicate event"


def test_a_failed_finalization_is_reported_not_swallowed(tmp_path, monkeypatch) -> None:
    """Reporting success while the take failed to file is the original bug."""

    import pytest as _pytest
    from fastapi import HTTPException

    from aimusic.server import routes

    take_id = "t-fails"
    session_dir = tmp_path / take_id
    session_dir.mkdir(parents=True)
    _write_midi(session_dir / "solo.mid", (60,))

    monkeypatch.setattr(routes.paths, "session_dir", lambda _sid: session_dir)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(routes.take_store, "finalize_take", boom)

    with _pytest.raises(HTTPException) as excinfo:
        routes._keep_recording_from_aborted_job(take_id)
    assert excinfo.value.status_code == 500
    assert "still on disk" in str(excinfo.value.detail)


def test_aborting_before_any_note_keeps_nothing(tmp_path, monkeypatch) -> None:
    """A lead-in cancelled before playing has nothing worth keeping."""

    from aimusic.server import routes

    session_dir = tmp_path / "empty-take"
    session_dir.mkdir(parents=True)
    _write_midi(session_dir / "solo.mid", ())

    monkeypatch.setattr(routes.paths, "session_dir", lambda _sid: session_dir)
    calls: list[object] = []
    monkeypatch.setattr(routes.take_store, "finalize_take", lambda *a, **k: calls.append(k))

    routes._keep_recording_from_aborted_job("empty-take")

    assert not calls, "a take with no notes must not be registered"


def _write_midi(path, pitches) -> None:
    import mido

    midi = mido.MidiFile()
    track = mido.MidiTrack()
    midi.tracks.append(track)
    for pitch in pitches:
        track.append(mido.Message("note_on", note=pitch, velocity=80, time=240))
        track.append(mido.Message("note_off", note=pitch, velocity=0, time=240))
    midi.save(path)


class _FakeSavedTake:
    def __init__(self, take_id) -> None:
        self.take_id = take_id
        self.piece_id = "chopin_op11"
        self.movement = 2
        self.duration_seconds = 1.0


def test_recording_cursor_advances_across_the_orchestra_playback() -> None:
    """The cursor must actually move while the orchestra plays.

    Regression: it was bounded to the lead-in to stop it sweeping ahead of the
    performer. Once the cue started *on* the selected measure, lead_in_seconds
    became 0, so the cursor sat frozen at that barline for the whole take. It now
    tracks the orchestra for as long as the orchestra plays -- which is what the
    performer needs while deciding when to come in.
    """

    from aimusic.server.live_control import _score_transport_for_source_window

    start = 248.9

    def span(transport) -> float:
        beats = [a.position.score_beat for a in transport.anchors]
        return (max(beats) - min(beats)) if beats else 0.0

    frozen = _score_transport_for_source_window(
        piece_id="chopin_op11", movement=2, start_seconds=start, duration_seconds=0.0
    )
    playing = _score_transport_for_source_window(
        piece_id="chopin_op11", movement=2, start_seconds=start, duration_seconds=40.0
    )

    assert span(frozen) == 0.0, "a zero-length window is exactly the frozen cursor"
    assert span(playing) > 4.0, "the cursor must advance while the orchestra plays"
