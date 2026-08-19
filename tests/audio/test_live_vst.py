from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from aimusic.accompaniment.runtime_contracts import AudioWorkerTrace, MidiOutputTrace
from aimusic.accompaniment.runtime_io import MemoryTraceSink
from aimusic.accompaniment.scheduler import ScheduledAccompanimentEvent
from aimusic.accompaniment.score_bundle import ScoreEvent
from aimusic.accompaniment.section_policy import AccompanimentMode
from aimusic.audio.live_config import LiveVstZoneConfig, VstInstrumentBinding
from aimusic.audio.live_vst import (
    LiveVstZoneMixer,
    LiveVstZoneWorker,
    MultiZoneAccompanimentOutput,
    _bindings_for_part,
    _master_gain,
    _queue_depth,
    _score_tick_at,
    _worker_main,
)
from aimusic.audio.mix_gauges import MixGaugeBlock
from aimusic.mixing.models import MixProgram, MixRoute, ZoneConfig, ZoneHealth
from aimusic.mixing.policy import compile_mix_policy


def binding(tmp_path: Path, instrument_id: str, stems: tuple[str, ...]):
    return VstInstrumentBinding(
        instrument_id=instrument_id,
        stem_ids=stems,
        plugin_path=tmp_path / "plugin.vst3",
        plugin_state_path=tmp_path / f"{instrument_id}.state",
    )


def zone(tmp_path: Path) -> LiveVstZoneConfig:
    return LiveVstZoneConfig(
        zone_id="room_center",
        label="Room",
        output_device_name="Fake device",
        acoustic_position="center",
        configured_output_advance_ms=0,
        residual_error_p95_ms=1,
        calibration_revision="test",
        sample_rate=1_000,
        block_size=64,
        prefill_blocks=1,
        instruments=(
            binding(tmp_path, "orchestra", ("orchestra",)),
            binding(tmp_path, "oboe", ("oboe",)),
        ),
    )


def policy():
    program = MixProgram(
        program_id="main",
        name="Main",
        piece_id="piece",
        movement=1,
        score_bundle_id="bundle",
        score_bundle_revision="1",
        timeline_digest="digest",
        updated_at=datetime.now(timezone.utc),
        default_routes=(MixRoute(zone_id="room_center", stem_ids=("orchestra",), level=50),),
    )
    return compile_mix_policy(
        program,
        (
            ZoneConfig(
                zone_id="room_center",
                label="Room",
                renderer_id="vst",
                acoustic_position="center",
                configured_output_advance_ms=0,
                residual_error_p95_ms=1,
                calibration_revision="test",
                health=ZoneHealth.READY,
            ),
        ),
    )


def test_exact_patch_binding_wins_over_orchestra_fallback(tmp_path: Path) -> None:
    bindings = zone(tmp_path).instruments

    assert [item.instrument_id for item in _bindings_for_part(bindings, "oboe")] == ["oboe"]
    assert [item.instrument_id for item in _bindings_for_part(bindings, "horn")] == ["orchestra"]


def test_transport_anchor_extrapolates_score_position() -> None:
    anchors = ((10.0, 960), (11.0, 1920))

    assert _score_tick_at(11.5, anchors) == 2400
    assert _score_tick_at(9.0, anchors) == 0


def test_audio_fader_uses_safe_perceptual_gain_curve() -> None:
    assert _master_gain(0.0) == 0.0
    assert _master_gain(0.5) == pytest.approx(0.25)
    assert _master_gain(1.0) == 1.0


def test_queue_depth_is_best_effort_on_macos_multiprocessing_queues() -> None:
    class UnsupportedQueue:
        def qsize(self) -> int:
            raise NotImplementedError

    assert _queue_depth(UnsupportedQueue()) == 0


def test_release_retimes_are_coalesced_to_one_audio_block() -> None:
    worker = object.__new__(LiveVstZoneWorker)
    worker.zone = SimpleNamespace(
        configured_output_advance_ms=0.0,
        block_size=512,
        sample_rate=48_000.0,
    )
    worker._pending_targets = {"note-1": 10.0}
    worker._release_targets = {"note-1": 11.0}
    worker._release_retime_sent_at = {"note-1": 0.0}
    worker._release_retime_queued = {}
    worker._rendered_until = SimpleNamespace(value=10.5)
    commands = []
    worker._put = lambda command: commands.append(command)

    assert worker.retime_release("note-1", scheduled_at=11.005)
    assert commands == []
    assert worker.retime_release("note-1", scheduled_at=11.02)
    assert commands == [("retime_release", ("note-1", 11.02))]

    worker._rendered_until.value = 11.03
    assert not worker.retime_release("note-1", scheduled_at=11.04)
    assert "note-1" not in worker._release_targets


def test_release_retimes_allow_only_one_inflight_command_per_note() -> None:
    worker = object.__new__(LiveVstZoneWorker)
    worker.zone = SimpleNamespace(
        zone_id="room_center",
        configured_output_advance_ms=0.0,
        block_size=512,
        sample_rate=48_000.0,
        instruments=(SimpleNamespace(instrument_id="violins"),),
    )
    worker._trace_sink = MemoryTraceSink()
    worker._pending_targets = {"note-1": 10.0}
    worker._release_targets = {"note-1": 11.0}
    worker._release_retime_sent_at = {"note-1": 0.0}
    worker._release_retime_queued = {}
    worker._rendered_until = SimpleNamespace(value=10.5)
    commands = []
    worker._put = lambda command: commands.append(command)

    assert worker.retime_release("note-1", scheduled_at=11.02)
    assert worker.retime_release("note-1", scheduled_at=11.20)
    assert commands == [("retime_release", ("note-1", 11.02))]

    worker._accept_response("retime_release_applied", ("note-1", 11.02))

    assert commands == [
        ("retime_release", ("note-1", 11.02)),
        ("retime_release", ("note-1", 11.20)),
    ]


def test_response_observer_captures_release_and_completes_panic_barrier() -> None:
    """Resident telemetry drains even when no scheduler command follows it."""

    worker = object.__new__(LiveVstZoneWorker)
    worker.zone = SimpleNamespace(zone_id="room_center")
    worker._responses = queue.Queue()
    worker._trace_sink = MemoryTraceSink()
    worker._response_observer_stop = threading.Event()
    worker._response_condition = threading.Condition()
    worker._completed_panic_barriers = set()
    worker._pending_targets = {"note-1": 10.0}
    worker._release_targets = {"note-1": 11.0}
    worker._release_retime_sent_at = {"note-1": 1.0}
    worker._release_retime_queued = {"note-1": 11.0}
    worker._error = None
    worker._process = SimpleNamespace(is_alive=lambda: True)
    observer = threading.Thread(target=worker._observe_responses, daemon=True)
    observer.start()
    worker._response_observer = observer

    for action in ("note_on", "note_off"):
        worker._responses.put(
            (
                "midi_output",
                MidiOutputTrace(
                    type="midi_output",
                    monotonic_time=time.monotonic(),
                    action=action,
                    renderer="live_vst",
                    zone_id="room_center",
                    instrument_id="violins",
                    event_id="note-1",
                    channel=0,
                    pitch=60,
                    velocity=80 if action == "note_on" else 0,
                ).model_dump(mode="python"),
            )
        )
    worker._responses.put(("panic_complete", "barrier-1"))

    assert worker.wait_for_panic("barrier-1", timeout_seconds=0.5)
    worker._response_observer_stop.set()
    observer.join(timeout=0.5)

    assert [row.action for row in worker._trace_sink.records] == ["note_on", "note_off"]
    assert "note-1" not in worker._release_targets


def test_release_retime_ack_is_traceable_on_the_audio_timeline() -> None:
    worker = object.__new__(LiveVstZoneWorker)
    worker.zone = SimpleNamespace(
        zone_id="room_center",
        block_size=512,
        sample_rate=48_000.0,
        configured_output_advance_ms=0.0,
        instruments=(SimpleNamespace(instrument_id="violins"),),
    )
    worker._trace_sink = MemoryTraceSink()
    worker._release_targets = {"note-1": 11.0}
    worker._release_retime_queued = {"note-1": 11.0}
    worker._release_retime_sent_at = {"note-1": 1.0}
    worker._rendered_until = SimpleNamespace(value=10.0)
    worker._put = lambda _command: None

    worker._accept_response("retime_release_applied", ("note-1", 11.0))

    row = worker._trace_sink.records[-1]
    assert row.action == "release_retime"
    assert row.event_id == "note-1"
    assert row.instrument_id == "violins"
    assert row.scheduled_note_off_time == 11.0


class FakePlugin:
    def __init__(self) -> None:
        self.messages: list[tuple[bytes, float]] = []

    def process(self, messages, *, duration, sample_rate, num_channels, buffer_size, reset):
        _ = duration, sample_rate, num_channels, reset
        self.messages.extend(messages)
        return np.ones((2, buffer_size), dtype=np.float32)


class FakeStream:
    def __init__(self, block_seconds: float) -> None:
        self.block_seconds = block_seconds
        self.blocks: list[np.ndarray] = []

    def __enter__(self):
        return self

    def write(self, block, sample_rate) -> None:
        _ = sample_rate
        self.blocks.append(block.copy())
        time.sleep(self.block_seconds)

    def close(self) -> None:
        pass


def test_zone_mixer_opens_one_stream_and_sums_isolated_patch_blocks(tmp_path: Path) -> None:
    configured = zone(tmp_path)
    stream = FakeStream(configured.block_size / configured.sample_rate)

    class PatchWorker:
        def __init__(self, level: float) -> None:
            self.level = level
            self.audio_blocks: queue.Queue = queue.Queue()
            self.origins: list[float] = []
            self.rebased_origins: list[float] = []
            self.prime_barriers: list[str] = []

        def prime(self) -> str:
            barrier = f"prime-{self.level}"
            self.prime_barriers.append(barrier)
            return barrier

        def wait_for_prime(self, barrier_id: str, *, timeout_seconds: float) -> bool:
            assert timeout_seconds > 0
            return barrier_id in self.prime_barriers

        def activate(self, *, origin: float) -> None:
            self.origins.append(origin)
            self.audio_blocks.put(
                (
                    0,
                    np.full((2, configured.block_size), self.level, dtype=np.float32),
                )
            )

        def rebase(self, *, origin: float) -> None:
            self.rebased_origins.append(origin)

    workers = (PatchWorker(0.2), PatchWorker(0.3))
    mixer = LiveVstZoneMixer(
        configured,
        workers,  # type: ignore[arg-type]
        trace_sink=MemoryTraceSink(),
        stream_factory=lambda _zone: stream,
    )
    mixer.close()

    assert workers[0].origins == workers[1].origins
    assert workers[0].rebased_origins == workers[1].rebased_origins
    assert any(np.allclose(block, 0.5) for block in stream.blocks)


def test_worker_streams_live_blocks_and_emits_timing_telemetry(tmp_path: Path) -> None:
    configured = zone(tmp_path).model_copy(update={"instruments": (zone(tmp_path).instruments[0],)})
    plugin = FakePlugin()
    stream = FakeStream(configured.block_size / configured.sample_rate)
    commands: queue.Queue = queue.Queue()
    transport: queue.Queue = queue.Queue(maxsize=1)
    responses: queue.Queue = queue.Queue()
    rendered_until = SimpleNamespace(value=0.0)
    thread = threading.Thread(
        target=_worker_main,
        args=(configured, policy(), commands, transport, responses, rendered_until),
        kwargs={
            "plugin_loader": lambda _zone: {"orchestra": plugin},
            "stream_factory": lambda _zone: stream,
        },
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 2
    startup_states = []
    while True:
        kind, payload = responses.get(timeout=1)
        if kind == "ready":
            break
        if kind == "startup":
            startup_states.append(payload["state"])
            continue
        assert kind == "trace"
        assert AudioWorkerTrace.model_validate(payload).action in {"startup", "ready"}
    assert startup_states == ["loading", "opening_audio"]
    now = time.monotonic()
    transport.put((now, 960))
    commands.put(
        (
            "note",
            {
                "event_id": "note-1",
                "part_id": "horn",
                "pitch": 64,
                "velocity": 90,
                "target_acoustic_time": now + 0.25,
                "duration_seconds": 0.1,
                "committed_at": now,
            },
        )
    )
    commands.put(("volume", 0.5))
    traces = []
    midi_rows = []
    while time.monotonic() < deadline and not any(
        AudioWorkerTrace.model_validate(item).action == "window" for item in traces
    ):
        try:
            kind, payload = responses.get(timeout=0.2)
        except queue.Empty:
            continue
        if kind == "trace":
            traces.append(payload)
        elif kind == "midi_output":
            midi_rows.append(MidiOutputTrace.model_validate(payload))
    commands.put(("panic", {"reason": "test", "barrier_id": "panic-1"}))
    deadline = time.monotonic() + 1
    panic_completed = False
    while time.monotonic() < deadline and not panic_completed:
        try:
            kind, payload = responses.get(timeout=0.2)
        except queue.Empty:
            continue
        if kind == "panic_complete" and payload == "panic-1":
            panic_completed = True
        elif kind == "midi_output":
            midi_rows.append(MidiOutputTrace.model_validate(payload))
    commands.put(("stop", None))
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert plugin.messages
    assert any(bytes_[:1] for bytes_, _offset in plugin.messages)
    windows = [
        AudioWorkerTrace.model_validate(item)
        for item in traces
        if AudioWorkerTrace.model_validate(item).action == "window"
    ]
    assert windows
    assert windows[0].blocks > 0
    assert windows[0].render_ms_p95 is not None
    assert windows[0].render_utilization_p95 is not None
    assert stream.blocks
    assert [row.action for row in midi_rows if row.event_id == "note-1"] == [
        "note_on",
        "note_off",
    ]
    assert any(
        row.action == "master_volume" and row.master_volume == pytest.approx(0.25)
        for row in midi_rows
    )
    assert panic_completed


def test_worker_reports_structured_startup_failure(tmp_path: Path) -> None:
    """The parent receives the stage and traceback instead of an opaque exit."""

    configured = zone(tmp_path).model_copy(
        update={"instruments": (zone(tmp_path).instruments[0],)}
    )
    commands: queue.Queue = queue.Queue()
    transport: queue.Queue = queue.Queue(maxsize=1)
    responses: queue.Queue = queue.Queue()
    rendered_until = SimpleNamespace(value=0.0)

    def fail_loader(_zone):
        raise ValueError("No such device: LG TV SSCR2")

    _worker_main(
        configured,
        policy(),
        commands,
        transport,
        responses,
        rendered_until,
        plugin_loader=fail_loader,
        stream_factory=lambda _zone: FakeStream(0.01),
    )

    messages = []
    while not responses.empty():
        messages.append(responses.get_nowait())
    errors = [payload for kind, payload in messages if kind == "error"]
    assert len(errors) == 1
    assert errors[0]["error_type"] == "ValueError"
    assert errors[0]["detail"] == "No such device: LG TV SSCR2"
    assert errors[0]["stage"] == "loading_plugins"
    assert "fail_loader" in errors[0]["traceback"]
    startup_traces = [
        AudioWorkerTrace.model_validate(payload)
        for kind, payload in messages
        if kind == "trace"
    ]
    error_trace = next(row for row in startup_traces if row.action == "error")
    assert error_trace.startup_stage == "loading_plugins"
    assert error_trace.error_type == "ValueError"
    assert error_trace.traceback is not None and "fail_loader" in error_trace.traceback


def test_worker_publishes_applied_gain_and_unit_signal_level(tmp_path: Path) -> None:
    configured = zone(tmp_path).model_copy(update={"instruments": (zone(tmp_path).instruments[0],)})
    plugin = FakePlugin()
    stream = FakeStream(configured.block_size / configured.sample_rate)
    commands: queue.Queue = queue.Queue()
    transport: queue.Queue = queue.Queue(maxsize=1)
    responses: queue.Queue = queue.Queue()
    rendered_until = SimpleNamespace(value=0.0)
    gauges = MixGaugeBlock(("orchestra",))
    thread = threading.Thread(
        target=_worker_main,
        args=(configured, policy(), commands, transport, responses, rendered_until),
        kwargs={
            "plugin_loader": lambda _zone: {"orchestra": plugin},
            "stream_factory": lambda _zone: stream,
            "mix_gauges": gauges,
        },
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and gauges.sample()[0].sequence == 0:
        time.sleep(0.01)
    sample = gauges.sample()[0]
    while time.monotonic() < deadline and not any(
        np.allclose(block, 0.5) for block in stream.blocks
    ):
        time.sleep(0.01)
    commands.put(("stop", None))
    thread.join(timeout=2)

    assert sample.sequence > 0
    assert sample.route_gain_start == pytest.approx(0.5)
    assert sample.route_gain_end == pytest.approx(0.5)
    assert sample.master_gain == pytest.approx(1.0)
    assert sample.effective_gain_end == pytest.approx(0.5)
    assert sample.output_rms == pytest.approx(0.5)
    assert sample.output_peak == pytest.approx(0.5)
    assert any(np.allclose(block, 0.5) for block in stream.blocks)


class FakeOutput:
    def __init__(self) -> None:
        self.calls = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return True

        return call


def test_multi_zone_output_fans_live_events_and_controls_to_both_paths() -> None:
    yamaha = FakeOutput()
    vst = FakeOutput()
    output = MultiZoneAccompanimentOutput(yamaha, vst)
    event = ScheduledAccompanimentEvent(
        event=ScoreEvent(
            event_id="e1",
            measure=1,
            beat=0,
            part_id="oboe",
            role="accompaniment",
            duration_beats=1,
            pitch=64,
            velocity=80,
        ),
        perf_time=10,
        section_mode=AccompanimentMode.FOLLOW,
        tempo_bpm=60,
    )

    output.send(event, sent_at=9.9)
    output.apply_mix_automation(960, sent_at=10)
    output.set_master_volume(0.8, sent_at=10)
    output.panic(sent_at=10.1, reason="test")

    assert [name for name, _args, _kwargs in yamaha.calls] == [
        "send",
        "apply_mix_automation",
        "set_master_volume",
        "panic",
    ]
    assert [name for name, _args, _kwargs in vst.calls] == [
        "send",
        "apply_mix_automation",
        "set_master_volume",
        "panic",
    ]


def _event() -> ScheduledAccompanimentEvent:
    return ScheduledAccompanimentEvent(
        event=ScoreEvent(
            event_id="e1",
            measure=1,
            beat=0,
            part_id="oboe",
            role="accompaniment",
            duration_beats=1,
            pitch=64,
            velocity=80,
        ),
        perf_time=10,
        section_mode=AccompanimentMode.FOLLOW,
        tempo_bpm=60,
    )


def test_multi_zone_output_runs_vst_only_when_yamaha_is_none() -> None:
    # No MIDI output chosen: everything flows to the live audio zones only, and
    # the missing Yamaha side is a no-op rather than a crash.
    vst = FakeOutput()
    output = MultiZoneAccompanimentOutput(None, vst)

    output.send(_event(), sent_at=9.9)
    output.apply_mix_automation(960, sent_at=10)
    output.set_master_volume(0.8, sent_at=10)
    output.set_output_advance(50)
    output.panic(sent_at=10.1, reason="test")
    assert output.cancel_pending(["e1"]) is True
    assert output.retime_release("e1", scheduled_at=10.2) is True
    output.close()

    assert [name for name, _args, _kwargs in vst.calls] == [
        "send",
        "apply_mix_automation",
        "set_master_volume",
        # The live output-advance knob now reaches the VST/room path too (so the
        # room latency can be compensated), not just the Yamaha side.
        "set_output_advance",
        "panic",
        "cancel_pending",
        "retime_release",
        "close",
    ]


def test_multi_zone_output_requires_at_least_one_sink() -> None:
    with pytest.raises(ValueError, match="needs a Yamaha or a VST output"):
        MultiZoneAccompanimentOutput(None, None)


def test_multizone_output_advance_reaches_both_yamaha_and_vst() -> None:
    """The live output-advance knob must reach the VST/REAPER path, not just Yamaha."""
    from aimusic.audio.live_vst import MultiZoneAccompanimentOutput

    class Recorder:
        def __init__(self) -> None:
            self.advance: float | None = None

        def set_output_advance(self, output_advance_ms: float) -> None:
            self.advance = output_advance_ms

    yamaha, vst = Recorder(), Recorder()
    out = MultiZoneAccompanimentOutput(yamaha, vst)  # type: ignore[arg-type]

    out.set_output_advance(80.0)

    assert yamaha.advance == 80.0
    assert vst.advance == 80.0  # previously dropped on the floor for VST/REAPER


def test_reaper_router_set_output_advance_updates_deadline_output() -> None:
    """ReaperMidiRouter must apply a live advance to its deadline output."""
    from aimusic.audio.live_reaper import ReaperMidiRouter

    class FakeDeadline:
        def __init__(self) -> None:
            self.advance: float | None = None

        def set_output_advance(self, output_advance_ms: float) -> None:
            self.advance = output_advance_ms

    router = ReaperMidiRouter.__new__(ReaperMidiRouter)  # bypass hardware __init__
    fake = FakeDeadline()
    router._output = fake  # type: ignore[attr-defined]

    router.set_output_advance(72.0)

    assert fake.advance == 72.0
