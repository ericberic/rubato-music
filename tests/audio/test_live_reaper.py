from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import mido
import pytest

from aimusic.accompaniment.runtime_io import MemoryTraceSink
from aimusic.audio.live_config import LiveVstZoneConfig, VstInstrumentBinding
from aimusic.audio.live_reaper import (
    _STABLE_MIDI_UNIQUE_ID,
    ReaperHeartbeat,
    ReaperMidiRouter,
    _pin_coremidi_unique_id,
)
from aimusic.audio.live_vst import LiveVstError


class FakePort:
    def __init__(self) -> None:
        self.messages: list[mido.Message] = []
        self.closed = False

    def send(self, message: mido.Message) -> None:
        self.messages.append(message.copy())

    def close(self) -> None:
        self.closed = True


def zone(tmp_path: Path) -> LiveVstZoneConfig:
    plugin = tmp_path / "BBCSO.vst3"
    plugin.mkdir()
    state = tmp_path / "violins.state"
    state.write_bytes(b"state")
    app = tmp_path / "REAPER.app"
    app.mkdir()
    project = tmp_path / "Rubato Orchestra.RPP"
    project.write_text("<REAPER_PROJECT\n>\n")
    return LiveVstZoneConfig(
        renderer="reaper",
        zone_id="room_center",
        label="REAPER center",
        output_device_name="LG TV SSCR2",
        acoustic_position="front wall",
        configured_output_advance_ms=0,
        residual_error_p95_ms=0,
        calibration_revision="reaper-test",
        midi_port_name="Rubato Orchestra",
        reaper_app_path=app,
        reaper_project_path=project,
        reaper_heartbeat_path=tmp_path / "renderer-status.json",
        block_size=128,
        instruments=(
            VstInstrumentBinding(
                instrument_id="violins",
                stem_ids=("violins_1", "violins_2"),
                plugin_path=plugin,
                plugin_state_path=state,
                midi_channel=0,
            ),
        ),
    )


def heartbeat(*, state: str = "ready", output: str = "LG TV SSCR2") -> ReaperHeartbeat:
    return ReaperHeartbeat(
        updated_at_epoch=100.0,
        state=state,
        message="REAPER ready" if state == "ready" else "setup required",
        project_path="/tmp/Rubato Orchestra.RPP",
        midi_input_name="Rubato Orchestra",
        audio_output_name=output,
        sample_rate=48_000,
        block_size=128,
        output_latency_samples=128,
        ready_tracks=1,
        total_tracks=1,
    )


def policy() -> SimpleNamespace:
    return SimpleNamespace(audio_gain_for_part_at=lambda _zone, _part, _tick: 1.0)


def test_reaper_router_opens_virtual_port_and_reports_ready(tmp_path: Path) -> None:
    port = FakePort()
    progress = []
    sink = MemoryTraceSink()
    router = ReaperMidiRouter(
        zone(tmp_path),
        policy(),  # type: ignore[arg-type]
        trace_sink=sink,
        startup_observer=progress.append,
        port_factory=lambda name: port,
        heartbeat_loader=lambda path: heartbeat(),
        wall_time=lambda: 100.5,
        midi_probe_timeout_seconds=0,
    )

    assert router.is_alive
    assert progress[-1]["state"] == "ready"
    assert progress[-1]["loaded"] == 1
    assert any(
        message.type == "control_change" and message.control == 7 for message in port.messages
    )

    router.panic_and_wait(sent_at=time.monotonic(), reason="test")
    assert any(
        message.type == "control_change" and message.control == 123 for message in port.messages
    )
    router.close()
    assert port.closed


def test_reaper_router_rejects_wrong_audio_output(tmp_path: Path) -> None:
    with pytest.raises(LiveVstError, match="expected LG TV SSCR2"):
        ReaperMidiRouter(
            zone(tmp_path),
            policy(),  # type: ignore[arg-type]
            trace_sink=MemoryTraceSink(),
            port_factory=lambda name: FakePort(),
            heartbeat_loader=lambda path: heartbeat(output="MacBook Air Speakers"),
            startup_timeout_seconds=0.01,
            wall_time=lambda: 100.5,
            midi_probe_timeout_seconds=0,
        )


def test_reaper_router_rejects_stale_heartbeat(tmp_path: Path) -> None:
    with pytest.raises(LiveVstError, match="heartbeat is stale"):
        ReaperMidiRouter(
            zone(tmp_path),
            policy(),  # type: ignore[arg-type]
            trace_sink=MemoryTraceSink(),
            port_factory=lambda name: FakePort(),
            heartbeat_loader=lambda path: heartbeat(),
            startup_timeout_seconds=0.01,
            wall_time=lambda: 105.0,
            midi_probe_timeout_seconds=0,
        )


@pytest.mark.parametrize(
    ("sample_rate", "block_size", "message"),
    [
        (44_100, 128, "sample rate is 44100"),
        (48_000, 512, "block size is 512"),
    ],
)
def test_reaper_router_rejects_high_latency_audio_settings(
    tmp_path: Path, sample_rate: float, block_size: int, message: str
) -> None:
    observed = heartbeat().model_copy(
        update={"sample_rate": sample_rate, "block_size": block_size}
    )
    with pytest.raises(LiveVstError, match=message):
        ReaperMidiRouter(
            zone(tmp_path),
            policy(),  # type: ignore[arg-type]
            trace_sink=MemoryTraceSink(),
            port_factory=lambda name: FakePort(),
            heartbeat_loader=lambda path: observed,
            startup_timeout_seconds=0.01,
            wall_time=lambda: 100.5,
            midi_probe_timeout_seconds=0,
        )


def test_reaper_router_resolves_coreaudio_default_to_the_real_device(tmp_path: Path) -> None:
    port = FakePort()
    default_heartbeat = heartbeat(output="CoreAudio Default")
    router = ReaperMidiRouter(
        zone(tmp_path),
        policy(),  # type: ignore[arg-type]
        trace_sink=MemoryTraceSink(),
        port_factory=lambda name: port,
        heartbeat_loader=lambda path: default_heartbeat,
        audio_output_resolver=lambda: "LG TV SSCR2",
        wall_time=lambda: 100.5,
        midi_probe_timeout_seconds=0,
    )

    assert router.is_alive
    router.close()


def test_reaper_router_requires_a_round_trip_midi_ingress_probe(tmp_path: Path) -> None:
    port = FakePort()

    def observed(_path: Path) -> ReaperHeartbeat:
        if any(
            message.type == "control_change"
            and message.channel == 15
            and message.control == 119
            and message.value == 17
            for message in port.messages
        ):
            return heartbeat().model_copy(
                update={
                    "midi_event_sequence": 42,
                    "midi_event_status": 0xBF,
                    "midi_event_data1": 119,
                    "midi_event_data2": 17,
                }
            )
        return heartbeat()

    router = ReaperMidiRouter(
        zone(tmp_path),
        policy(),  # type: ignore[arg-type]
        trace_sink=MemoryTraceSink(),
        port_factory=lambda name: port,
        heartbeat_loader=observed,
        wall_time=lambda: 100.5,
    )

    assert router.is_alive
    assert any(
        message.type == "control_change" and message.channel == 15 and message.control == 119
        for message in port.messages
    )
    router.close()


def test_reaper_router_resends_probe_until_reaper_latches(tmp_path: Path) -> None:
    """REAPER may bind the live endpoint a beat after the source opens.

    A single probe can land before the bridge latches the live same-name device
    row; the router must keep probing across the window so a later heartbeat can
    still observe it.
    """

    port = FakePort()

    def observed(_path: Path) -> ReaperHeartbeat:
        probes = [
            message
            for message in port.messages
            if message.type == "control_change"
            and message.channel == 15
            and message.control == 119
        ]
        if len(probes) >= 2:
            return heartbeat().model_copy(
                update={
                    "midi_event_sequence": 7,
                    "midi_event_status": 0xBF,
                    "midi_event_data1": 119,
                    "midi_event_data2": 17,
                }
            )
        return heartbeat()

    router = ReaperMidiRouter(
        zone(tmp_path),
        policy(),  # type: ignore[arg-type]
        trace_sink=MemoryTraceSink(),
        port_factory=lambda name: port,
        heartbeat_loader=observed,
        wall_time=lambda: 100.5,
        midi_probe_timeout_seconds=1.5,
    )

    probes = [
        message
        for message in port.messages
        if message.type == "control_change"
        and message.channel == 15
        and message.control == 119
    ]
    assert len(probes) >= 2
    assert router.is_alive
    router.close()


def test_pin_coremidi_unique_id_is_best_effort_when_source_absent() -> None:
    # No live CoreMIDI source with this name exists in the test environment, so
    # the helper must fail closed (return False) rather than raise, on macOS or
    # any other platform.
    assert _pin_coremidi_unique_id("nonexistent-rubato-source-xyz") is False


def test_stable_midi_unique_id_fits_uint32() -> None:
    # REAPER remembers enabled input rows by unique ID; the constant must be a
    # stable positive 32-bit value so the same row is reused across launches.
    assert 0 < _STABLE_MIDI_UNIQUE_ID < 2**31


def test_reaper_router_fails_when_probe_is_never_observed(tmp_path: Path) -> None:
    port = FakePort()
    with pytest.raises(LiveVstError, match="did not receive its MIDI probe"):
        ReaperMidiRouter(
            zone(tmp_path),
            policy(),  # type: ignore[arg-type]
            trace_sink=MemoryTraceSink(),
            port_factory=lambda name: port,
            # Ready, but the heartbeat never reports the probe bytes.
            heartbeat_loader=lambda path: heartbeat(),
            wall_time=lambda: 100.5,
            midi_probe_timeout_seconds=0.6,
        )
    probes = [
        message
        for message in port.messages
        if message.type == "control_change"
        and message.channel == 15
        and message.control == 119
    ]
    assert len(probes) >= 2
