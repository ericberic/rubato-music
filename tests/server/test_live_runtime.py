from __future__ import annotations

import json
import shutil
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import mido
import pytest
import yaml

from aimusic.accompaniment.anchors import AnchorFirer
from aimusic.accompaniment.bundle_v2 import BundleRegistry
from aimusic.accompaniment.following import FollowerUpdate, PerformedNote
from aimusic.accompaniment.predictive_follow import InterpretationArrivalCurve
from aimusic.accompaniment.runtime_contracts import (
    OrchestraRendererStatus,
    RunPhase,
    RuntimeConfig,
    RuntimeStatus,
    TelemetryLevel,
)
from aimusic.accompaniment.runtime_io import CapturingOutput
from aimusic.accompaniment.runtime_projection import (
    MOVEMENT_2_BUNDLE_ID,
    _movement_2_sections,
    default_bundle_registry,
    project_bundle_v2_to_provisional_runtime,
)
from aimusic.accompaniment.scheduler import AccompanimentScheduler
from aimusic.accompaniment.score_bundle import ScoreBundle
from aimusic.accompaniment.section_policy import (
    AccompanimentMode,
    Section,
    SectionMap,
)
from aimusic.accompaniment.tempo_model import TempoState
from aimusic.audio.live_config import (
    LiveAudioConfig,
    LiveVstZoneConfig,
    VstInstrumentBinding,
)
from aimusic.audio.live_vst import LiveVstError
from aimusic.mixing import store as mix_store
from aimusic.mixing.models import (
    EnvelopePoint,
    Gesture,
    MixProgramCreate,
    MixRegion,
    MixRoute,
)
from aimusic.realtime.harness import (
    InjectedNote,
    MixProbeRouter,
    VirtualInputPort,
    virtual_mix_zones,
)
from aimusic.server.live_control import LiveControl
from aimusic.server.live_runtime import (
    _RESIDENT_HEALTH_GRACE_SECONDS,
    LiveRuntimeManager,
    _anchor_ticks,
    _bounded_runtime_score_tick,
    _CanonicalFollower,
    _interpretation_arrival_curve,
    _LatestTempoCommand,
    _runtime_entry_point,
    _runtime_score_position,
    _RuntimeEntryPoint,
    _slice_replay_notes,
    _start_live_vst_router,
)
from aimusic.takes.models import Interpretation, PerformanceProfileCell

V2_FIXTURE = Path("tests/fixtures/score_bundles/synthetic_v2")


from tests.oguri_guard import requires_oguri_derived

@pytest.fixture(autouse=True)
def isolate_runtime_recordings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Always-on capture must never write test performances into Eric's data."""

    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))


class FakePort:
    def __init__(self) -> None:
        self.messages: list[mido.Message] = []

    def send(self, message: mido.Message) -> None:
        self.messages.append(message.copy())

    def close(self) -> None:
        pass


class FakeInput:
    def __init__(self) -> None:
        self._sent = False

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        pass

    def iter_pending(self):
        if self._sent:
            return []
        self._sent = True
        return [mido.Message("note_on", note=60, velocity=80)]


class FakeFollower:
    observed_perf_times: list[float] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def observe(self, note):
        self.observed_perf_times.append(note.perf_time)
        return FollowerUpdate(
            perf_time=note.perf_time,
            score_beat=0.0,
            confidence=1.0,
            raw_state={"follower": "fake"},
        )

    def close(self) -> None:
        pass


def wait_for_phase(manager: LiveRuntimeManager, phase: RunPhase, timeout: float = 1) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = manager.status()
        if status is not None and status.phase is phase:
            return
        time.sleep(0.002)
    raise AssertionError(f"runtime did not reach {phase}")


def test_matchmaker_warm_start_requires_reference_coordinate() -> None:
    entry = _RuntimeEntryPoint(
        measure=12,
        score_beat=44.0,
        reference_beat=None,
        section_mode=AccompanimentMode.FOLLOW,
    )

    with pytest.raises(
        ValueError,
        match="requires a mapped reference-performance beat",
    ):
        _ = entry.follower_prior_reference_beat


@requires_oguri_derived
def test_movement_2_reactive_cues_survive_an_empty_local_anchor_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection = project_bundle_v2_to_provisional_runtime(
        bundle_id=MOVEMENT_2_BUNDLE_ID,
        revision=None,
        registry=default_bundle_registry(),
    )
    monkeypatch.setattr(
        "aimusic.server.live_runtime.take_store.load_anchor_set",
        lambda *_args: SimpleNamespace(anchors=()),
    )

    assert _anchor_ticks(projection) == (
        165_120,
        166_080,
        167_040,
        168_000,
        168_960,
        353_280,
        354_240,
        355_200,
        356_160,
        357_120,
    )

    firer = AnchorFirer(_anchor_ticks(projection), projection.bundle)
    assert firer.armed_cues == (
        (172.0, 40),
        (173.0, 39),
        (174.0, 32),
        (175.0, 31),
        (176.0, 30),
        (368.0, 37),
        (369.0, 36),
        (370.0, 29),
        (371.0, 28),
        (372.0, 27),
    )


def make_midi_v2_bundle(tmp_path: Path, *, solo_start_ticks: int = 0) -> Path:
    root = tmp_path / "bundle"
    shutil.copytree(V2_FIXTURE, root)
    manifest_path = root / "bundle.yaml"
    payload = yaml.safe_load(manifest_path.read_text())
    for artifact in payload["derived"]:
        if artifact["role"] == "follower_reference":
            artifact["path"] = "derived/solo_reference.mid"
        elif artifact["role"] == "accompaniment":
            artifact["path"] = "derived/accompaniment.mid"
    manifest_path.write_text(yaml.safe_dump(payload, sort_keys=False))
    _write_midi(
        root / "derived/solo_reference.mid",
        pitches=[60, 62],
        program=0,
        start_ticks=solo_start_ticks,
    )
    _write_midi(root / "derived/accompaniment.mid", pitches=[48, 55], program=48)
    return root


def _write_midi(
    path: Path,
    *,
    pitches: list[int],
    program: int,
    start_ticks: int = 0,
) -> None:
    midi = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    midi.tracks.append(track)
    track.append(mido.MetaMessage("track_name", name="Test part", time=0))
    track.append(mido.Message("program_change", channel=0, program=program, time=0))
    track.append(mido.Message("control_change", channel=0, control=7, value=91, time=0))
    for index, pitch in enumerate(pitches):
        track.append(
            mido.Message(
                "note_on",
                channel=0,
                note=pitch,
                velocity=70,
                time=start_ticks if index == 0 else 240,
            )
        )
        track.append(mido.Message("note_off", channel=0, note=pitch, velocity=0, time=240))
    midi.save(path)


def test_deterministic_no_sound_replay_completes(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))
    control = LiveControl()
    manager = LiveRuntimeManager(hardware_control=control)
    root = make_midi_v2_bundle(tmp_path)

    manager.start_replay(
        bundle_id="synthetic_movement_2",
        revision="fixture-v1",
        _bundle_root=root,
        config=RuntimeConfig(run_id="replay-test"),
        notes=(),
    )
    control.wait_until_idle()

    assert manager.status().phase is RunPhase.COMPLETED
    assert manager.status().coordinate_system == "midi_performance_provisional"
    assert manager.status().canonical_position is False
    assert manager.status().performance_ready is False
    trace = tmp_path / "runs" / "replay-test" / "trace" / "runtime.jsonl"
    assert trace.exists()


def test_mix_audition_streams_score_events_through_live_output_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))
    control = LiveControl()
    port = FakePort()
    manager = LiveRuntimeManager(
        hardware_control=control,
        output_factory=lambda _name: port,
    )
    root = make_midi_v2_bundle(tmp_path)
    program = mix_store.create_program(MixProgramCreate(piece_id="chopin_op11", movement=2))

    preparing = manager.start_mix_audition(
        piece_id="chopin_op11",
        movement=2,
        program_id=program.program_id,
        expected_revision=program.revision,
        bundle_id="synthetic_movement_2",
        revision="fixture-v1",
        output_name="Yamaha",
        start_tick=0,
        end_tick=1920,
        tempo_bpm=600,
        _bundle_root=root,
    )
    finished = control.wait_until_idle(timeout=3)

    assert preparing.phase is RunPhase.PREPARING
    assert finished.running is False
    assert manager.status().phase is RunPhase.COMPLETED
    assert any(message.type == "note_on" and message.velocity > 0 for message in port.messages)
    trace = next((tmp_path / "runs").glob("mix-audition-*/trace/runtime.jsonl"))
    rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert any(row["type"] == "midi_output" and row["action"] == "note_on" for row in rows)


def test_mix_audition_can_route_only_to_room_renderer_without_yamaha_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeRoomRouter:
        def __init__(self) -> None:
            self.sent = []
            self.panics = []
            self.mix_ticks = []

        def send(self, event, *, sent_at: float) -> None:
            self.sent.append((sent_at, event))

        def panic(self, *, sent_at: float, reason: str) -> None:
            self.panics.append((sent_at, reason))

        def apply_mix_automation(self, score_tick: int, *, sent_at: float) -> None:
            self.mix_ticks.append((sent_at, score_tick))

        def set_master_volume(self, volume: float, *, sent_at: float) -> None:
            self.volume = (sent_at, volume)

        def close(self) -> None:
            pass

    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))
    control = LiveControl()
    opened_outputs: list[str] = []
    manager = LiveRuntimeManager(
        hardware_control=control,
        output_factory=lambda name: opened_outputs.append(name),
    )
    room = FakeRoomRouter()
    monkeypatch.setattr(manager, "_start_vst_router", lambda *args, **kwargs: room)
    root = make_midi_v2_bundle(tmp_path)
    program = mix_store.create_program(MixProgramCreate(piece_id="chopin_op11", movement=2))

    manager.start_mix_audition(
        piece_id="chopin_op11",
        movement=2,
        program_id=program.program_id,
        expected_revision=program.revision,
        bundle_id="synthetic_movement_2",
        revision="fixture-v1",
        output_name="",
        start_tick=0,
        end_tick=1920,
        tempo_bpm=600,
        volume=0.37,
        _bundle_root=root,
    )
    control.wait_until_idle(timeout=3)

    assert opened_outputs == []
    assert room.sent
    assert room.panics[-1][1] == "mix_audition_complete"
    assert room.volume[1] == pytest.approx(0.37)


def test_recorded_style_follow_replay_observes_mix_swell_without_hardware(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ProgressFollower:
        def __init__(self, *args, **kwargs) -> None:
            self.index = 0

        def observe(self, note):
            beat = (0.0, 0.5, 1.0)[min(self.index, 2)]
            self.index += 1
            return FollowerUpdate(
                perf_time=note.perf_time,
                score_beat=beat,
                confidence=1.0,
                raw_state={"follower": "progress"},
            )

        def close(self) -> None:
            pass

    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("RUBATO_FOLLOWER_PROCESS", "0")
    monkeypatch.setattr("aimusic.server.live_runtime.MatchmakerStreamFollower", ProgressFollower)
    monkeypatch.setattr("aimusic.server.live_runtime.find_spec", lambda _: object())
    program = mix_store.create_program(
        MixProgramCreate(piece_id="chopin_op11", movement=2, program_id="load-swell")
    )
    program = mix_store.add_region(
        "chopin_op11",
        2,
        program.program_id,
        expected_revision=program.revision,
        region=MixRegion(
            region_id="load-swell",
            start_tick=0,
            end_tick=961,
            gesture=Gesture.SWELL,
            routes=(
                MixRoute(
                    zone_id="room_center",
                    stem_ids=("orchestra",),
                    envelope=(
                        EnvelopePoint(score_tick=0, level=10),
                        EnvelopePoint(score_tick=960, level=90),
                    ),
                ),
            ),
        ),
    )
    root = make_midi_v2_bundle(tmp_path)
    control = LiveControl()
    manager = LiveRuntimeManager(
        hardware_control=control,
        input_factory=lambda _name: VirtualInputPort(
            (
                InjectedNote(0.0, 60),
                InjectedNote(0.1, 62),
                InjectedNote(0.2, 64),
            ),
            realtime=True,
        ),
        output_factory=lambda _name: FakePort(),
        audio_config_loader=LiveAudioConfig,
        mix_zones_factory=virtual_mix_zones,
        vst_router_factory=lambda _config, policy, sink, level: MixProbeRouter(
            policy,
            sink,
            telemetry_level=level,
            sample_interval_seconds=0.02,
        ),
    )

    manager.start_follow(
        bundle_id="synthetic_movement_2",
        revision="fixture-v1",
        _bundle_root=root,
        input_name="recorded-take",
        output_name="software-midi-sink",
        config=RuntimeConfig(
            run_id="mix-load-replay",
            initial_tempo_bpm=120,
            orchestra_volume=0.8,
            mix_program_id=program.program_id,
            mix_program_revision=program.revision,
            telemetry_level=TelemetryLevel.TRACE,
        ),
    )
    wait_for_phase(manager, RunPhase.ACTIVE)
    time.sleep(0.35)
    manager.stop()
    control.wait_until_idle()

    trace = tmp_path / "runs" / "mix-load-replay" / "trace" / "runtime.jsonl"
    rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    mix_rows = [row for row in rows if row["type"] == "mix_state"]
    assert len(mix_rows) >= 3
    assert {row["mix_program_revision"] for row in mix_rows} == {program.revision}
    assert max(row["route_gain_end"] for row in mix_rows) > min(
        row["route_gain_end"] for row in mix_rows
    )
    assert all(row["master_gain"] == pytest.approx(0.8) for row in mix_rows)
    assert any(
        row["type"] == "midi_output"
        and row["action"] == "channel_volume"
        and row["reason"] == "mix_policy"
        for row in rows
    )


@requires_oguri_derived
def test_movement_2_measure_start_resolves_canonical_and_reference_once() -> None:
    projection = project_bundle_v2_to_provisional_runtime(
        bundle_id=MOVEMENT_2_BUNDLE_ID,
        revision=None,
        registry=default_bundle_registry(),
    )
    scheduler = AccompanimentScheduler(projection.bundle)

    follow_entry = _runtime_entry_point(
        projection,
        start_measure=44,
    )
    lead_entry = _runtime_entry_point(
        projection,
        start_measure=22,
    )

    assert follow_entry is not None
    assert follow_entry.score_beat == 172.0
    assert follow_entry.reference_beat == pytest.approx(
        scheduler.reference_beat_at_score_beat(172.0)
    )
    assert follow_entry.follower_prior_reference_beat == follow_entry.reference_beat
    assert follow_entry.section_mode is AccompanimentMode.FOLLOW
    assert lead_entry is not None
    assert lead_entry.score_beat == 84.0
    assert lead_entry.section_mode is AccompanimentMode.LEAD


@requires_oguri_derived
def test_measure_start_rejects_stop_before_runtime_side_effects() -> None:
    projection = project_bundle_v2_to_provisional_runtime(
        bundle_id=MOVEMENT_2_BUNDLE_ID,
        revision=None,
        registry=default_bundle_registry(),
    )
    stop_projection = replace(
        projection,
        bundle=replace(
            projection.bundle,
            section_map=SectionMap(
                piece_id="stop-entry",
                sections=(
                    Section(
                        id="stop",
                        start_beat=0.0,
                        end_beat=1000.0,
                        mode=AccompanimentMode.STOP,
                    ),
                ),
            ),
        ),
    )

    with pytest.raises(ValueError, match="cannot start inside STOP section"):
        _runtime_entry_point(
            stop_projection,
            start_measure=44,
        )


def test_replay_slice_preserves_time_relative_to_nonzero_entry() -> None:
    notes = tuple(
        PerformedNote(
            perf_time=10.0 + index * 0.6,
            pitch=60 + index,
            velocity=70,
            score_beat=float(index),
        )
        for index in range(7)
    )

    sliced = _slice_replay_notes(notes, start_beat=3.0)

    assert [note.score_beat for note in sliced] == [3.0, 4.0, 5.0, 6.0]
    assert [note.perf_time for note in sliced] == pytest.approx([0.0, 0.6, 1.2, 1.8])


def test_replay_slice_preserves_a_later_repeat_in_performed_order() -> None:
    notes = tuple(
        PerformedNote(
            perf_time=float(index),
            pitch=60 + index,
            velocity=70,
            score_beat=beat,
        )
        for index, beat in enumerate((0.0, 1.0, 2.0, 3.0, 1.0, 2.0))
    )

    sliced = _slice_replay_notes(notes, start_beat=2.0)

    assert [note.score_beat for note in sliced] == [2.0, 3.0, 1.0, 2.0]
    assert [note.perf_time for note in sliced] == [0.0, 1.0, 2.0, 3.0]


def test_mid_piece_replay_matches_full_run_relative_accompaniment_timing(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))
    root = make_midi_v2_bundle(tmp_path)
    pitches = list(range(60, 68))
    _write_midi(root / "derived/solo_reference.mid", pitches=pitches, program=0)
    _write_midi(root / "derived/accompaniment.mid", pitches=pitches, program=48)
    notes = tuple(
        PerformedNote(
            perf_time=index * 0.5,
            pitch=pitch,
            velocity=70,
            score_beat=float(index),
        )
        for index, pitch in enumerate(pitches)
    )
    control = LiveControl()
    manager = LiveRuntimeManager(hardware_control=control)

    manager.start_replay(
        bundle_id="synthetic_movement_2",
        revision="fixture-v1",
        _bundle_root=root,
        config=RuntimeConfig(run_id="replay-full", initial_tempo_bpm=120),
        notes=notes,
    )
    control.wait_until_idle()
    manager.start_replay(
        bundle_id="synthetic_movement_2",
        revision="fixture-v1",
        _bundle_root=root,
        config=RuntimeConfig(run_id="replay-mid", initial_tempo_bpm=120),
        notes=notes,
        start_measure=2,
    )
    control.wait_until_idle()

    full_target = _scheduler_target_at(
        tmp_path / "runs" / "replay-full" / "trace" / "runtime.jsonl",
        score_beat=5.0,
    )
    mid_target = _scheduler_target_at(
        tmp_path / "runs" / "replay-mid" / "trace" / "runtime.jsonl",
        score_beat=5.0,
    )
    assert mid_target == pytest.approx(full_target - 2.0, abs=1e-12)
    mid_rows = [
        json.loads(line)
        for line in (tmp_path / "runs" / "replay-mid" / "trace" / "runtime.jsonl")
        .read_text()
        .splitlines()
    ]
    start = next(row for row in mid_rows if row["type"] == "runtime_start")
    assert start["score_beat"] == 4.0
    assert start["section_mode"] == "FOLLOW"


def _scheduler_target_at(path: Path, *, score_beat: float) -> float:
    for line in path.read_text().splitlines():
        row = json.loads(line)
        if row["type"] != "scheduler":
            continue
        for event in (*row["planned_events"], *row["dispatched_events"]):
            if event["score_beat"] == score_beat:
                return float(event["target_perf_time"])
    raise AssertionError(f"no scheduler trace event at beat {score_beat}")


def test_mocked_midi_follow_uses_shared_hardware_lock(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("RUBATO_FOLLOWER_PROCESS", "0")
    monkeypatch.setattr("aimusic.server.live_runtime.MatchmakerStreamFollower", FakeFollower)
    monkeypatch.setattr("aimusic.server.live_runtime.find_spec", lambda _: object())
    port = FakePort()
    control = LiveControl()
    root = make_midi_v2_bundle(tmp_path)
    manager = LiveRuntimeManager(
        hardware_control=control,
        input_factory=lambda _: FakeInput(),
        output_factory=lambda _: port,
    )

    FakeFollower.observed_perf_times.clear()
    observed_at = time.monotonic()
    manager.start_follow(
        bundle_id="synthetic_movement_2",
        revision="fixture-v1",
        _bundle_root=root,
        input_name="fake-in",
        output_name="fake-out",
        config=RuntimeConfig(run_id="follow-test", minimum_follower_confidence=0.5),
    )
    wait_for_phase(manager, RunPhase.ACTIVE)
    try:
        control.start_record(
            input_name="another-device", session_id="collision", duration_seconds=1
        )
    except RuntimeError as exc:
        assert "already running" in str(exc)
    else:
        raise AssertionError("a second hardware job should have been rejected")

    manager.stop()
    control.wait_until_idle()
    assert manager.status().phase is RunPhase.COMPLETED
    assert any(message.type == "note_on" for message in port.messages)
    assert FakeFollower.observed_perf_times
    assert FakeFollower.observed_perf_times[0] >= observed_at
    assert (tmp_path / "data" / "processed" / "follow-test" / "solo.mid").is_file()


def test_failed_live_run_keeps_partial_recording_for_debugging(tmp_path, monkeypatch) -> None:
    class FailingFollower(FakeFollower):
        def observe(self, note):
            raise RuntimeError(f"simulated follower failure after note {note.pitch}")

    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("RUBATO_FOLLOWER_PROCESS", "0")
    monkeypatch.setattr(
        "aimusic.server.live_runtime.MatchmakerStreamFollower",
        FailingFollower,
    )
    monkeypatch.setattr("aimusic.server.live_runtime.find_spec", lambda _: object())
    control = LiveControl()
    manager = LiveRuntimeManager(
        hardware_control=control,
        input_factory=lambda _: FakeInput(),
        output_factory=lambda _: FakePort(),
    )

    manager.start_follow(
        bundle_id="synthetic_movement_2",
        revision="fixture-v1",
        _bundle_root=make_midi_v2_bundle(tmp_path),
        input_name="fake-in",
        output_name="fake-out",
        config=RuntimeConfig(run_id="failed-follow"),
    )
    control.wait_until_idle(timeout=2.0)

    status = manager.status()
    assert status is not None
    assert status.phase is RunPhase.FAILED
    recording = tmp_path / "data" / "processed" / "failed-follow" / "solo.mid"
    assert recording.is_file()
    assert (
        len(
            [
                message
                for track in mido.MidiFile(recording).tracks
                for message in track
                if message.type == "note_on" and message.velocity > 0
            ]
        )
        == 1
    )


@requires_oguri_derived
def test_measure_start_warms_follower_and_locks_by_second_onset(
    tmp_path,
    monkeypatch,
) -> None:
    class WarmFollower:
        init_kwargs: dict[str, object] = {}
        observations = 0
        repositioned: tuple[float, float | None] | None = None

        def __init__(self, *args, **kwargs) -> None:
            _ = args
            WarmFollower.init_kwargs = kwargs
            self._prior = float(kwargs["initial_reference_beat"])

        def reposition_for_entry(
            self,
            *,
            score_beat: float,
            reference_beat: float | None,
        ) -> None:
            WarmFollower.repositioned = (score_beat, reference_beat)
            self._prior = score_beat if reference_beat is None else reference_beat

        def observe(self, note):
            WarmFollower.observations += 1
            stable = WarmFollower.observations
            return FollowerUpdate(
                perf_time=note.perf_time,
                score_beat=self._prior + (stable - 1) * 0.5,
                confidence=0.5 if stable >= 2 else 0.0,
                raw_state={
                    "follower": "matchmaker",
                    "stable_update_count": stable,
                    "warm_start_reference_beat": self._prior,
                },
            )

        def close(self) -> None:
            pass

    class TwoNoteInput:
        def __init__(self) -> None:
            self._sent = False

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            pass

        def iter_pending(self):
            if self._sent:
                return []
            self._sent = True
            return [
                mido.Message("note_on", note=60, velocity=80),
                mido.Message("note_on", note=62, velocity=80),
            ]

    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("RUBATO_FOLLOWER_PROCESS", "0")
    monkeypatch.setattr("aimusic.server.live_runtime.MatchmakerStreamFollower", WarmFollower)
    monkeypatch.setattr("aimusic.server.live_runtime.find_spec", lambda _: object())
    port = FakePort()
    control = LiveControl()
    manager = LiveRuntimeManager(
        hardware_control=control,
        input_factory=lambda _: TwoNoteInput(),
        output_factory=lambda _: port,
    )

    manager.start_follow(
        bundle_id=MOVEMENT_2_BUNDLE_ID,
        revision=None,
        input_name="fake-in",
        output_name="fake-out",
        config=RuntimeConfig(run_id="measure-warm-start", initial_tempo_bpm=76),
        start_measure=44,
    )
    deadline = time.monotonic() + 1.0
    while WarmFollower.observations < 2 and time.monotonic() < deadline:
        time.sleep(0.002)
    assert WarmFollower.observations == 2
    assert WarmFollower.repositioned is not None
    assert WarmFollower.init_kwargs["minimum_lock_updates"] == 2
    assert WarmFollower.init_kwargs["initial_reference_beat"] == pytest.approx(
        _runtime_entry_point(
            project_bundle_v2_to_provisional_runtime(
                bundle_id=MOVEMENT_2_BUNDLE_ID,
                revision=None,
                registry=default_bundle_registry(),
            ),
            start_measure=44,
        ).reference_beat
    )
    manager.stop()
    control.wait_until_idle()

    rows = [
        json.loads(line)
        for line in (tmp_path / "runs" / "measure-warm-start" / "trace" / "runtime.jsonl")
        .read_text()
        .splitlines()
    ]
    start = next(row for row in rows if row["type"] == "runtime_start")
    assert start["score_beat"] == 172.0
    assert start["start_kind"] == "orchestra_lead_in"
    assert start["reference_beat"] == pytest.approx(
        WarmFollower.init_kwargs["initial_reference_beat"]
    )
    assert not any(row["type"] == "count_off" for row in rows)
    follower_rows = [row for row in rows if row["type"] == "follower"]
    assert len(follower_rows) >= 2
    assert follower_rows[1]["follower_state"]["stable_update_count"] == 2
    assert follower_rows[1]["confidence"] >= 0.5


def test_orchestra_led_follow_sounds_before_any_input(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("RUBATO_FOLLOWER_PROCESS", "0")
    monkeypatch.setattr("aimusic.server.live_runtime.MatchmakerStreamFollower", FakeFollower)
    monkeypatch.setattr("aimusic.server.live_runtime.find_spec", lambda _: object())
    port = FakePort()
    control = LiveControl()
    root = make_midi_v2_bundle(tmp_path, solo_start_ticks=1920)
    manager = LiveRuntimeManager(
        hardware_control=control,
        input_factory=lambda _: EmptyInput(),
        output_factory=lambda _: port,
    )

    manager.start_follow(
        bundle_id="synthetic_movement_2",
        revision="fixture-v1",
        _bundle_root=root,
        input_name="fake-in",
        output_name="fake-out",
        config=RuntimeConfig(run_id="orchestra-led-follow", initial_tempo_bpm=120),
    )
    wait_for_phase(manager, RunPhase.ACTIVE)
    try:
        deadline = time.monotonic() + 1
        while not any(message.type == "note_on" for message in port.messages):
            if time.monotonic() >= deadline:
                raise AssertionError("opening orchestra did not sound without piano input")
            time.sleep(0.002)
        assert manager.status().state_word == "Leading"
        changed = manager.update_tempo(60.0)
        assert changed.tempo_bpm == 60.0
        assert changed.orchestra_tempo_bpm == 60.0
        volume_changed = manager.update_volume(0.25)
        assert volume_changed.orchestra_volume == 0.25
        expected_channel_volumes = {(0, round(91 * 0.25)), (9, round(100 * 0.25))}
        deadline = time.monotonic() + 1
        while not expected_channel_volumes.issubset(
            {
                (message.channel, message.value)
                for message in port.messages
                if message.type == "control_change" and message.control == 7
            }
        ):
            if time.monotonic() >= deadline:
                raise AssertionError("acknowledged channel volume changes did not reach MIDI")
            time.sleep(0.002)
        channel_volumes = [
            (message.channel, message.value)
            for message in port.messages
            if message.type == "control_change" and message.control == 7
        ]
        assert [
            message.value
            for message in port.messages
            if message.type == "control_change" and message.control == 7 and message.channel == 0
        ][-1] == round(91 * 0.25)
        assert channel_volumes[-1] == (9, round(100 * 0.25))
        advance_changed = manager.update_output_advance(40.0)
        assert advance_changed.orchestra_output_advance_ms == 40.0
    finally:
        manager.stop()
        control.wait_until_idle()


class EmptyInput:
    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        pass

    def iter_pending(self):
        return []


def test_projection_is_explicitly_noncanonical_and_uses_declared_midi(tmp_path) -> None:
    root = make_midi_v2_bundle(tmp_path)
    projection = project_bundle_v2_to_provisional_runtime(
        bundle_id="synthetic_movement_2",
        revision="fixture-v1",
        registry=BundleRegistry(),
        explicit_root=root,
    )

    assert projection.canonical is False
    assert projection.performance_ready is False
    assert projection.follower_reference_path.name == "solo_reference.mid"
    assert projection.bundle.metadata.source is not None
    assert "not the canonical timeline" in projection.bundle.metadata.source
    assert all(
        event.source_refs["coordinate_system"] == "midi_performance_provisional"
        for event in projection.bundle.events
    )
    assert projection.bundle.instrument_map[0].program == 48
    assert projection.bundle.instrument_map[0].volume == 91


def test_projection_declares_orchestra_opening_before_first_solo(tmp_path) -> None:
    root = make_midi_v2_bundle(tmp_path, solo_start_ticks=1920)
    projection = project_bundle_v2_to_provisional_runtime(
        bundle_id="synthetic_movement_2",
        revision="fixture-v1",
        registry=BundleRegistry(),
        explicit_root=root,
    )

    assert projection.orchestra_start_beat == 0.0
    assert projection.first_solo_entry_beat == 4.0
    assert projection.bundle.section_map.mode_at(0.0).value == "LEAD"
    assert projection.bundle.section_map.mode_at(4.0).value == "FOLLOW"


@requires_oguri_derived
def test_movement_2_projection_authors_measure_22_orchestra_interlude() -> None:
    projection = project_bundle_v2_to_provisional_runtime(
        bundle_id=MOVEMENT_2_BUNDLE_ID,
        revision=None,
        registry=default_bundle_registry(),
    )

    assert projection.bundle.section_map.section_at(84.0).id == "m22-orchestra-interlude"
    assert projection.bundle.section_map.mode_at(84.0).value == "LEAD"
    assert projection.bundle.section_map.mode_at(87.999).value == "LEAD"
    assert projection.bundle.section_map.mode_at(88.0).value == "FOLLOW"
    assert projection.bundle.section_map.mode_at(204.0).value == "LEAD"
    assert projection.bundle.section_map.mode_at(210.999).value == "LEAD"
    assert projection.bundle.section_map.mode_at(211.0).value == "FOLLOW"
    assert projection.bundle.section_map.mode_at(412.0).value == "LEAD"
    assert projection.source.manifest.revision == "movement2-symbolic-path-v4"


@requires_oguri_derived
def test_movement_2_measure_22_metronome_and_fourth_beat_are_complete() -> None:
    projection = project_bundle_v2_to_provisional_runtime(
        bundle_id=MOVEMENT_2_BUNDLE_ID,
        revision=None,
        registry=default_bundle_registry(),
    )
    scheduler = AccompanimentScheduler(projection.bundle)
    period = scheduler.reference_period_for_score_tempo(
        start_score_beat=84.0,
        end_score_beat=88.0,
        score_tempo_bpm=90.0,
    )
    start_reference = scheduler.reference_beat_at_score_beat(84.0)
    end_reference = scheduler.reference_beat_at_score_beat(88.0)
    assert start_reference is not None
    assert end_reference is not None

    # A four-quarter interlude at quarter=90 lasts 2.667 seconds even though
    # its expressive source coordinate spans about ten reference units.
    assert (end_reference - start_reference) * period == pytest.approx(4 * 60 / 90)
    fourth_beat_events = [
        event for event in projection.bundle.accompaniment_events if 87.0 <= event.beat < 88.0
    ]
    assert fourth_beat_events
    assert all(
        event.source_refs.get("source_performance_beat") is not None for event in fourth_beat_events
    )

    output = CapturingOutput()
    start_time = 100.0
    section_seconds = 4 * 60 / 90
    for step in range(0, 135):
        now = start_time + step * 0.02
        reference_beat = min(
            end_reference,
            start_reference + (now - start_time) / period,
        )
        score_beat = scheduler.score_beat_at_reference_beat(reference_beat)
        assert score_beat is not None
        canonical_period = scheduler.score_beat_period_at_reference_beat(reference_beat, period)
        scheduler.update(
            TempoState(
                perf_time=now,
                score_beat=score_beat,
                beat_period_seconds=canonical_period,
                tempo_bpm=60 / canonical_period,
                confidence=1.0,
                reference_beat=reference_beat,
                reference_beat_period_seconds=period,
            ),
            now=now,
            output=output,
            mode=AccompanimentMode.LEAD,
            planning_end_beat=88.0,
        )
    assert output.sent
    assert max(sent_at for sent_at, _ in output.sent) <= start_time + section_seconds + 0.02
    assert {item.event.event_id for _, item in output.sent if 87.0 <= item.event.beat < 88.0} == {
        event.event_id for event in fourth_beat_events
    }


def test_latest_tempo_mailbox_supersedes_obsolete_intermediate_values() -> None:
    commands = _LatestTempoCommand()
    completed: list[float] = []
    errors: list[str] = []

    def submit_first() -> None:
        try:
            commands.submit(60.0)
            completed.append(60.0)
        except RuntimeError as exc:
            errors.append(str(exc))

    first = threading.Thread(target=submit_first)
    first.start()
    deadline = time.monotonic() + 1
    while commands.next_pending() != (1, 60.0):
        if time.monotonic() >= deadline:
            raise AssertionError("first tempo command was not submitted")

    second = threading.Thread(target=lambda: (commands.submit(72.0), completed.append(72.0)))
    second.start()
    while True:
        pending = commands.next_pending()
        if pending == (2, 72.0):
            break
        if time.monotonic() >= deadline:
            raise AssertionError("latest tempo command did not supersede the first")
    commands.acknowledge(2)
    first.join(timeout=1)
    second.join(timeout=1)

    assert completed == [72.0]
    assert errors == ["Live tempo change was superseded by a newer request"]


def test_live_control_mailbox_has_a_bounded_acknowledgment_wait() -> None:
    commands = _LatestTempoCommand("volume")

    with pytest.raises(RuntimeError, match="volume change was not acknowledged"):
        commands.submit(0.25, timeout=0)


def test_machine_section_fallback_preserves_exact_note_release_boundaries() -> None:
    root = ScoreBundle.load(Path("tests/fixtures/score_bundles/synthetic_accompaniment_capability"))
    first = replace(root.solo_events[0], beat=0.0, duration_beats=2.25)
    second = replace(root.solo_events[1], beat=6.0, duration_beats=1.0)

    sections = _movement_2_sections((first, second), 8.0)
    lead = next(section for section in sections if section.mode.value == "LEAD")

    assert lead.start_beat == 2.25
    assert lead.end_beat == 6.0


def test_live_plan_uses_kept_rehearsal_tempo_for_orchestra_opening(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    root = make_midi_v2_bundle(tmp_path / "bundle-source", solo_start_ticks=1920)
    profile_path = tmp_path / "data" / "profiles" / "synthetic_concerto" / "2" / "profile.json"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        Interpretation(
            piece_id="synthetic_concerto",
            movement=2,
            take_count=3,
            updated=datetime.now(timezone.utc),
            base_seconds_per_quarter=1.0,
            cells=(
                PerformanceProfileCell(
                    score_tick=4 * 960,
                    seconds_per_quarter=1.0,
                    seconds_per_quarter_mad=0.05,
                    rubato_ratio=1.0,
                    rubato_ratio_mad=0.05,
                    velocity=64,
                    velocity_mad=2,
                    pedal=0,
                    pedal_mad=0,
                    support=3,
                    mean_quality=0.9,
                ),
            ),
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )
    manager = LiveRuntimeManager(hardware_control=LiveControl())

    plan = manager.performance_plan(
        bundle_id="synthetic_movement_2",
        revision="fixture-v1",
        _bundle_root=root,
    )

    assert plan.orchestra_starts_automatically is True
    assert plan.initial_tempo_bpm == 60.0
    assert plan.tempo_source == "performance_profile"
    assert plan.rehearsal_take_count == 3
    assert plan.follow_prior_take_count == 3


@pytest.mark.parametrize(
    (
        "start_measure",
        "start_beat",
        "follow_measure",
        "follow_beat_in_measure",
        "follow_score_beat",
    ),
    (
        (9, 32.0, 12, 3.0, 47.0),
        (17, 64.0, 17, 0.0, 64.0),
        (22, 84.0, 23, 0.0, 88.0),
        (53, 208.0, 53, 3.0, 211.0),
    ),
)
@requires_oguri_derived
def test_live_plan_states_verified_movement_2_authority_boundaries(
    tmp_path,
    monkeypatch,
    start_measure,
    start_beat,
    follow_measure,
    follow_beat_in_measure,
    follow_score_beat,
) -> None:
    """The UI plan must speak canonical score facts, never density guesses."""

    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "empty-data"))
    manager = LiveRuntimeManager(hardware_control=LiveControl())

    plan = manager.performance_plan(
        bundle_id=MOVEMENT_2_BUNDLE_ID,
        revision=None,
        start_measure=start_measure,
    )

    assert plan.orchestra_starts_automatically is True
    assert plan.orchestra_start is not None
    assert plan.orchestra_start.measure_index + 1 == start_measure
    assert plan.orchestra_start.score_beat == start_beat
    assert plan.follow_start is not None
    assert plan.follow_start.measure_index + 1 == follow_measure
    assert plan.follow_start.beat_in_measure == follow_beat_in_measure
    assert plan.follow_start.score_beat == follow_score_beat
    assert plan.tempo_source == "movement_default"
    assert plan.follow_prior_take_count == 0


def test_live_lte_loads_supported_interpretation_dispersion_curve(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    root = make_midi_v2_bundle(tmp_path / "bundle-source")
    profile_path = tmp_path / "data" / "profiles" / "synthetic_concerto" / "2" / "profile.json"
    profile_path.parent.mkdir(parents=True)
    cell = PerformanceProfileCell(
        score_tick=0,
        seconds_per_quarter=0.8,
        seconds_per_quarter_mad=0.0,
        rubato_ratio=1.0,
        rubato_ratio_mad=0.0,
        velocity=64,
        velocity_mad=0,
        pedal=0,
        pedal_mad=0,
        support=3,
        mean_quality=0.9,
    )
    interpretation = Interpretation(
        piece_id="synthetic_concerto",
        movement=2,
        take_count=3,
        updated=datetime.now(timezone.utc),
        base_seconds_per_quarter=0.8,
        cells=(cell,),
        revision=4,
        input_revision="abcdef1234567890",
    )
    profile_path.write_text(
        interpretation.model_dump_json(indent=2),
        encoding="utf-8",
    )
    projection = project_bundle_v2_to_provisional_runtime(
        bundle_id="synthetic_movement_2",
        revision="fixture-v1",
        registry=BundleRegistry(),
        explicit_root=root,
    )

    curve = _interpretation_arrival_curve(
        projection,
        RuntimeConfig(run_id="lte-prior", follow_clock="lte", initial_tempo_bpm=75),
    )

    assert isinstance(curve, InterpretationArrivalCurve)
    assert curve.trust_gain_at(0.0) == 1.0
    assert "r4-abcdef123456" in curve.curve_id
    assert (
        _interpretation_arrival_curve(
            projection,
            RuntimeConfig(run_id="reactive", follow_clock="reactive"),
        )
        is None
    )
    profile_path.write_text(
        interpretation.model_copy(
            update={"cells": (cell.model_copy(update={"support": 1}),)}
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )
    assert (
        _interpretation_arrival_curve(
            projection,
            RuntimeConfig(run_id="unsupported", follow_clock="lte"),
        )
        is None
    )


def test_runtime_status_stream_throttles_lead_clock_but_not_mode_changes(
    monkeypatch,
) -> None:
    published: list[object] = []
    monkeypatch.setattr("aimusic.server.live_runtime.events.publish", published.append)
    manager = LiveRuntimeManager(hardware_control=LiveControl())
    leading = RuntimeStatus(
        run_id="status-stream",
        run_mode="performance",
        phase="active",
        state_word="Leading",
        monotonic_time=1.0,
        score_beat=0.0,
        section_mode="LEAD",
    )

    # The UI publish is asynchronous and conflating (a newer status replaces an
    # unsent one), so drain between publishes to assert the throttle itself.
    manager._publish(leading, only_if_changed=True)
    manager._broadcaster.flush()
    manager._publish(
        leading.model_copy(update={"monotonic_time": 1.01, "score_beat": 0.01}),
        only_if_changed=True,
    )
    manager._broadcaster.flush()
    assert manager.status().score_beat == 0.01
    manager._publish(
        leading.model_copy(
            update={
                "monotonic_time": 1.02,
                "score_beat": 4.0,
                "section_mode": "HOLD",
                "state_word": "Waiting",
            }
        ),
        only_if_changed=True,
    )

    manager._broadcaster.flush()
    assert len(published) == 2
    assert published[-1].status.state_word == "Waiting"


def test_live_runtime_projects_follower_beat_to_display_score_position() -> None:
    status = RuntimeStatus(
        run_id="live-position",
        run_mode="rehearsal",
        phase="active",
        state_word="Following",
        monotonic_time=1.0,
        score_beat=144.245,
    )
    projection = SimpleNamespace(
        coordinate_system="midi_performance_provisional",
        source=SimpleNamespace(
            manifest=SimpleNamespace(work=SimpleNamespace(work_id="chopin_op11", movement_id="2"))
        ),
    )

    position = _runtime_score_position(status, projection)  # type: ignore[arg-type]

    assert position is not None
    assert position.measure_label == "12"
    assert position.mapping_review_state == "machine"
    assert position.canonical_position is True


def test_live_runtime_preserves_canonical_follower_position() -> None:
    status = RuntimeStatus(
        run_id="canonical-live-position",
        run_mode="performance",
        phase="active",
        state_word="Following",
        monotonic_time=1.0,
        score_beat=47.0,
        coordinate_system="canonical_score",
    )
    projection = SimpleNamespace(
        coordinate_system="canonical_score",
        source=SimpleNamespace(
            manifest=SimpleNamespace(work=SimpleNamespace(work_id="chopin_op11", movement_id="2"))
        ),
    )

    position = _runtime_score_position(status, projection)  # type: ignore[arg-type]

    assert position is not None
    assert position.measure_label == "12"
    assert position.beat_in_measure == 3.0


@requires_oguri_derived
def test_live_runtime_clamps_position_and_automation_after_final_barline() -> None:
    projection = project_bundle_v2_to_provisional_runtime(
        bundle_id=MOVEMENT_2_BUNDLE_ID,
        revision=None,
        registry=default_bundle_registry(),
    )
    status = RuntimeStatus(
        run_id="canonical-live-finish",
        run_mode="performance",
        phase="failed",
        state_word="Silent",
        monotonic_time=1.0,
        score_beat=504.01,
        coordinate_system="canonical_score",
    )

    position = _runtime_score_position(status, projection)

    assert position is not None
    assert position.measure_label == "126"
    assert _bounded_runtime_score_tick(status.score_beat, projection) == 483_839


@requires_oguri_derived
def test_movement_2_final_orchestra_chord_sustains_to_score_end() -> None:
    projection = project_bundle_v2_to_provisional_runtime(
        bundle_id=MOVEMENT_2_BUNDLE_ID,
        revision=None,
        registry=default_bundle_registry(),
    )
    final_onset = max(event.beat for event in projection.bundle.accompaniment_events)
    final_chord = tuple(
        event
        for event in projection.bundle.accompaniment_events
        if final_onset - event.beat <= 0.25
    )

    assert {event.pitch for event in final_chord} == {40}
    assert len(final_chord) == 2
    for event in final_chord:
        assert event.beat + event.duration_beats == pytest.approx(504.0)
        assert event.source_refs["terminal_sustain_to_score_end"] is True
        assert "source_performance_duration_beats" not in event.source_refs


def test_live_follower_maps_expressive_reference_beat_to_canonical_score() -> None:
    delegate = SimpleNamespace(
        observe=lambda note: FollowerUpdate(
            perf_time=note.perf_time,
            score_beat=10.0,
            confidence=1.0,
            raw_state={"follower": "expressive-reference"},
        )
    )
    mapped = _CanonicalFollower(
        delegate,
        SimpleNamespace(
            position_at_source_tick=lambda source_tick: SimpleNamespace(
                score_beat=source_tick / 1920,
                score_tick=source_tick // 2,
                confidence=0.8,
            )
        ),
    )

    update = mapped.observe(PerformedNote(perf_time=10.0, pitch=60, velocity=80))

    assert update is not None
    assert update.score_beat == 5.0
    assert update.confidence == 0.8
    assert update.raw_state["source_performance_beat"] == 10.0
    assert update.raw_state["mapping_confidence"] == 0.8


def test_default_registry_registers_repo_layout_when_manifest_exists(tmp_path) -> None:
    root = tmp_path / "data/scores/chopin_op11_movement_2"
    root.parent.mkdir(parents=True)
    source = make_midi_v2_bundle(tmp_path / "source")
    shutil.copytree(source, root)
    manifest_path = root / "bundle.yaml"
    payload = yaml.safe_load(manifest_path.read_text())
    payload["bundle_id"] = "chopin_op11_movement_2"
    payload["revision"] = "movement2-machine-draft-v1"
    manifest_path.write_text(yaml.safe_dump(payload, sort_keys=False))

    registry = default_bundle_registry(tmp_path)

    assert (
        registry.resolve("chopin_op11_movement_2", "movement2-machine-draft-v1") == root.resolve()
    )


def test_projection_rejects_missing_declared_midi(tmp_path) -> None:
    root = make_midi_v2_bundle(tmp_path)
    (root / "derived/accompaniment.mid").unlink()

    try:
        project_bundle_v2_to_provisional_runtime(
            bundle_id="synthetic_movement_2",
            revision="fixture-v1",
            registry=BundleRegistry(),
            explicit_root=root,
        )
    except ValueError as exc:
        assert "declared artifact does not exist" in str(exc)
    else:
        raise AssertionError("missing accompaniment MIDI must be rejected")


def test_input_reader_stamps_a_chord_with_one_timestamp() -> None:
    """A chord drained in one iter_pending() batch shares a single perf_time.

    Regression for the serialized-chord bug: the reader thread must stamp one
    clock.now() per batch, so simultaneous notes are not rolled apart by
    downstream processing time.
    """

    from collections import deque

    from aimusic.server.live_runtime import _read_input_into_queue

    class ChordPort:
        def __init__(self) -> None:
            self._done = False

        def iter_pending(self):
            if self._done:
                return []
            self._done = True
            return [mido.Message("note_on", note=n, velocity=80) for n in (60, 64, 67)]

    class CountingClock:
        def __init__(self) -> None:
            self.calls = 0

        def now(self) -> float:
            self.calls += 1
            return float(self.calls)

    sink: deque[tuple[float, int, int]] = deque()
    captured: list[tuple[float, mido.Message]] = []
    stop = threading.Event()
    reader = threading.Thread(
        target=_read_input_into_queue,
        args=(ChordPort(), CountingClock(), sink, captured, stop),
    )
    reader.start()
    for _ in range(2000):
        if len(sink) >= 3:
            break
        time.sleep(0.001)
    stop.set()
    reader.join(timeout=1.0)

    assert [note for _, note, _ in sink] == [60, 64, 67]
    stamps = {ts for ts, _, _ in sink}
    assert len(stamps) == 1  # all three notes share one timestamp
    assert [message.note for _, message in captured] == [60, 64, 67]


def test_loop_profiler_emits_window_stats() -> None:
    """LoopProfiler summarizes interval (cadence) and work (hot spots) per window."""

    from aimusic.accompaniment.runtime_io import MemoryTraceSink
    from aimusic.server.live_runtime import LoopProfiler

    class StepClock:
        def __init__(self) -> None:
            self.t = 0.0

        def now(self) -> float:
            return self.t

    sink = MemoryTraceSink()
    clock = StepClock()
    profiler = LoopProfiler("test", clock, sink, window_seconds=1.0)
    for i in range(12):
        clock.t = i * 0.1  # 100 ms cadence
        profiler.record(0.005)  # 5 ms work

    rows = [r for r in sink.records if getattr(r, "type", None) == "loop_timing"]
    assert rows, "expected at least one emitted window"
    row = rows[0]
    assert row.loop == "test"
    assert row.iterations > 0
    assert row.work_ms_median == pytest.approx(5.0, abs=0.5)
    assert row.interval_ms_median == pytest.approx(100.0, abs=5.0)


def test_a_live_run_is_captured_for_later_promotion(tmp_path, monkeypatch) -> None:
    """Every performance is scratch evidence before it becomes a kept take."""

    import mido

    from aimusic.server.live_runtime import _write_captured_performance

    monkeypatch.setattr(
        "aimusic.server.live_runtime.paths.session_dir", lambda take_id: tmp_path / take_id
    )
    # Two notes struck together, then one a beat later. Releases and pedal are
    # retained for faithful debugging playback, not synthesized by the writer.
    events = [
        (100.0, mido.Message("note_on", note=60, velocity=80)),
        (100.0, mido.Message("note_on", note=64, velocity=78)),
        (100.4, mido.Message("control_change", control=64, value=127)),
        (100.8, mido.Message("note_off", note=60, velocity=0)),
        (101.0, mido.Message("note_on", note=67, velocity=90)),
        (101.2, mido.Message("control_change", control=64, value=0)),
    ]
    path = _write_captured_performance("live-performance", events)

    assert path.exists()
    midi = mido.MidiFile(path)
    onsets = []
    elapsed = 0.0
    for message in midi.tracks[0]:
        elapsed += mido.tick2second(message.time, midi.ticks_per_beat, mido.bpm2tempo(120))
        if message.type == "note_on" and message.velocity > 0:
            onsets.append((elapsed, message.note, message.velocity))

    assert [n for _t, n, _v in onsets] == [60, 64, 67]
    assert onsets[0][0] == pytest.approx(0.0, abs=0.01), "the take starts at the first note"
    # Simultaneous notes stay simultaneous; the third lands a second later.
    assert onsets[1][0] == pytest.approx(onsets[0][0], abs=0.01)
    assert onsets[2][0] == pytest.approx(1.0, abs=0.05)
    assert onsets[0][2] == 80, "velocity is preserved for the learner"
    controls = [
        (message.control, message.value)
        for message in midi.tracks[0]
        if message.type == "control_change"
    ]
    assert controls == [(64, 127), (64, 0)], "pedal is preserved for playback"


def test_mix_resolution_is_identical_across_entry_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rehearse-from-a-bar and perform-from-the-top resolve one and the same mix.

    The divergence that made m.44 sound different by mode was one entry point
    applying the spatial mix while the other skipped it. Both now flow through a
    single resolver keyed on ``mix_enabled``, so a config that names no program
    (the rehearsal path) resolves the piece default exactly like one that names
    it (the live path), a stale revision pin is tolerated, and disabling the mix
    or missing the program is graceful rather than fatal.
    """

    fake_program = SimpleNamespace(revision=7)
    sentinel_policy = object()

    def fake_load(piece, movement, program_id, *, require_current_score):
        assert (piece, movement) == ("chopin_op11", 2)
        if program_id == "missing":
            raise mix_store.MixProgramNotFoundError("nope")
        if program_id == "stale":
            raise ValueError("mix authored against a different score revision")
        return fake_program

    monkeypatch.setattr("aimusic.server.live_runtime.mix_store.load_program", fake_load)
    monkeypatch.setattr(
        "aimusic.server.live_runtime.compile_mix_policy", lambda *a, **k: sentinel_policy
    )
    manager = LiveRuntimeManager(
        hardware_control=LiveControl(),
        audio_config_loader=lambda: None,
        mix_zones_factory=lambda _config: (),
    )
    base = RuntimeConfig(run_id="collapse", initial_tempo_bpm=120)

    # Rehearsal path names no program; live path names the default explicitly.
    pol_a, cfg_a = manager._resolve_mix_policy(base, None)
    pol_b, cfg_b = manager._resolve_mix_policy(
        base.model_copy(update={"mix_program_id": "main", "mix_program_revision": 2}), None
    )
    assert pol_a is sentinel_policy and pol_b is sentinel_policy
    assert cfg_a.mix_program_id == cfg_b.mix_program_id == "main"
    assert cfg_a.mix_program_revision == cfg_b.mix_program_revision == 7  # current wins over pin

    # Disabling the mix yields no policy regardless of a named program.
    pol_off, _ = manager._resolve_mix_policy(
        base.model_copy(update={"mix_enabled": False, "mix_program_id": "main"}), None
    )
    assert pol_off is None

    # A missing program is graceful (no mix), never a failed startup.
    pol_missing, _ = manager._resolve_mix_policy(
        base.model_copy(update={"mix_program_id": "missing"}), None
    )
    assert pol_missing is None

    # A *broken* program (stale score identity, malformed data) must also degrade
    # to no mix, never propagate: live performance is the one capability that can
    # never be blocked by mix or data quality.
    pol_stale, _ = manager._resolve_mix_policy(
        base.model_copy(update={"mix_program_id": "stale"}), None
    )
    assert pol_stale is None


def test_vst_startup_progress_reaches_manager_and_survives_engine_status() -> None:
    observed: list[dict[str, object]] = []

    def factory(_audio, _policy, _sink, _level, *, startup_observer=None):
        assert startup_observer is not None
        payload = {
            "state": "loading",
            "zone_id": "room_center",
            "instrument_id": "low_strings",
            "loaded": 1,
            "total": 4,
        }
        startup_observer(payload)
        observed.append(payload)
        return "router"

    manager = LiveRuntimeManager(
        hardware_control=LiveControl(),
        vst_router_factory=factory,
    )
    router = manager._start_vst_router(
        None,
        None,
        None,  # type: ignore[arg-type]
        TelemetryLevel.COUNTERS,
        startup_observer=observed.append,
    )
    assert router == "router"
    assert observed[-1]["instrument_id"] == "low_strings"

    manager._status = RuntimeStatus(
        run_id="renderer-status",
        run_mode="performance",
        phase="preparing",
        state_word="Listening",
        monotonic_time=1.0,
        orchestra_renderer_state="ready",
        orchestra_renderer_zone_id="room_center",
        orchestra_renderer_loaded_instruments=4,
        orchestra_renderer_total_instruments=4,
    )
    manager._publish(
        RuntimeStatus(
            run_id="renderer-status",
            run_mode="performance",
            phase="active",
            state_word="Leading",
            monotonic_time=2.0,
        )
    )
    status = manager.status()
    assert status is not None
    assert status.orchestra_renderer_state == "ready"
    assert status.orchestra_renderer_loaded_instruments == 4


def test_renderer_preloads_once_and_is_leased_across_live_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PWA preload owns the heavy router; Go Live borrows it without reloading."""

    class FakeRouter:
        def __init__(self) -> None:
            self.alive = True
            self.closed = False
            self.trace_sinks: list[object] = []
            self.panic_waits = 0

        @property
        def is_alive(self) -> bool:
            return self.alive

        def set_trace_sink(self, sink: object) -> None:
            self.trace_sinks.append(sink)

        def panic_and_wait(self, **_: object) -> None:
            self.panic_waits += 1

        def close(self) -> None:
            self.closed = True
            self.alive = False

    audio_config = SimpleNamespace(
        zones=(SimpleNamespace(zone_id="room_center", output_device_name="LG TV SSCR2"),),
        model_dump_json=lambda: '{"device":"LG TV SSCR2"}',
    )
    mix_policy = SimpleNamespace(model_dump_json=lambda: '{"program":"main"}')
    router = FakeRouter()
    starts: list[int] = []

    def start_router(_audio, _policy, _sink, _level, *, startup_observer=None):
        starts.append(1)
        assert startup_observer is not None
        for payload in (
            {
                "state": "loading",
                "zone_id": "room_center",
                "instrument_id": "violins",
                "loaded": 0,
                "total": 4,
            },
            {
                "state": "opening_audio",
                "zone_id": "room_center",
                "loaded": 4,
                "total": 4,
            },
            {
                "state": "ready",
                "zone_id": "room_center",
                "loaded": 4,
                "total": 4,
            },
        ):
            startup_observer(payload)
        return router

    monkeypatch.setattr(
        "aimusic.server.live_runtime.mix_store.load_program", lambda *a, **k: object()
    )
    monkeypatch.setattr(
        "aimusic.server.live_runtime.compile_mix_policy", lambda *a, **k: mix_policy
    )
    monkeypatch.setattr(
        "aimusic.server.live_runtime.paths.run_trace_dir",
        lambda preload_id: tmp_path / preload_id / "trace",
    )
    manager = LiveRuntimeManager(
        hardware_control=LiveControl(),
        audio_config_loader=lambda: audio_config,  # type: ignore[arg-type]
        mix_zones_factory=lambda _config: (),
        vst_router_factory=start_router,
    )

    initial = manager.preload_renderer()
    assert initial.state == "loading"
    deadline = time.monotonic() + 2
    while manager.renderer_status().state != "ready" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager.renderer_status().state == "ready"
    assert manager.renderer_status().device_name == "LG TV SSCR2"
    assert starts == [1]

    lease = manager._lease_resident_renderer(audio_config, mix_policy, object())  # type: ignore[arg-type]
    assert lease is not None
    lease.close()
    assert router.closed is False
    assert router.panic_waits == 1
    assert starts == [1]

    stopped = manager.stop_preloaded_renderer()
    assert stopped.state == "not_loaded"
    assert router.closed is True


def test_live_vst_router_isolates_and_quiesces_each_patch_during_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never put the third BBCSO instance into a host that supports only two."""

    bindings = tuple(
        VstInstrumentBinding(
            instrument_id=name,
            stem_ids=(f"stem-{index}",),
            plugin_path=tmp_path / "BBCSO.vst3",
            plugin_state_path=tmp_path / f"{name}.state",
            midi_channel=index,
        )
        for index, name in enumerate(("violins", "low_strings", "woodwinds", "horns"))
    )
    zone = LiveVstZoneConfig(
        zone_id="room_center",
        label="LG",
        output_device_name="LG TV SSCR2",
        acoustic_position="room",
        residual_error_p95_ms=1,
        calibration_revision="test",
        instruments=bindings,
    )
    policy = SimpleNamespace(
        default_routes=(SimpleNamespace(active_zone_id="room_center"),),
        regions=(),
    )
    constructed: list[str] = []
    lifecycle: list[tuple[str, str]] = []

    class FakeWorker:
        def __init__(self, isolated_zone, _policy, **kwargs):
            assert len(isolated_zone.instruments) == 1
            binding = isolated_zone.instruments[0]
            self.instrument_id = binding.instrument_id
            constructed.append(binding.instrument_id)
            observer = kwargs["startup_observer"]
            for index, binding in enumerate(isolated_zone.instruments):
                observer(
                    {
                        "state": "loading",
                        "zone_id": isolated_zone.zone_id,
                        "instrument_id": binding.instrument_id,
                        "loaded": index,
                        "total": len(isolated_zone.instruments),
                    }
                )
            observer(
                {
                    "state": "ready",
                    "zone_id": isolated_zone.zone_id,
                    "instrument_id": None,
                    "loaded": len(isolated_zone.instruments),
                    "total": len(isolated_zone.instruments),
                }
            )

        def close(self) -> None:
            pass

        def suspend(self) -> None:
            lifecycle.append((self.instrument_id, "suspend"))

        def resume(self) -> None:
            lifecycle.append((self.instrument_id, "resume"))

    class FakeMixer:
        def __init__(self, mixed_zone, mixed_workers, **_kwargs):
            assert mixed_zone is zone
            assert len(mixed_workers) == 4

        def close(self) -> None:
            pass

    monkeypatch.setattr("aimusic.server.live_runtime.LiveVstZoneWorker", FakeWorker)
    monkeypatch.setattr("aimusic.server.live_runtime.LiveVstZoneMixer", FakeMixer)
    progress: list[dict[str, object]] = []
    router = _start_live_vst_router(
        LiveAudioConfig(zones=(zone,)),
        policy,
        object(),  # type: ignore[arg-type]
        startup_observer=progress.append,
    )

    assert router is not None
    assert constructed == ["violins", "low_strings", "woodwinds", "horns"]
    assert lifecycle == [
        ("violins", "suspend"),
        ("low_strings", "suspend"),
        ("woodwinds", "suspend"),
        ("horns", "suspend"),
        ("violins", "resume"),
        ("low_strings", "resume"),
        ("woodwinds", "resume"),
        ("horns", "resume"),
    ]
    assert [item["instrument_id"] for item in progress if item["state"] == "loading"] == [
        "violins",
        "low_strings",
        "woodwinds",
        "horns",
    ]
    assert progress[-1]["state"] == "ready"
    assert progress[-1]["loaded"] == progress[-1]["total"] == 4


@pytest.mark.parametrize("worker_exit_code", [-11, -15])
def test_live_vst_router_retries_one_native_cold_start_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_exit_code: int
) -> None:
    binding = VstInstrumentBinding(
        instrument_id="woodwinds",
        stem_ids=("woodwinds",),
        plugin_path=tmp_path / "BBCSO.vst3",
        plugin_state_path=tmp_path / "woodwinds.state",
    )
    zone = LiveVstZoneConfig(
        zone_id="room_center",
        label="LG",
        output_device_name="LG TV SSCR2",
        acoustic_position="room",
        residual_error_p95_ms=1,
        calibration_revision="test",
        instruments=(binding,),
    )
    policy = SimpleNamespace(
        default_routes=(SimpleNamespace(active_zone_id="room_center"),),
        regions=(),
    )
    attempts = 0

    class FlakyWorker:
        def __init__(self, isolated_zone, _policy, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise LiveVstError(
                    "native cold-start failure", worker_exit_code=worker_exit_code
                )
            kwargs["startup_observer"](
                {
                    "state": "ready",
                    "zone_id": isolated_zone.zone_id,
                    "loaded": 1,
                    "total": 1,
                }
            )

        def close(self) -> None:
            pass

        def suspend(self) -> None:
            pass

        def resume(self) -> None:
            pass

    class FakeMixer:
        def __init__(self, _zone, mixed_workers, **_kwargs):
            assert len(mixed_workers) == 1

        def close(self) -> None:
            pass

    monkeypatch.setattr("aimusic.server.live_runtime.LiveVstZoneWorker", FlakyWorker)
    monkeypatch.setattr("aimusic.server.live_runtime.LiveVstZoneMixer", FakeMixer)
    monkeypatch.setattr("aimusic.server.live_runtime.time.sleep", lambda _seconds: None)

    progress: list[dict[str, object]] = []
    router = _start_live_vst_router(
        LiveAudioConfig(zones=(zone,)),
        policy,
        object(),  # type: ignore[arg-type]
        startup_observer=progress.append,
    )

    assert router is not None
    assert attempts == 2
    assert any(item.get("detail") == "Native host exited; retrying once" for item in progress)
    assert progress[-1]["state"] == "ready"


def test_failed_renderer_does_not_auto_retry_until_explicitly_forced() -> None:
    manager = LiveRuntimeManager(hardware_control=LiveControl())
    manager._renderer_status = OrchestraRendererStatus(
        state="failed",
        preload_id="native-crash",
        updated_at_monotonic=1.0,
        message="BBCSO worker crashed",
        worker_exit_code=-11,
    )

    status = manager.preload_renderer()

    assert status.preload_id == "native-crash"
    assert status.worker_exit_code == -11
    assert manager._renderer_thread is None


def test_renderer_status_invalidates_stale_ready_worker_without_auto_retry() -> None:
    class DeadRouter:
        is_alive = False
        failure_details = (
            {
                "zone_id": "room_center",
                "instrument_id": "violins",
                "exit_code": -11,
                "last_stage": "ready",
                "block_frames": 512,
                "sample_rate": 48_000,
            },
        )

    manager = LiveRuntimeManager(hardware_control=LiveControl())
    manager._resident_vst_router = DeadRouter()  # type: ignore[assignment]
    manager._renderer_status = OrchestraRendererStatus(
        state="ready",
        preload_id="stale-ready",
        loaded_instruments=4,
        total_instruments=4,
        updated_at_monotonic=1.0,
        message="BBCSO ready",
    )
    # Past the health grace window: a sustained failure, not a transient blip.
    manager._resident_unhealthy_monotonic = time.monotonic() - 999

    status = manager.renderer_status()

    assert status.state == "failed"
    assert status.instrument_id == "violins"
    assert status.worker_exit_code == -11
    assert manager.preload_renderer().preload_id == "stale-ready"
    assert manager._renderer_thread is None


def _write_reaper_heartbeat(path: Path, *, state: str, age_seconds: float) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at_epoch": time.time() - age_seconds,
                "state": state,
                "message": f"bridge {state}",
                "total_tracks": 9,
                "ready_tracks": 9 if state == "ready" else 0,
            }
        ),
        encoding="utf-8",
    )


def _reaper_runtime_with_heartbeat(path: Path, *, timeout: float = 10.0) -> LiveRuntimeManager:
    zone = SimpleNamespace(
        renderer="reaper",
        reaper_heartbeat_path=path,
        reaper_heartbeat_timeout_seconds=timeout,
    )
    audio_config = SimpleNamespace(zones=[zone])
    return LiveRuntimeManager(
        hardware_control=LiveControl(),
        audio_config_loader=lambda: audio_config,
    )


def test_reaper_heartbeat_recovered_true_only_when_fresh_and_ready(tmp_path: Path) -> None:
    heartbeat = tmp_path / "renderer-status.json"
    manager = _reaper_runtime_with_heartbeat(heartbeat)

    _write_reaper_heartbeat(heartbeat, state="ready", age_seconds=1.0)
    assert manager._reaper_heartbeat_recovered() is True

    _write_reaper_heartbeat(heartbeat, state="ready", age_seconds=999.0)
    assert manager._reaper_heartbeat_recovered() is False

    _write_reaper_heartbeat(heartbeat, state="setup_required", age_seconds=1.0)
    assert manager._reaper_heartbeat_recovered() is False


def test_reaper_heartbeat_recovered_false_without_reaper_zone(tmp_path: Path) -> None:
    manager = LiveRuntimeManager(
        hardware_control=LiveControl(),
        audio_config_loader=lambda: SimpleNamespace(zones=[]),
    )
    assert manager._reaper_heartbeat_recovered() is False


def test_client_status_auto_retries_when_bridge_heartbeat_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LiveRuntimeManager(hardware_control=LiveControl())
    manager._renderer_status = OrchestraRendererStatus(
        state="failed",
        preload_id="stale-fail",
        updated_at_monotonic=1.0,
        message="REAPER bridge heartbeat is stale (769.7s old)",
    )
    monkeypatch.setattr(manager, "_reaper_heartbeat_recovered", lambda: True)
    calls: list[bool] = []

    def fake_preload(*, program_id: str = "main", force: bool = False) -> OrchestraRendererStatus:
        calls.append(force)
        return OrchestraRendererStatus(
            state="loading", preload_id="retry", updated_at_monotonic=2.0, message="retrying"
        )

    monkeypatch.setattr(manager, "preload_renderer", fake_preload)

    status = manager.renderer_status_for_client()

    assert calls == [True]
    assert status.state == "loading"
    assert status.preload_id == "retry"


def test_client_status_retry_is_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = LiveRuntimeManager(hardware_control=LiveControl())
    manager._renderer_status = OrchestraRendererStatus(
        state="failed", preload_id="stale-fail", updated_at_monotonic=1.0, message="stale"
    )
    monkeypatch.setattr(manager, "_reaper_heartbeat_recovered", lambda: True)
    calls: list[bool] = []

    def fake_preload(*, program_id: str = "main", force: bool = False) -> OrchestraRendererStatus:
        calls.append(force)
        # Leave the public status failed so the second poll would retry if uncooled.
        return manager._renderer_status.model_copy(deep=True)

    monkeypatch.setattr(manager, "preload_renderer", fake_preload)

    manager.renderer_status_for_client()
    manager.renderer_status_for_client()

    assert calls == [True]  # second poll is inside the cooldown window


def test_client_status_does_not_retry_when_heartbeat_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LiveRuntimeManager(hardware_control=LiveControl())
    manager._renderer_status = OrchestraRendererStatus(
        state="failed", preload_id="stale-fail", updated_at_monotonic=1.0, message="stale"
    )
    monkeypatch.setattr(manager, "_reaper_heartbeat_recovered", lambda: False)
    monkeypatch.setattr(
        manager,
        "preload_renderer",
        lambda **_: pytest.fail("must not retry while the heartbeat is unhealthy"),
    )

    status = manager.renderer_status_for_client()

    assert status.state == "failed"
    assert status.preload_id == "stale-fail"


def test_client_status_passes_through_a_healthy_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = LiveRuntimeManager(hardware_control=LiveControl())
    manager._renderer_status = OrchestraRendererStatus(
        state="ready", preload_id="live", updated_at_monotonic=1.0, message="ready"
    )
    monkeypatch.setattr(
        manager,
        "_reaper_heartbeat_recovered",
        lambda: pytest.fail("healthy status must not probe the heartbeat"),
    )

    status = manager.renderer_status_for_client()

    assert status.state == "ready"


def test_input_reader_reconnects_after_a_device_dropout() -> None:
    """A mid-run device blip must not silently drop the soloist forever."""
    from collections import deque as _deque

    from aimusic.server import live_runtime as lr

    stop = threading.Event()
    sink: _deque = _deque()
    caps: list = []

    class _Clock:
        def now(self) -> float:
            return time.monotonic()

    class DroppingPort:
        def __init__(self) -> None:
            self.calls = 0

        def iter_pending(self):
            self.calls += 1
            if self.calls == 1:
                return []
            raise OSError("device disconnected")

        def close(self) -> None:
            pass

    class LivePort:
        def __init__(self) -> None:
            self.delivered = False

        def iter_pending(self):
            if not self.delivered:
                self.delivered = True
                return [mido.Message("note_on", note=60, velocity=64)]
            stop.set()  # end the loop after the reconnected note lands
            return []

        def close(self) -> None:
            pass

    live_port = LivePort()
    opened: list[str] = []

    def factory(name: str) -> LivePort:
        opened.append(name)
        return live_port

    lr._read_input_into_queue(
        DroppingPort(),
        _Clock(),
        sink,
        caps,
        stop,
        None,
        input_name="Clavinova",
        input_factory=factory,
    )

    assert opened == ["Clavinova"]  # reconnected by name after the dropout
    assert list(sink) and sink[0][1] == 60 and sink[0][2] == 64  # note survived
    assert any(msg.type == "note_on" and msg.note == 60 for _, msg in caps)


def test_input_reader_without_factory_stops_quietly_on_error() -> None:
    """Preserve the old teardown behavior when no reconnect factory is given."""
    from collections import deque as _deque

    from aimusic.server import live_runtime as lr

    class _Clock:
        def now(self) -> float:
            return time.monotonic()

    class FailingPort:
        def iter_pending(self):
            raise OSError("closed under us")

    # Should return promptly rather than raise or loop forever.
    lr._read_input_into_queue(
        FailingPort(), _Clock(), _deque(), [], threading.Event(), None
    )


def test_keep_awake_starts_and_releases_a_wake_assertion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aimusic.server import live_runtime as lr

    class FakeProc:
        def __init__(self) -> None:
            self.pid = 4321
            self.terminated = False

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:  # pragma: no cover - only on wait timeout
            self.terminated = True

    created: list[list[str]] = []
    proc = FakeProc()

    def fake_popen(args, **kwargs):
        created.append(args)
        return proc

    monkeypatch.setattr(lr.subprocess, "Popen", fake_popen)

    with lr._keep_awake("test-run"):
        assert created and created[0][0] == "/usr/bin/caffeinate"
        assert proc.terminated is False  # held for the duration

    assert proc.terminated is True  # released on exit


def test_keep_awake_tolerates_a_missing_caffeinate(monkeypatch: pytest.MonkeyPatch) -> None:
    from aimusic.server import live_runtime as lr

    def boom(*_a, **_k):
        raise FileNotFoundError("no caffeinate")

    monkeypatch.setattr(lr.subprocess, "Popen", boom)

    # The run must proceed even if the wake assertion cannot be held.
    with lr._keep_awake("test-run"):
        pass


def test_resident_router_tolerates_a_transient_device_blip() -> None:
    """A brief unhealthy read (audio device re-open) must NOT fail the renderer."""

    class BlippingRouter:
        def __init__(self) -> None:
            self.alive = False

        @property
        def is_alive(self) -> bool:
            return self.alive

    manager = LiveRuntimeManager(hardware_control=LiveControl())
    router = BlippingRouter()

    # First unhealthy read starts the grace timer but does not fail yet.
    assert manager._resident_router_failed(router) is False
    assert manager._resident_unhealthy_monotonic is not None
    # Still within grace: keep tolerating.
    assert manager._resident_router_failed(router) is False
    # Device recovers before the grace elapses -> timer clears, healthy again.
    router.alive = True
    assert manager._resident_router_failed(router) is False
    assert manager._resident_unhealthy_monotonic is None


def test_resident_router_fails_after_sustained_unhealth() -> None:
    class DeadRouter:
        is_alive = False

    manager = LiveRuntimeManager(hardware_control=LiveControl())
    router = DeadRouter()

    assert manager._resident_router_failed(router) is False  # grace starts
    # Simulate the grace window elapsing.
    manager._resident_unhealthy_monotonic = time.monotonic() - (
        _RESIDENT_HEALTH_GRACE_SECONDS + 1
    )
    assert manager._resident_router_failed(router) is True
