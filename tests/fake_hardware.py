"""Shared software MIDI fixtures standing in for `LiveControl`.

No test in this suite touches a real Yamaha or `python-rtmidi` port: every
test that exercises the record/play HTTP endpoints monkeypatches
`aimusic.server.routes.live_control` with `FakeLiveControl` below, which
completes "recordings" synchronously by writing a fixture MIDI file instead
of reading from real hardware. This is the one place that fake lived (three
near-identical copies existed in test_hardware_api.py, test_takes_api.py,
and test_capture_flow_e2e.py); it's centralized here so `make test`-style
runs never depend on a Yamaha being connected, and so a new fake doesn't
have to be hand-rolled per test module.
"""

from __future__ import annotations

from pathlib import Path

from mido import Message, MidiFile, MidiTrack

from aimusic.accompaniment.midi_ports import MidiPorts
from aimusic.accompaniment.rehearsal_position import score_projection
from aimusic.accompaniment.runtime_contracts import (
    RuntimeConfig,
    RuntimeScorePosition,
    RuntimeStatus,
)
from aimusic.core import paths
from aimusic.core.events import events
from aimusic.core.time import utc_now
from aimusic.server.live_control import LiveJobStatus
from aimusic.server.schemas import (
    HardwareStatusEvent,
    LivePerformancePlanResponse,
    LiveStatusResponse,
    OrchestraRendererStatus,
)

# Authored FOLLOW regions from Movement II's sections.json, in canonical beats.
# Keep the end exclusive, matching SectionMap.section_at: starting inside a
# FOLLOW region follows immediately, while starting at m.22 (beat 84) waits for
# the next FOLLOW region at m.23 (beat 88).
MOVEMENT_2_FOLLOW_REGIONS = (
    (47.0, 84.0),
    (88.0, 204.0),
    (211.0, 412.0),
    (417.0, float("inf")),
)


def movement_2_follow_beat(start_beat: float) -> float | None:
    """Return the first authored FOLLOW beat at or after ``start_beat``."""

    return next(
        (
            max(start_beat, region_start)
            for region_start, region_end in MOVEMENT_2_FOLLOW_REGIONS
            if region_end > start_beat
        ),
        None,
    )


def write_fixture_midi(path: Path, *, note_count: int = 2) -> None:
    """Write a tiny valid MIDI file with `note_count` note on/off pairs."""

    midi = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    midi.tracks.append(track)
    for pitch in range(60, 60 + note_count):
        track.append(Message("note_on", note=pitch, velocity=64, time=0))
        track.append(Message("note_off", note=pitch, velocity=0, time=240))
    path.parent.mkdir(parents=True, exist_ok=True)
    midi.save(path)


class FakeLiveControl:
    """Simulates hardware record/playback jobs that finish synchronously.

    Every `start_*` call writes its output (if any) immediately and reports
    `running=True`, matching the real `LiveControl`'s response shape without
    spinning up a background thread or touching a MIDI port.
    """

    def __init__(self, *, note_count: int = 2) -> None:
        self.current = LiveJobStatus(kind="idle", running=False, message="Idle")
        self.record_args: dict[str, object] | None = None
        self.play_args: dict[str, object] | None = None
        self.play_file_args: dict[str, object] | None = None
        self.managed_args: dict[str, object] | None = None
        self.panic_output: str | None = None
        self.stop_calls = 0
        self._note_count = note_count

    def status(self) -> LiveJobStatus:
        return self.current

    def midi_devices(self) -> MidiPorts:
        return MidiPorts(
            inputs=("Fake CLP-795GP USB",), outputs=("Fake Synth",), backend_available=True
        )

    def _set_status(self, status: LiveJobStatus) -> LiveJobStatus:
        self.current = status
        events.publish(
            HardwareStatusEvent(
                type="hardware:status",
                status=LiveStatusResponse(
                    phase=status.phase,
                    kind=status.kind,
                    running=status.running,
                    message=status.message,
                    started_at=status.started_at,
                    session_id=status.session_id,
                    score_transport=status.score_transport,
                ),
            )
        )
        return status

    def start_record(
        self, *, input_name: str, session_id: str, duration_seconds: float | None
    ) -> LiveJobStatus:
        self.record_args = {
            "input_name": input_name,
            "session_id": session_id,
            "duration_seconds": duration_seconds,
        }
        write_fixture_midi(paths.session_dir(session_id) / "solo.mid", note_count=self._note_count)
        return self._set_status(
            LiveJobStatus(
                kind="record",
                running=True,
                message=f"Recording from {input_name}",
                session_id=session_id,
            )
        )

    def start_play_oguri(self, **kwargs: object) -> LiveJobStatus:
        self.play_args = kwargs
        return self._set_status(
            LiveJobStatus(
                kind="playback",
                running=True,
                message=f"Playing movement {kwargs['movement']}",
            )
        )

    def start_play_midi_file(self, **kwargs: object) -> LiveJobStatus:
        self.play_file_args = kwargs
        return self._set_status(
            LiveJobStatus(
                kind="playback",
                running=True,
                message=f"Playing {kwargs['source_path'].name}",
                session_id=kwargs.get("session_id"),
            )
        )

    def start_managed(self, **kwargs: object) -> LiveJobStatus:
        self.managed_args = kwargs
        return self._set_status(
            LiveJobStatus(
                kind=str(kwargs["kind"]),
                running=True,
                message=str(kwargs["message"]),
                session_id=kwargs.get("session_id"),
            )
        )

    def attach_score_transport(self, score_transport) -> LiveJobStatus:
        return self._set_status(
            LiveJobStatus(
                kind=self.current.kind,
                running=self.current.running,
                message=self.current.message,
                started_at=self.current.started_at,
                session_id=self.current.session_id,
                score_transport=score_transport,
            )
        )

    def stop(self) -> LiveJobStatus:
        self.stop_calls += 1
        return self._set_status(
            LiveJobStatus(
                kind="idle", running=False, message="Idle", session_id=self.current.session_id
            )
        )

    def wait_until_idle(self, timeout: float = 10.0) -> LiveJobStatus:
        return self.current

    def panic(self, output_name: str) -> LiveJobStatus:
        self.panic_output = output_name
        return self._set_status(LiveJobStatus(kind="idle", running=False, message="Panic sent"))


class FakeLiveRuntime:
    """Boundary fake for rehearsal routes that now use the causal runtime."""

    def __init__(self, hardware: FakeLiveControl, *, note_count: int = 2) -> None:
        self.hardware = hardware
        self.note_count = note_count
        self.start_args: dict[str, object] | None = None
        self.projection = score_projection("chopin_op11", 2)

    def _position(self, score_beat: float) -> RuntimeScorePosition:
        ppq = self.projection.timeline.document.canonical_ppq
        source_tick = self.projection.source_tick_at_score_tick(round(score_beat * ppq))
        position = self.projection.position_at_source_tick(source_tick)
        return RuntimeScorePosition(
            **position.__dict__,
            mapping_id=self.projection.mapping_id,
            mapping_review_state=self.projection.mapping_review_state.value,
            canonical_position=self.projection.canonical_position,
        )

    def performance_plan(
        self,
        *,
        bundle_id: str,
        revision: str | None,
        start_measure: int | None = None,
    ) -> LivePerformancePlanResponse:
        _ = revision
        explicit_start = start_measure is not None
        start_measure = 1 if start_measure is None else start_measure
        start_tick = self.projection.timeline.tick_at(start_measure - 1)
        start_beat = start_tick / self.projection.timeline.document.canonical_ppq
        follow_beat = movement_2_follow_beat(start_beat)
        return LivePerformancePlanResponse(
            bundle_id=bundle_id,
            orchestra_starts_automatically=(
                explicit_start or (follow_beat is not None and follow_beat > start_beat)
            ),
            orchestra_start=self._position(start_beat),
            first_solo_entry=self._position(47.0),
            follow_start=None if follow_beat is None else self._position(follow_beat),
            follow_prior_take_count=6,
            initial_tempo_bpm=64.0,
            tempo_source="performance_profile",
            rehearsal_take_count=8,
        )

    def start_follow(self, **kwargs: object) -> RuntimeStatus:
        self.start_args = kwargs
        config = kwargs["config"]
        assert isinstance(config, RuntimeConfig)
        write_fixture_midi(
            paths.session_dir(config.run_id) / "solo.mid",
            note_count=self.note_count,
        )
        self.hardware._set_status(
            LiveJobStatus(
                kind="live_follow",
                running=True,
                message="Following Fake CLP-795GP USB",
                started_at=utc_now(),
                session_id=config.run_id,
            )
        )
        return RuntimeStatus(
            run_id=config.run_id,
            run_mode=config.run_mode,
            phase="preparing",
            state_word="Listening",
            monotonic_time=0.0,
            orchestra_tempo_bpm=config.initial_tempo_bpm,
            orchestra_volume=config.orchestra_volume,
            message="Preparing the live follower",
        )

    def renderer_status(self) -> OrchestraRendererStatus:
        return OrchestraRendererStatus(
            backend_kind="yamaha_midi",
            state="ready",
            renderer_id="yamaha_midi",
            device_name="Fake MIDI Keyboard",
            buffer_size_samples=0,
            sample_rate_hz=44100,
            preload_id="fake-preload-id",
            progress_percent=100.0,
            detail="Keyboard MIDI output is ready",
        )

    def renderer_status_for_client(self) -> OrchestraRendererStatus:
        return self.renderer_status()
