"""Threaded live MIDI control used by the local rehearsal API."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from mido import MidiFile

from aimusic.accompaniment.live_midi import (
    MAX_TEMPO_SCALE,
    MIN_TEMPO_SCALE,
    play_midi,
    record_midi,
    send_panic,
)
from aimusic.accompaniment.midi_ports import list_midi_ports
from aimusic.accompaniment.oguri import (
    DEFAULT_ORCHESTRA_VOLUME,
    PIANO_TRACK_MARKER,
    oguri_movement,
)
from aimusic.accompaniment.rehearsal_position import SOURCE_TICKS_PER_SECOND, score_projection
from aimusic.core import paths, recordings
from aimusic.core.event_journal import journal_exception
from aimusic.core.events import events
from aimusic.core.time import utc_now
from aimusic.server.schemas import (
    HardwareJobPhase,
    HardwareStatusEvent,
    LiveStatusResponse,
    ScorePositionResponse,
    ScoreTransportAnchorResponse,
    ScoreTransportResponse,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, init=False)
class LiveJobStatus:
    phase: HardwareJobPhase
    kind: str
    running: bool
    message: str
    started_at: datetime | None = None
    session_id: str | None = None
    score_transport: ScoreTransportResponse | None = None

    def __init__(
        self,
        kind: str,
        running: bool,
        message: str,
        started_at: datetime | None = None,
        session_id: str | None = None,
        phase: HardwareJobPhase | None = None,
        score_transport: ScoreTransportResponse | None = None,
    ) -> None:
        """Build a status while keeping legacy test/adapter construction valid.

        Production transitions always pass ``phase`` explicitly. The inference
        exists only for older in-process fakes that construct the compatibility
        ``kind``/``running`` shape directly.
        """

        if phase is None:
            if kind == "error":
                phase = HardwareJobPhase.FAILED
            elif kind == "stopping":
                phase = HardwareJobPhase.STOPPING
            elif running:
                phase = HardwareJobPhase.RUNNING
            else:
                phase = HardwareJobPhase.IDLE
        expected_running = phase in {HardwareJobPhase.RUNNING, HardwareJobPhase.STOPPING}
        if running is not expected_running:
            raise ValueError(f"Hardware phase {phase.value} requires running={expected_running}")
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "running", running)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "score_transport", score_transport)


_LEGAL_PHASE_TRANSITIONS: dict[HardwareJobPhase, frozenset[HardwareJobPhase]] = {
    HardwareJobPhase.IDLE: frozenset(
        {HardwareJobPhase.RUNNING, HardwareJobPhase.COMPLETED, HardwareJobPhase.FAILED}
    ),
    HardwareJobPhase.RUNNING: frozenset(
        {
            HardwareJobPhase.STOPPING,
            HardwareJobPhase.COMPLETED,
            HardwareJobPhase.FAILED,
        }
    ),
    HardwareJobPhase.STOPPING: frozenset({HardwareJobPhase.COMPLETED, HardwareJobPhase.FAILED}),
    HardwareJobPhase.COMPLETED: frozenset(
        {HardwareJobPhase.IDLE, HardwareJobPhase.RUNNING, HardwareJobPhase.FAILED}
    ),
    HardwareJobPhase.FAILED: frozenset(
        {HardwareJobPhase.IDLE, HardwareJobPhase.RUNNING, HardwareJobPhase.COMPLETED}
    ),
}


class LiveControl:
    """Single-process live MIDI job manager.

    The MVP server supports one hardware job at a time. That is deliberate:
    Yamaha input/output is a shared physical resource, and overlapping playback
    and recording would make debugging ambiguous.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._status = LiveJobStatus(
            phase=HardwareJobPhase.IDLE,
            kind="idle",
            running=False,
            message="Idle",
        )

    def status(self) -> LiveJobStatus:
        with self._lock:
            self._reap_finished_locked()
            return self._status

    def midi_devices(self):
        return list_midi_ports()

    def start_record(
        self,
        *,
        input_name: str,
        session_id: str,
        duration_seconds: float | None,
    ) -> LiveJobStatus:
        with self._lock:
            self._require_idle_locked()
            stop_event = threading.Event()
            self._stop_event = stop_event
            started_at = utc_now()
            output_path = paths.run_input_dir(session_id) / "solo.mid"
            paths.session_dir(session_id)
            status = self._transition_locked(
                phase=HardwareJobPhase.RUNNING,
                kind="record",
                running=True,
                started_at=started_at,
                session_id=session_id,
                message=f"Recording from {input_name}",
            )
            self._thread = threading.Thread(
                target=self._record_worker,
                kwargs={
                    "input_name": input_name,
                    "session_id": session_id,
                    "output_path": output_path,
                    "duration_seconds": duration_seconds,
                    "stop_event": stop_event,
                },
                daemon=True,
            )
            self._thread.start()
            return status

    def start_play_oguri(
        self,
        *,
        movement: int,
        output_name: str,
        volume: float = DEFAULT_ORCHESTRA_VOLUME,
        tempo_scale: float = 1.0,
        duration_seconds: float | None,
        orchestra_only: bool = True,
    ) -> LiveJobStatus:
        with self._lock:
            self._require_idle_locked()
            stop_event = threading.Event()
            self._stop_event = stop_event
            started_at = utc_now()
            source = oguri_movement(movement)
            skip = (PIANO_TRACK_MARKER,) if orchestra_only else ()
            playback_duration_seconds = (
                duration_seconds * tempo_scale
                if duration_seconds is not None
                else max(
                    0.0,
                    MidiFile(source.local_path, clip=True).length
                    - source.first_orchestra_entry_seconds,
                )
            )
            status = self._transition_locked(
                phase=HardwareJobPhase.RUNNING,
                kind="playback",
                running=True,
                started_at=started_at,
                message=f"Playing Oguri movement {movement} to {output_name}",
                score_transport=_score_transport_for_source_window(
                    piece_id="chopin_op11",
                    movement=movement,
                    start_seconds=source.first_orchestra_entry_seconds,
                    duration_seconds=playback_duration_seconds,
                    tempo_scale=tempo_scale,
                ),
            )
            self._thread = threading.Thread(
                target=self._play_worker,
                kwargs={
                    "source_path": source.local_path,
                    "output_name": output_name,
                    "volume": volume,
                    "tempo_scale": tempo_scale,
                    "duration_seconds": playback_duration_seconds,
                    "start_seconds": source.first_orchestra_entry_seconds,
                    "skip": skip,
                    "stop_event": stop_event,
                },
                daemon=True,
            )
            self._thread.start()
            return status

    def start_play_midi_file(
        self,
        *,
        source_path: Path,
        output_name: str,
        volume: float,
        duration_seconds: float | None,
        start_seconds: float = 0.0,
        session_id: str | None = None,
    ) -> LiveJobStatus:
        with self._lock:
            self._require_idle_locked()
            stop_event = threading.Event()
            self._stop_event = stop_event
            started_at = utc_now()
            status = self._transition_locked(
                phase=HardwareJobPhase.RUNNING,
                kind="playback",
                running=True,
                started_at=started_at,
                session_id=session_id,
                message=f"Playing {source_path.name} to {output_name}",
            )
            self._thread = threading.Thread(
                target=self._play_worker,
                kwargs={
                    "source_path": source_path,
                    "output_name": output_name,
                    "volume": volume,
                    "tempo_scale": 1.0,
                    "duration_seconds": duration_seconds,
                    "start_seconds": start_seconds,
                    "skip": (),
                    "stop_event": stop_event,
                },
                daemon=True,
            )
            self._thread.start()
            return status

    def start_managed(
        self,
        *,
        kind: str,
        message: str,
        target: Callable[[threading.Event], str | None],
        session_id: str | None = None,
    ) -> LiveJobStatus:
        """Run another Yamaha workflow under the shared hardware-job lock."""

        with self._lock:
            self._require_idle_locked()
            stop_event = threading.Event()
            self._stop_event = stop_event
            status = self._transition_locked(
                phase=HardwareJobPhase.RUNNING,
                kind=kind,
                running=True,
                started_at=utc_now(),
                session_id=session_id,
                message=message,
            )
            self._thread = threading.Thread(
                target=self._managed_worker,
                kwargs={
                    "target": target,
                    "stop_event": stop_event,
                    "session_id": session_id,
                },
                daemon=True,
            )
            self._thread.start()
            return status

    def attach_score_transport(self, score_transport: ScoreTransportResponse) -> LiveJobStatus:
        """Attach score-time anchors to the currently running hardware job.

        Review playback knows its alignment anchors at the API boundary, while
        the shared MIDI-file player intentionally knows nothing about takes.
        Attaching immediately after start keeps that separation and publishes
        an authoritative updated hardware status for the cockpit.
        """

        with self._lock:
            self._reap_finished_locked()
            if self._status.phase is not HardwareJobPhase.RUNNING:
                raise RuntimeError("No running hardware job to attach score transport")
            return self._transition_locked(
                phase=self._status.phase,
                kind=self._status.kind,
                running=self._status.running,
                message=self._status.message,
                started_at=self._status.started_at,
                session_id=self._status.session_id,
                score_transport=score_transport,
            )

    def stop(self) -> LiveJobStatus:
        with self._lock:
            self._reap_finished_locked()
            if self._thread is None or not self._thread.is_alive():
                return self._transition_locked(
                    phase=HardwareJobPhase.IDLE,
                    kind="idle",
                    running=False,
                    message="Idle",
                )
            if self._stop_event is not None:
                self._stop_event.set()
            return self._transition_locked(
                phase=HardwareJobPhase.STOPPING,
                kind="stopping",
                running=True,
                message="Stopping",
                started_at=self._status.started_at,
                session_id=self._status.session_id,
                score_transport=self._status.score_transport,
            )

    def wait_until_idle(self, timeout: float = 10.0) -> LiveJobStatus:
        """Block until the current job (if any) finishes, then return its status.

        Callers that need the job's output file to exist before proceeding
        (e.g. the take-store stop endpoint registering a finished recording)
        should use this instead of the fire-and-forget `stop()`.
        """
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        with self._lock:
            self._reap_finished_locked()
            return self._status

    def panic(self, output_name: str) -> LiveJobStatus:
        import mido

        panic_exc: Exception | None = None
        try:
            with mido.open_output(output_name) as out:
                send_panic(out)
        except Exception as exc:  # pragma: no cover - exercised by hardware smoke
            panic_exc = exc
            self._journal_worker_failure("hardware:panic_failed", exc, output_name=output_name)

        self.stop()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
        with self._lock:
            self._reap_finished_locked()
            if self._thread is not None and self._thread.is_alive():
                return self._transition_locked(
                    phase=HardwareJobPhase.STOPPING,
                    kind="stopping",
                    running=True,
                    message="Panic sent but background thread failed to stop",
                )
            elif panic_exc is not None:
                return self._transition_locked(
                    phase=HardwareJobPhase.FAILED,
                    kind="error",
                    running=False,
                    message=f"Panic failed to send: {panic_exc}",
                )
            else:
                return self._transition_locked(
                    phase=HardwareJobPhase.COMPLETED,
                    kind="idle",
                    running=False,
                    message="Panic sent",
                )

    def _record_worker(
        self,
        *,
        input_name: str,
        session_id: str,
        output_path: Path,
        duration_seconds: float | None,
        stop_event: threading.Event,
    ) -> None:
        try:
            summary = record_midi(
                input_name,
                output_path,
                duration_seconds=duration_seconds,
                stop_event=stop_event,
            )
            self._persist_recording_outputs(session_id, summary.path)
            message = (
                f"Recorded {summary.message_count} MIDI messages "
                f"over {summary.duration_seconds:.2f}s"
            )
            self._finish(HardwareJobPhase.COMPLETED, message, session_id=session_id)
        except Exception as exc:  # pragma: no cover - exercised by hardware smoke
            self._journal_worker_failure(
                "hardware:record_failed",
                exc,
                session_id=session_id,
                input_name=input_name,
                output_path=output_path,
            )
            self._finish(
                HardwareJobPhase.FAILED,
                f"Recording failed: {exc}",
                session_id=session_id,
            )

    def _play_worker(
        self,
        *,
        source_path: Path,
        output_name: str,
        volume: float,
        tempo_scale: float,
        duration_seconds: float | None,
        start_seconds: float,
        skip: tuple[str, ...],
        stop_event: threading.Event,
    ) -> None:
        try:
            summary = play_midi(
                source_path,
                output_name,
                volume=volume,
                tempo_scale=tempo_scale,
                max_seconds=duration_seconds,
                start_seconds=start_seconds,
                skip_track_name_contains=skip,
                stop_event=stop_event,
            )
            message = (
                f"Played {summary.event_count} MIDI events at volume "
                f"{summary.volume:.2f} and {summary.tempo_scale:.2f}x tempo"
            )
            self._finish(HardwareJobPhase.COMPLETED, message)
        except Exception as exc:  # pragma: no cover - exercised by hardware smoke
            self._journal_worker_failure(
                "hardware:playback_failed",
                exc,
                source_path=source_path,
                output_name=output_name,
            )
            self._finish(HardwareJobPhase.FAILED, f"Playback failed: {exc}")

    def _managed_worker(
        self,
        *,
        target: Callable[[threading.Event], str | None],
        stop_event: threading.Event,
        session_id: str | None,
    ) -> None:
        try:
            message = target(stop_event) or "Live MIDI job completed"
            self._finish(HardwareJobPhase.COMPLETED, message, session_id=session_id)
        except Exception as exc:  # pragma: no cover - device failures need hardware
            self._journal_worker_failure(
                "hardware:managed_job_failed",
                exc,
                session_id=session_id,
            )
            self._finish(
                HardwareJobPhase.FAILED,
                f"Live MIDI job failed: {exc}",
                session_id=session_id,
            )

    @staticmethod
    def _journal_worker_failure(event_type: str, exc: Exception, **context: object) -> None:
        LOGGER.exception("%s: %s", event_type, exc)
        journal_exception(event_type, exc, **context)

    def _persist_recording_outputs(
        self,
        session_id: str,
        source_path: Path,
        *,
        metadata: dict[str, object] | None = None,
    ) -> None:
        processed_dir = paths.session_dir(session_id)
        processed_path = processed_dir / "solo.mid"
        processed_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path = paths.recording_file_path(session_id)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        midi_data = source_path.read_bytes()
        processed_path.write_bytes(midi_data)
        raw_path.write_bytes(midi_data)
        if metadata is not None:
            metadata_path = processed_dir / "recording_metadata.json"
            metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
        if recordings.get_recording(session_id) is None:
            recordings.register_recording(session_id, source_path.name)

    def _finish(
        self,
        phase: HardwareJobPhase,
        message: str,
        *,
        session_id: str | None = None,
    ) -> None:
        with self._lock:
            self._stop_event = None
            self._thread = None
            self._transition_locked(
                phase=phase,
                kind="idle" if phase is HardwareJobPhase.COMPLETED else "error",
                running=False,
                message=message,
                session_id=session_id,
            )

    def on_playback_ready(self) -> None:
        """Playback lifecycle: setup is done and the first note is about to sound.

        The job status is created when the run is *requested*, but parsing the
        source MIDI, building the cue events and opening both ports take a few
        hundred milliseconds. The score cursor counts elapsed time from the
        status origin, so leaving it at request time puts the cursor permanently
        that far ahead of the music -- and since a fixed time offset is worth
        more beats wherever the music moves faster, it reads as a drift that
        accelerates rather than a constant lead.

        Re-stamping the origin here is what keeps the cursor and the sound
        talking about the same instant.
        """

        with self._lock:
            current = self._status
            if current is None or not current.running:
                return
            self._transition_locked(
                phase=current.phase,
                kind=current.kind,
                running=True,
                message=current.message,
                started_at=utc_now(),
                session_id=current.session_id,
                score_transport=current.score_transport,
            )

    def _transition_locked(
        self,
        *,
        phase: HardwareJobPhase,
        kind: str,
        running: bool,
        message: str,
        started_at: datetime | None = None,
        session_id: str | None = None,
        score_transport: ScoreTransportResponse | None = None,
    ) -> LiveJobStatus:
        """Apply one legal lifecycle transition and publish it exactly once.

        Caller must hold ``_lock``. Repeating an identical status is an
        idempotent no-op; any other real change is broadcast to the websocket
        event stream after the state has become authoritative.
        """

        next_status = LiveJobStatus(
            phase=phase,
            kind=kind,
            running=running,
            message=message,
            started_at=started_at,
            session_id=session_id,
            score_transport=score_transport,
        )
        if next_status == self._status:
            return self._status

        current_phase = self._status.phase
        if phase is not current_phase and phase not in _LEGAL_PHASE_TRANSITIONS[current_phase]:
            raise RuntimeError(
                f"Illegal hardware job transition: {current_phase.value} -> {phase.value}"
            )

        self._status = next_status
        events.publish(
            HardwareStatusEvent(
                type="hardware:status",
                status=LiveStatusResponse(
                    phase=next_status.phase,
                    kind=next_status.kind,
                    running=next_status.running,
                    message=next_status.message,
                    started_at=next_status.started_at,
                    session_id=next_status.session_id,
                    score_transport=next_status.score_transport,
                ),
            )
        )
        return next_status

    def _require_idle_locked(self) -> None:
        self._reap_finished_locked()
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError(f"Live MIDI job already running: {self._status.kind}")

    def _reap_finished_locked(self) -> None:
        if self._thread is not None and not self._thread.is_alive():
            self._thread = None
            self._stop_event = None


live_control = LiveControl()


def _score_transport_for_source_window(
    *,
    piece_id: str,
    movement: int,
    start_seconds: float,
    duration_seconds: float,
    tempo_scale: float = 1.0,
) -> ScoreTransportResponse:
    if not MIN_TEMPO_SCALE <= tempo_scale <= MAX_TEMPO_SCALE:
        raise ValueError("tempo_scale must be between 1/3 and 4.0")
    projection = score_projection(piece_id, movement)
    duration_seconds = max(0.0, duration_seconds)
    end_seconds = start_seconds + duration_seconds
    positions = [(0.0, projection.position_at_source_seconds(start_seconds))]
    positions.extend(
        (
            (source_tick / SOURCE_TICKS_PER_SECOND - start_seconds) / tempo_scale,
            projection.position_at_source_tick(source_tick),
        )
        for source_tick, _score_tick, _confidence in projection.anchors
        if start_seconds < source_tick / SOURCE_TICKS_PER_SECOND < end_seconds
    )
    if duration_seconds > 0:
        positions.append(
            (
                duration_seconds / tempo_scale,
                projection.position_at_source_seconds(end_seconds),
            )
        )
    return ScoreTransportResponse(
        piece_id=piece_id,
        movement=movement,
        mapping_id=projection.mapping_id,
        mapping_review_state=projection.mapping_review_state.value,
        canonical_position=projection.canonical_position,
        anchors=[
            ScoreTransportAnchorResponse(
                elapsed_seconds=elapsed,
                position=ScorePositionResponse(**position.__dict__),
            )
            for elapsed, position in positions
        ],
    )
