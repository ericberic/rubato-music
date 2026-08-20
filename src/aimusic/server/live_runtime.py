"""Application manager for replay and live FOLLOW accompaniment runs."""

from __future__ import annotations

import contextlib
import gc
import inspect
import logging
import os
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import mido

from aimusic.accompaniment.bundle_v2 import BundleRegistry
from aimusic.accompaniment.following import (
    OracleFollower,
    PerformedNote,
    ReferencePitchFollower,
    RelockingFollower,
    ScoreFollower,
)
from aimusic.accompaniment.latency_calibration import (
    CalibrationResult,
    measure_output_latency,
)
from aimusic.accompaniment.live_engine import LiveEngine
from aimusic.accompaniment.matchmaker_follower import MatchmakerStreamFollower
from aimusic.accompaniment.midi_output import (
    DeadlineAccompanimentOutput,
    MidiOutputFactory,
    MidoAccompanimentOutput,
)
from aimusic.accompaniment.predictive_follow import InterpretationArrivalCurve, PaceProfile
from aimusic.accompaniment.rehearsal_position import (
    MOVEMENT_2_REACTIVE_ANCHOR_TICKS,
    score_projection,
)
from aimusic.accompaniment.runtime_contracts import (
    AudioWorkerTrace,
    LiveStateWord,
    LoopTimingTrace,
    OrchestraRendererStatus,
    RunMode,
    RunPhase,
    RuntimeConfig,
    RuntimeScorePosition,
    RuntimeStatus,
    TelemetryLevel,
)
from aimusic.accompaniment.runtime_io import (
    CapturingOutput,
    JsonlTraceSink,
    ManualClock,
    MonotonicClock,
    RecordedNoteReplay,
    SystemMonotonicClock,
    TraceSink,
)
from aimusic.accompaniment.runtime_projection import (
    CanonicalFollower,
    ProvisionalRuntimeProjection,
    default_bundle_registry,
    project_bundle_v2_to_provisional_runtime,
    runtime_follower,
)
from aimusic.accompaniment.scheduler import (
    ArrivalTimeCurve,
    ReferenceWarpArrivalCurve,
    ScheduledAccompanimentEvent,
)
from aimusic.accompaniment.section_policy import AccompanimentMode
from aimusic.audio.live_config import load_live_audio_config
from aimusic.audio.live_reaper import ReaperMidiRouter, load_reaper_heartbeat
from aimusic.audio.live_vst import (
    LiveVstError,
    LiveVstRouter,
    LiveVstZoneMixer,
    LiveVstZoneWorker,
    MultiZoneAccompanimentOutput,
)
from aimusic.core import paths
from aimusic.core.events import events
from aimusic.mixing import store as mix_store
from aimusic.mixing.policy import compile_mix_policy
from aimusic.mixing.zones import room_zones
from aimusic.realtime.follower_process import FollowerSpec, ProcessFollower
from aimusic.server.live_control import LiveControl, live_control
from aimusic.server.schemas import (
    LivePerformancePlanResponse,
    LiveRuntimeStatusEvent,
    OrchestraRendererStatusEvent,
)
from aimusic.takes import store as take_store
from aimusic.takes.models import Interpretation
from aimusic.takes.tempo_expectation import TempoPrior

MidiInputFactory = Callable[[str], Any]
CapturedMidiEvent = tuple[float, mido.Message]

_PERFORMANCE_MIDI_TYPES = frozenset(
    {
        "note_on",
        "note_off",
        "polytouch",
        "control_change",
        "program_change",
        "aftertouch",
        "pitchwheel",
    }
)

logger = logging.getLogger(__name__)

#: The piece's default spatial-mix program. Selected whenever a run enables the
#: mix without naming a specific program, so every entry point (rehearse from a
#: bar, perform from the top) resolves the same mix instead of one path silently
#: skipping it.
_DEFAULT_MIX_PROGRAM_ID = "main"
# When a REAPER renderer is in a durable failure but its bridge heartbeat has
# since recovered, the client-facing status auto-retries the preload at most this
# often, so the PWA self-heals without a manual forced Retry and without hammering.
_RENDERER_AUTO_RETRY_COOLDOWN_SECONDS = 20.0
# A ready resident renderer must read unhealthy for at least this long before we
# tear it down. An audio device change (e.g. the LG soundbar re-negotiating over
# HDMI/eARC) makes REAPER re-open its engine, so the bridge heartbeat blips
# "not ready" for a few seconds; riding through that blip keeps the resident MIDI
# source open and avoids the failed->retry churn that spawns duplicate device rows.
_RESIDENT_HEALTH_GRACE_SECONDS = 20.0


@dataclass(frozen=True)
class _RuntimeEntryPoint:
    """One explicit measure -> canonical beat -> reference beat conversion."""

    measure: int
    score_beat: float
    reference_beat: float | None
    section_mode: AccompanimentMode

    @property
    def follower_prior_reference_beat(self) -> float:
        if self.reference_beat is None:
            raise ValueError(
                "Matchmaker measure warm-start requires a mapped reference-performance beat"
            )
        return self.reference_beat


class _ResidentVstLease:
    """Run-scoped view of a renderer that remains resident after run shutdown."""

    def __init__(self, manager: "LiveRuntimeManager", router: Any) -> None:
        self._manager = manager
        self._router = router
        self._closed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._router, name)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._manager._return_resident_renderer(self._router)


def _runtime_entry_point(
    projection: ProvisionalRuntimeProjection,
    *,
    start_measure: int | None,
) -> _RuntimeEntryPoint | None:
    """Resolve printed measure identity without leaking it into reference APIs."""

    if start_measure is None:
        return None
    if start_measure < 1:
        raise ValueError("start_measure must be at least 1")
    manifest = projection.source.manifest
    if projection.coordinate_system == "canonical_score":
        display = score_projection(
            manifest.work.work_id,
            int(manifest.work.movement_id),
        )
        measure_index = start_measure - 1
        if measure_index >= len(display.timeline.document.measures):
            raise ValueError(f"start_measure {start_measure} is outside the score")
        score_beat = (
            display.timeline.tick_at(measure_index) / display.timeline.document.canonical_ppq
        )
    else:
        measure_events = tuple(
            event for event in projection.bundle.events if event.measure == start_measure
        )
        if not measure_events:
            raise ValueError(f"start_measure {start_measure} is outside the runtime bundle")
        score_beat = min(event.beat for event in measure_events)
    section = projection.bundle.section_map.section_at(score_beat)
    if section.mode is AccompanimentMode.STOP:
        raise ValueError(f"cannot start inside STOP section at measure {start_measure}")
    reference_beat = ReferenceWarpArrivalCurve(
        projection.bundle.events
    ).reference_beat_at_score_beat(score_beat)
    return _RuntimeEntryPoint(
        measure=start_measure,
        score_beat=score_beat,
        reference_beat=reference_beat,
        section_mode=section.mode,
    )


def _slice_replay_notes(
    notes: tuple[PerformedNote, ...],
    *,
    start_beat: float,
) -> tuple[PerformedNote, ...]:
    """Truncate an aligned replay without reordering its performed timeline."""

    if any(note.score_beat is None for note in notes):
        raise ValueError("mid-piece replay requires aligned score_beat on every note")
    if not notes:
        return ()
    ordered = tuple(sorted(notes, key=lambda note: note.perf_time))
    origin = _replay_time_at_score_beat(ordered, start_beat)
    return tuple(
        replace(note, perf_time=max(0.0, note.perf_time - origin))
        for note in ordered
        if note.perf_time >= origin
    )


def _replay_time_at_score_beat(
    notes: tuple[PerformedNote, ...],
    score_beat: float,
) -> float:
    points = tuple(
        (float(note.score_beat), note.perf_time) for note in notes if note.score_beat is not None
    )
    for beat, perf_time in points:
        if beat == score_beat:
            return perf_time
    for (left_beat, left_time), (right_beat, right_time) in zip(points, points[1:]):
        if min(left_beat, right_beat) <= score_beat <= max(left_beat, right_beat):
            if right_beat == left_beat:
                continue
            ratio = (score_beat - left_beat) / (right_beat - left_beat)
            return left_time + ratio * (right_time - left_time)
    raise ValueError(f"replay does not cross start beat {score_beat}")


def _anchor_ticks(projection: ProvisionalRuntimeProjection) -> tuple[int, ...]:
    """Load the piece/movement's structural beat anchors as canonical ticks."""

    work = projection.source.manifest.work
    anchor_set = take_store.load_anchor_set(work.work_id, int(work.movement_id))
    ticks = {anchor.score_tick for anchor in anchor_set.anchors}
    if (work.work_id, str(work.movement_id)) == ("chopin_op11", "2"):
        ticks.update(MOVEMENT_2_REACTIVE_ANCHOR_TICKS)
    return tuple(sorted(ticks))


def _interpretation_arrival_curve(
    projection: ProvisionalRuntimeProjection,
    config: RuntimeConfig,
) -> ArrivalTimeCurve | None:
    """Load a supported fitted curve for LTE, or preserve the reference baseline."""

    if config.follow_clock != "lte":
        return None
    work = projection.source.manifest.work
    profile_path = (
        paths.data_root() / "profiles" / work.work_id / str(work.movement_id) / "profile.json"
    )
    if not profile_path.is_file():
        return None
    interpretation = Interpretation.model_validate_json(profile_path.read_text(encoding="utf-8"))
    if (
        not interpretation.cells
        or interpretation.base_seconds_per_quarter is None
        or not any(cell.support >= 2 for cell in interpretation.cells)
    ):
        return None

    prior = TempoPrior.from_interpretation(interpretation)
    requested_base_period = 60.0 / config.initial_tempo_bpm
    scale = requested_base_period / interpretation.base_seconds_per_quarter

    def expected_period_at_tick(score_tick: float) -> float | None:
        period = prior.period_at(score_tick)
        return period * scale if period is not None else None

    def dispersion_at_tick(score_tick: float) -> float | None:
        dispersion = prior.dispersion_at(score_tick)
        return dispersion * scale if dispersion is not None else None

    return InterpretationArrivalCurve(
        reference_curve=ReferenceWarpArrivalCurve(projection.bundle.events),
        expected_period_at_tick=expected_period_at_tick,
        dispersion_at_tick=dispersion_at_tick,
        canonical_ppq=interpretation.canonical_ppq,
        grid_step_ticks=interpretation.grid_step_ticks,
        curve_id=(
            f"interpretation-dispersion-v1-r{interpretation.revision}"
            f"-{interpretation.input_revision[:12]}-scale{scale:.9g}"
        ),
    )


def _pace_profile(
    projection: ProvisionalRuntimeProjection,
    config: RuntimeConfig,
) -> PaceProfile | None:
    """Build the phrase-level rehearsal pace prior, or ``None`` when unfit.

    Mirrors the arrival curve's profile loading but exposes a bar-smoothed
    canonical pace and its take support, so the FOLLOW clock can anchor its pace
    magnitude to the rehearsals rather than to live follower jitter.
    """

    if config.follow_clock != "lte":
        return None
    work = projection.source.manifest.work
    profile_path = (
        paths.data_root() / "profiles" / work.work_id / str(work.movement_id) / "profile.json"
    )
    if not profile_path.is_file():
        return None
    interpretation = Interpretation.model_validate_json(profile_path.read_text(encoding="utf-8"))
    if (
        not interpretation.cells
        or interpretation.base_seconds_per_quarter is None
        or not any(cell.support >= 2 for cell in interpretation.cells)
    ):
        return None

    prior = TempoPrior.from_interpretation(interpretation)
    ppq = interpretation.canonical_ppq
    scale = (60.0 / config.initial_tempo_bpm) / interpretation.base_seconds_per_quarter
    # One 4/4 bar of smoothing keeps the phrase-level tempo arc the takes agree
    # on while dropping sub-beat micro-rubato a live follower cannot resolve.
    window_ticks = 4 * ppq

    def smoothed_period_at(score_beat: float) -> float | None:
        period = prior.smoothed_period_at(score_beat * ppq, window_ticks=window_ticks)
        return period * scale if period is not None else None

    def support_at(score_beat: float) -> int:
        return prior.support_at(score_beat * ppq)

    return PaceProfile(smoothed_period_at=smoothed_period_at, support_at=support_at)


class LoopProfiler:
    """Accumulate per-iteration cadence/work for a real-time loop, emit windows.

    ``record(work_seconds)`` is called once per loop iteration. Every
    ``window_seconds`` it writes a :class:`LoopTimingTrace` summarizing the gap
    between iterations (cadence / stalls) and the work duration (hot spots), then
    resets. Overhead is a couple of list appends per iteration; the median/p95/max
    are computed only at emit time. Each stage owns its own profiler, so no
    locking is needed even across threads.
    """

    def __init__(
        self,
        name: str,
        clock: MonotonicClock,
        trace_sink: TraceSink,
        *,
        window_seconds: float = 1.0,
    ) -> None:
        self._name = name
        self._clock = clock
        self._sink = trace_sink
        self._window = window_seconds
        self._intervals: list[float] = []
        self._work: list[float] = []
        self._last_iter: float | None = None
        self._last_emit = clock.now()

    def record(self, work_seconds: float) -> None:
        now = self._clock.now()
        if self._last_iter is not None:
            self._intervals.append(now - self._last_iter)
        self._last_iter = now
        self._work.append(max(0.0, work_seconds))
        if now - self._last_emit >= self._window:
            self._emit(now)

    @staticmethod
    def _stats(values: list[float]) -> tuple[float | None, float | None, float | None]:
        if not values:
            return None, None, None
        ordered = sorted(values)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        return ordered[len(ordered) // 2] * 1000, p95 * 1000, ordered[-1] * 1000

    def _emit(self, now: float) -> None:
        interval = self._stats(self._intervals)
        work = self._stats(self._work)
        self._sink.write(
            LoopTimingTrace(
                type="loop_timing",
                monotonic_time=now,
                loop=self._name,
                window_seconds=max(1e-9, now - self._last_emit),
                iterations=len(self._work),
                interval_ms_median=interval[0],
                interval_ms_p95=interval[1],
                interval_ms_max=interval[2],
                work_ms_median=work[0],
                work_ms_p95=work[1],
                work_ms_max=work[2],
            )
        )
        self._intervals.clear()
        self._work.clear()
        self._last_emit = now


class RealtimeGC:
    """Keep garbage collection from stalling the real-time loops during a run.

    A generational pass has to walk live objects, and the runtime holds a lot of
    them (score bundle, warp tables, prefix curves). Measured on a replay, the
    input thread stalled up to 49 ms while doing 0.00 ms of its own work — it
    simply was not scheduled, the signature of a collector pause rather than
    contention. Setup allocates almost everything the run needs, so
    ``gc.freeze()`` moves that graph into a permanent generation the collector
    stops re-walking, and generous thresholds stop routine gen-0 churn from
    escalating mid-performance. Reference cycles are still collected on exit;
    this trades a little peak memory for deadline stability.
    """

    def __enter__(self) -> "RealtimeGC":
        self._thresholds = gc.get_threshold()
        gc.collect()
        gc.freeze()
        gc.set_threshold(50_000, 500, 500)
        return self

    def __exit__(self, *exc: object) -> None:
        gc.set_threshold(*self._thresholds)
        gc.unfreeze()


class StatusBroadcaster:
    """Publish runtime status to the UI without holding up the audio path.

    Browser updates are not real-time work: they only need to be *recent*. The
    conductor stores the latest status and returns immediately while a daemon
    thread does the serialization and socket fan-out. The slot **conflates** — a
    newer status overwrites an unsent one — so a slow or stalled client can never
    queue work or apply backpressure to the runtime. Measured before this change,
    an inline publish spiked to 10-34 ms against a ~5 ms tick budget.
    """

    def __init__(self) -> None:
        self._pending: LiveRuntimeStatusEvent | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="rubato-ui-publish", daemon=True)
        self._thread.start()

    def submit(self, event: LiveRuntimeStatusEvent) -> None:
        with self._lock:
            self._pending = event
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=0.1)
            self._wake.clear()
            with self._lock:
                event, self._pending = self._pending, None
            if event is not None:
                try:
                    events.publish(event)
                except Exception:  # a failing subscriber must not kill the loop
                    logger.exception("live status publish failed")

    def flush(self, timeout: float = 1.0) -> None:
        """Block until the pending status has been sent (tests and shutdown)."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._pending is None:
                    return
            time.sleep(0.001)

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=1.0)


def _write_captured_performance(
    recording_id: str,
    events: list[CapturedMidiEvent],
) -> Path:
    """Persist one live performance to the ephemeral session workspace.

    Capture is an invariant of the causal runtime, not a rehearsal mode. The
    performer can hear or inspect this MIDI after any live run and may later
    promote it into the durable take store. Until then it remains scratch
    evidence keyed by the public runtime ``run_id``.
    """

    from mido import MidiFile, MidiTrack, bpm2tempo, second2tick

    directory = paths.session_dir(recording_id)
    directory.mkdir(parents=True, exist_ok=True)
    output_path = directory / "solo.mid"
    ticks_per_beat, tempo_bpm = 480, 120
    tempo = bpm2tempo(tempo_bpm)
    midi = MidiFile(ticks_per_beat=ticks_per_beat)
    track = MidiTrack()
    midi.tracks.append(track)
    ordered = sorted(events, key=lambda item: item[0])
    origin = ordered[0][0] if ordered else 0.0
    last = 0.0
    active_notes: set[tuple[int, int]] = set()
    track.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))
    for perf_time, message in ordered:
        elapsed = max(0.0, perf_time - origin)
        delta_ticks = int(round(second2tick(elapsed - last, ticks_per_beat, tempo)))
        track.append(message.copy(time=max(0, delta_ticks)))
        if message.type == "note_on" and message.velocity > 0:
            active_notes.add((message.channel, message.note))
        elif message.type == "note_off" or (message.type == "note_on" and message.velocity == 0):
            active_notes.discard((message.channel, message.note))
        last = elapsed
    # A failure or panic can stop the stream before the Yamaha emits releases.
    # Close only those still-held notes so the scratch file cannot stick when
    # auditioned; genuine note durations and pedal events remain intact.
    for index, (channel, note) in enumerate(sorted(active_notes)):
        track.append(
            mido.Message(
                "note_off",
                channel=channel,
                note=note,
                velocity=0,
                time=1 if index == 0 else 0,
            )
        )
    midi.save(output_path)
    logger.info(
        "Captured live performance %s to the ephemeral workspace: %d notes",
        recording_id,
        len(ordered),
    )
    return output_path


def _follower_in_subprocess() -> bool:
    """Isolate the follower's HMM in its own process unless explicitly disabled.

    ``RUBATO_FOLLOWER_PROCESS=0`` restores the in-process follower for A/B
    profiling of the GIL-starvation fix.
    """

    return os.environ.get("RUBATO_FOLLOWER_PROCESS", "1") not in {"0", "false", "no"}


def _start_live_vst_router(
    audio_config: Any,
    mix_policy: Any,
    trace_sink: TraceSink,
    telemetry_level: TelemetryLevel = TelemetryLevel.COUNTERS,
    *,
    startup_observer: Callable[[dict[str, Any]], None] | None = None,
) -> Any | None:
    active_zone_ids = {
        route.active_zone_id
        for route in mix_policy.default_routes
        if route.active_zone_id is not None
    }
    active_zone_ids.update(
        route.active_zone_id
        for region in mix_policy.regions
        for route in region.routes
        if route.active_zone_id is not None
    )
    active_zones = tuple(zone for zone in audio_config.zones if zone.zone_id in active_zone_ids)
    reaper_zones = tuple(zone for zone in active_zones if zone.renderer == "reaper")
    pedalboard_zones = tuple(zone for zone in active_zones if zone.renderer == "pedalboard")
    if reaper_zones and pedalboard_zones:
        raise LiveVstError("one live mix cannot combine REAPER and Pedalboard audio hosts")
    if len(reaper_zones) > 1:
        raise LiveVstError("the REAPER experiment currently supports one live audio zone")
    if reaper_zones:
        return ReaperMidiRouter(
            reaper_zones[0],
            mix_policy,
            trace_sink=trace_sink,
            startup_observer=startup_observer,
        )
    total_instruments = sum(len(zone.instruments) for zone in active_zones)
    workers: list[LiveVstZoneWorker] = []
    mixers: list[LiveVstZoneMixer] = []
    loaded_instruments = 0
    try:
        for zone in active_zones:
            # BBCSO 1.12 cannot render two resident instances efficiently in
            # one host, and concurrent native initialization is unstable.
            # Load one patch per host, suspend each completed host while the
            # next initializes, then resume all of them behind one zone-owned
            # CoreAudio stream. BBCSO also has an
            # intermittent pointer-authentication crash in its own Work Thread
            # during cold initialization. Recover one fresh host inside this
            # explicit preload attempt; a second native exit remains a durable,
            # circuit-broken failure rather than becoming a crash loop.
            zone_workers: list[LiveVstZoneWorker] = []
            for binding in zone.instruments:
                isolated_zone = zone.model_copy(update={"instruments": (binding,)})
                base_loaded = loaded_instruments

                def observe_isolated(
                    progress: dict[str, Any],
                    *,
                    base: int = base_loaded,
                    fallback_instrument_id: str = binding.instrument_id,
                ) -> None:
                    if startup_observer is None:
                        return
                    local_state = str(progress.get("state", "loading"))
                    if local_state == "failed":
                        # The constructor will either perform the one bounded
                        # native-host retry below or surface the final failure.
                        # Do not flash a terminal UI state between those paths.
                        return
                    instrument_id = progress.get("instrument_id") or fallback_instrument_id
                    is_last = base + 1 == total_instruments
                    if not is_last and local_state in {"opening_audio", "ready"}:
                        return
                    if local_state == "ready":
                        state = "ready"
                        loaded = base + 1
                    elif local_state == "opening_audio":
                        state = "opening_audio"
                        loaded = base + 1
                    else:
                        state = local_state
                        loaded = base
                    startup_observer(
                        {
                            **progress,
                            "state": state,
                            "instrument_id": instrument_id,
                            "loaded": loaded,
                            "total": total_instruments,
                        }
                    )

                for attempt in range(2):
                    try:
                        worker = LiveVstZoneWorker(
                            isolated_zone,
                            mix_policy,
                            trace_sink=trace_sink,
                            startup_observer=observe_isolated,
                            start_timeout_seconds=30.0,
                            telemetry_level=telemetry_level,
                            deferred_audio_output=True,
                        )
                        break
                    except LiveVstError as exc:
                        recoverable_native_exit = attempt == 0 and exc.worker_exit_code in {
                            -15,
                            -11,
                        }
                        if not recoverable_native_exit:
                            raise
                        logger.warning(
                            "BBCSO %s cold host crashed with exit %s; retrying once",
                            binding.instrument_id,
                            exc.worker_exit_code,
                        )
                        if startup_observer is not None:
                            startup_observer(
                                {
                                    "state": "loading",
                                    "zone_id": zone.zone_id,
                                    "instrument_id": binding.instrument_id,
                                    "loaded": loaded_instruments,
                                    "total": total_instruments,
                                    "detail": "Native host exited; retrying once",
                                }
                            )
                        time.sleep(1.0)
                workers.append(worker)
                zone_workers.append(worker)
                worker.suspend()
                loaded_instruments += 1
            for worker in zone_workers:
                worker.resume()
            mixers.append(
                LiveVstZoneMixer(
                    zone,
                    tuple(zone_workers),
                    trace_sink=trace_sink,
                )
            )
    except Exception:
        for worker in workers:
            worker.resume()
            worker.close()
        for mixer in mixers:
            mixer.close()
        raise
    return LiveVstRouter(tuple(workers), tuple(mixers)) if workers else None


def _safe_close_port(port: Any) -> None:
    try:
        port.close()
    except Exception:  # pragma: no cover - close is best-effort during recovery
        pass


def _reconnect_input_port(
    input_name: str,
    input_factory: "MidiInputFactory",
    stop_event: threading.Event,
    *,
    backoff_seconds: float = 0.25,
) -> Any | None:
    """Reopen a live MIDI input that dropped mid-run, or None if asked to stop.

    A transient CoreMIDI/device blip (e.g. the Yamaha briefly disconnecting)
    invalidates the open port. Retry opening it by name until it reappears or the
    run stops, so a momentary dropout does not end the performance.
    """

    while not stop_event.is_set():
        try:
            return input_factory(input_name)
        except Exception:
            stop_event.wait(backoff_seconds)
    return None


@contextlib.contextmanager
def _keep_awake(reason: str = "Rubato live performance"):
    """Hold a macOS power assertion for the duration of a live performance.

    A performance can run for many minutes without touching the trackpad, and if
    the Mac idle-sleeps or blanks the display mid-run the audio engine and MIDI
    stop. ``caffeinate`` asserts display + idle + system wake while the run is
    active and is released the instant it ends (any exit path). Best-effort: if
    it cannot start, the run continues -- keeping the Mac on AC power remains the
    reliable guard, especially with the lid closed.
    """

    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(
            ["/usr/bin/caffeinate", "-dis", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("Holding wake assertion for %s (pid %s)", reason, proc.pid)
    except Exception:
        logger.warning("Could not hold a wake assertion; the Mac may sleep during the run")
        proc = None
    try:
        yield
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except Exception:
                proc.kill()
            logger.info("Released wake assertion for %s", reason)


def _read_input_into_queue(
    port: Any,
    clock: MonotonicClock,
    sink: "deque[tuple[float, int, int]]",
    capture_sink: list[CapturedMidiEvent],
    stop_event: threading.Event,
    profiler: LoopProfiler | None = None,
    *,
    input_name: str | None = None,
    input_factory: "MidiInputFactory | None" = None,
) -> None:
    """Timestamp incoming MIDI note-ons at arrival, in a dedicated thread.

    Runs independently of the follower/scheduler/publish work so a chord's notes
    are stamped together the instant they arrive, never serialized behind
    processing. rtmidi already buffers incoming messages in its own C thread;
    this loop drains that buffer every ~0.5 ms and stamps one ``perf_time`` per
    drain, so simultaneous notes share it. The GIL is released during the port
    read and the wait, so a busy processing thread cannot delay these stamps.

    If the port read raises mid-run (a transient device/CoreMIDI dropout) and a
    reconnect factory was supplied, reopen the input by name and keep going with a
    logged warning -- a silent return here would drop the soloist for the rest of
    the performance. Without a factory (or once stop is requested), stop quietly.
    """

    reconnect_enabled = input_name is not None and input_factory is not None
    current = port
    owns_current = False  # True once we reopened the port ourselves and must close it

    try:
        while not stop_event.is_set():
            now = clock.now()
            try:
                messages = list(current.iter_pending())
            except Exception:
                if stop_event.is_set() or not reconnect_enabled:
                    # Intentional teardown, or no way to recover: stop quietly.
                    return
                logger.warning(
                    "MIDI input %r stopped delivering mid-run; reconnecting", input_name
                )
                if owns_current:
                    _safe_close_port(current)
                assert input_name is not None and input_factory is not None
                reopened = _reconnect_input_port(input_name, input_factory, stop_event)
                if reopened is None:
                    return  # stop requested while waiting for the device to return
                current = reopened
                owns_current = True
                logger.warning("MIDI input %r reconnected; resuming capture", input_name)
                continue
            for message in messages:
                if message.type in _PERFORMANCE_MIDI_TYPES:
                    capture_sink.append((now, message.copy(time=0)))
                if message.type == "note_on" and message.velocity > 0:
                    sink.append((now, message.note, message.velocity))
            if profiler is not None:
                profiler.record(clock.now() - now)
            stop_event.wait(0.0005)
    finally:
        if owns_current:
            _safe_close_port(current)


def _resolve_follow_clock(config: RuntimeConfig) -> RuntimeConfig:
    """Let an operator force the FOLLOW clock at launch for hardware A/B.

    ``RUBATO_FOLLOW_CLOCK=lte|reactive`` overrides the request's clock so LTE can
    be exercised on real Yamaha input before a cockpit toggle exists
    (docs/decisions/0008-predictive-follow-clock.md). Unset, the request's own
    ``follow_clock`` (default ``lte``) is used unchanged.
    """

    override = os.environ.get("RUBATO_FOLLOW_CLOCK")
    if override not in {"reactive", "lte"}:
        return config
    if override != config.follow_clock:
        config = config.model_copy(update={"follow_clock": override})
        logger.info("FOLLOW clock overridden to %r via RUBATO_FOLLOW_CLOCK", override)
    return config


class _LatestTempoCommand:
    """Single-slot, acknowledged mailbox for live tempo changes."""

    def __init__(self, control_name: str = "tempo") -> None:
        self._control_name = control_name
        self._condition = threading.Condition()
        self._desired_tempo_bpm: float | None = None
        self._requested_revision = 0
        self._applied_revision = 0
        self._closed = False
        self._error: str | None = None

    def submit(self, tempo_bpm: float, *, timeout: float = 0.5) -> None:
        with self._condition:
            if self._closed:
                raise RuntimeError(self._error or "Live performance is no longer running")
            self._desired_tempo_bpm = tempo_bpm
            self._requested_revision += 1
            revision = self._requested_revision
            self._condition.notify_all()
            applied = self._condition.wait_for(
                lambda: self._applied_revision >= revision or self._closed,
                timeout=timeout,
            )
            if not applied:
                raise RuntimeError(f"Live {self._control_name} change was not acknowledged")
            if self._closed and self._applied_revision < revision:
                raise RuntimeError(
                    self._error or f"Live performance stopped before {self._control_name} changed"
                )
            if self._applied_revision > revision:
                raise RuntimeError(
                    f"Live {self._control_name} change was superseded by a newer request"
                )

    def next_pending(self) -> tuple[int, float] | None:
        with self._condition:
            if self._closed or self._requested_revision <= self._applied_revision:
                return None
            assert self._desired_tempo_bpm is not None
            return self._requested_revision, self._desired_tempo_bpm

    def acknowledge(self, revision: int) -> None:
        with self._condition:
            self._applied_revision = max(self._applied_revision, revision)
            self._condition.notify_all()

    def close(self, error: str | None = None) -> None:
        with self._condition:
            self._closed = True
            self._error = error
            self._condition.notify_all()


class LiveRuntimeManager:
    """Own one runtime status while ``LiveControl`` owns hardware exclusion."""

    def __init__(
        self,
        *,
        hardware_control: LiveControl = live_control,
        input_factory: MidiInputFactory = mido.open_input,
        output_factory: MidiOutputFactory = mido.open_output,
        bundle_registry: BundleRegistry | None = None,
        audio_config_loader: Callable[[], Any] = load_live_audio_config,
        mix_zones_factory: Callable[[Any], tuple[Any, ...]] = room_zones,
        vst_router_factory: Callable[
            [Any, Any, TraceSink, TelemetryLevel], Any
        ] = _start_live_vst_router,
    ) -> None:
        self._hardware_control = hardware_control
        self._input_factory = input_factory
        self._output_factory = output_factory
        self._bundle_registry = bundle_registry or default_bundle_registry()
        self._audio_config_loader = audio_config_loader
        self._mix_zones_factory = mix_zones_factory
        self._vst_router_factory = vst_router_factory
        self._lock = threading.Lock()
        self._status: RuntimeStatus | None = None
        self._last_published_status: RuntimeStatus | None = None
        self._broadcaster = StatusBroadcaster()
        self._tempo_commands: _LatestTempoCommand | None = None
        self._volume_commands: _LatestTempoCommand | None = None
        self._output_advance_commands: _LatestTempoCommand | None = None
        self._renderer_lock = threading.RLock()
        self._renderer_status = OrchestraRendererStatus(updated_at_monotonic=time.monotonic())
        self._resident_vst_router: Any | None = None
        self._resident_vst_key: str | None = None
        self._resident_trace_sink: JsonlTraceSink | None = None
        self._resident_in_use = False
        self._renderer_thread: threading.Thread | None = None
        self._renderer_auto_retry_monotonic = 0.0
        self._resident_unhealthy_monotonic: float | None = None

    def status(self) -> RuntimeStatus | None:
        with self._lock:
            return self._status.model_copy(deep=True) if self._status is not None else None

    def _resident_router_failed(self, router: Any) -> bool:
        """True only once the resident renderer has been unhealthy past the grace.

        A single unhealthy heartbeat read is usually a transient audio-device
        re-open (soundbar re-negotiating over eARC), which recovers in a couple of
        seconds. Tearing the renderer down on the first bad read cascades into the
        failed->retry loop that churns duplicate MIDI device rows; riding out a
        brief blip avoids that entirely. A genuine failure persists past the grace.
        """

        if router.is_alive:
            self._resident_unhealthy_monotonic = None
            return False
        now = time.monotonic()
        started = self._resident_unhealthy_monotonic
        if started is None:
            self._resident_unhealthy_monotonic = now
            return False
        return (now - started) >= _RESIDENT_HEALTH_GRACE_SECONDS

    def renderer_status(self) -> OrchestraRendererStatus:
        with self._renderer_lock:
            router = self._resident_vst_router
            should_check = self._renderer_status.state == "ready" and router is not None
        if should_check and self._resident_router_failed(router):
            self._mark_resident_renderer_failed(router)
        with self._renderer_lock:
            return self._renderer_status.model_copy(deep=True)

    def renderer_status_for_client(self) -> OrchestraRendererStatus:
        """Renderer status for the PWA poll, with self-healing auto-retry.

        The failure circuit breaker in ``preload_renderer`` deliberately refuses
        to auto-retry a ``failed``/``unavailable`` renderer, so the badge would
        otherwise stay stale until the user pressed a forced Retry -- even after
        the underlying host recovered (LG soundbar reconnected, MIDI re-bound by
        the bridge's own auto-reset). When the REAPER bridge heartbeat is fresh
        and ready again, kick off one forced preload (rate-limited) so the stale
        ``failed``/``heartbeat stale`` state clears on its own. The retry's
        startup performs the authoritative readiness check.
        """

        status = self.renderer_status()
        if status.state not in {"failed", "unavailable"}:
            return status
        now = time.monotonic()
        with self._renderer_lock:
            if self._renderer_status.state in {"loading", "opening_audio"}:
                return self._renderer_status.model_copy(deep=True)
            if (now - self._renderer_auto_retry_monotonic) < _RENDERER_AUTO_RETRY_COOLDOWN_SECONDS:
                return status
        if not self._reaper_heartbeat_recovered():
            return status
        with self._renderer_lock:
            self._renderer_auto_retry_monotonic = now
        logger.info("REAPER bridge heartbeat recovered; auto-retrying orchestra renderer preload")
        try:
            return self.preload_renderer(force=True)
        except Exception:  # pragma: no cover - defensive; a retry must never 500 the poll
            logger.exception("Auto-retry orchestra renderer preload failed to start")
            return self.renderer_status()

    def _reaper_heartbeat_recovered(self) -> bool:
        """True when the REAPER bridge heartbeat is fresh and reports ``ready``.

        A cheap, best-effort recovery signal that gates the auto-retry above. It
        checks only freshness and the bridge's own ready state -- not the full
        router readiness (MIDI ingress probe, device/rate match), which the retry
        itself re-verifies authoritatively. Returns ``False`` for non-REAPER
        renderers or any read error, so it never forces a retry it cannot justify.
        """

        try:
            audio_config = self._audio_config_loader()
        except Exception:
            return False
        reaper_zones = [
            zone for zone in audio_config.zones if getattr(zone, "renderer", "") == "reaper"
        ]
        if len(reaper_zones) != 1:
            return False
        zone = reaper_zones[0]
        heartbeat_path = getattr(zone, "reaper_heartbeat_path", None)
        if heartbeat_path is None:
            return False
        try:
            heartbeat = load_reaper_heartbeat(heartbeat_path)
        except Exception:
            return False
        age = time.time() - heartbeat.updated_at_epoch
        timeout = getattr(zone, "reaper_heartbeat_timeout_seconds", 10.0)
        if age < -2 or age > timeout:
            return False
        return heartbeat.state == "ready"

    def preload_renderer(
        self,
        *,
        program_id: str = _DEFAULT_MIX_PROGRAM_ID,
        force: bool = False,
    ) -> OrchestraRendererStatus:
        """Begin preparing the configured orchestra renderer in the background."""

        # Convert a stale Ready badge into a durable failure before applying
        # the no-auto-retry circuit breaker below.
        self.renderer_status()
        with self._renderer_lock:
            if self._renderer_status.state in {"loading", "opening_audio"}:
                return self._renderer_status.model_copy(deep=True)
            if self._renderer_status.state in {"failed", "unavailable"} and not force:
                return self._renderer_status.model_copy(deep=True)
            if (
                self._renderer_status.state == "ready"
                and self._resident_vst_router is not None
                and self._resident_vst_router.is_alive
            ):
                return self._renderer_status.model_copy(deep=True)
            preload_id = f"renderer-preload-{time.time_ns()}"
            trace_path = paths.run_trace_dir(preload_id) / "renderer.jsonl"
            initial = OrchestraRendererStatus(
                state="loading",
                preload_id=preload_id,
                started_at_monotonic=time.monotonic(),
                updated_at_monotonic=time.monotonic(),
                message="Checking the configured orchestra renderer",
                trace_path=str(trace_path),
            )
            self._renderer_status = initial
            thread = threading.Thread(
                target=self._preload_renderer_worker,
                args=(preload_id, program_id, trace_path),
                name="rubato-renderer-preload",
                daemon=True,
            )
            self._renderer_thread = thread
            thread.start()
        events.publish(OrchestraRendererStatusEvent(type="runtime:renderer_status", status=initial))
        return initial.model_copy(deep=True)

    def stop_preloaded_renderer(self) -> OrchestraRendererStatus:
        """Release the resident renderer connection when preload is disabled."""

        with self._renderer_lock:
            if self._resident_in_use:
                raise RuntimeError("Cannot unload the orchestra renderer during a live performance")
            router = self._resident_vst_router
            sink = self._resident_trace_sink
            self._resident_vst_router = None
            self._resident_vst_key = None
            self._resident_trace_sink = None
            status = OrchestraRendererStatus(
                state="not_loaded",
                updated_at_monotonic=time.monotonic(),
                message="Orchestra renderer preload is off",
            )
            self._renderer_status = status
        if router is not None:
            router.close()
        if sink is not None:
            sink.close()
        events.publish(OrchestraRendererStatusEvent(type="runtime:renderer_status", status=status))
        return status.model_copy(deep=True)

    def shutdown(self) -> None:
        try:
            self.stop_preloaded_renderer()
        except RuntimeError:
            logger.warning("Orchestra renderer still leased during server shutdown")

    @staticmethod
    def _renderer_key(audio_config: Any, mix_policy: Any) -> str:
        return f"{audio_config.model_dump_json()}\n{mix_policy.model_dump_json()}"

    def _publish_renderer_status(self, status: OrchestraRendererStatus) -> None:
        with self._renderer_lock:
            if (
                status.preload_id is not None
                and self._renderer_status.preload_id != status.preload_id
            ):
                return
            self._renderer_status = status
        events.publish(OrchestraRendererStatusEvent(type="runtime:renderer_status", status=status))

    def _mark_resident_renderer_failed(self, router: Any) -> None:
        """Atomically replace stale readiness when the active host disappears."""

        with self._renderer_lock:
            if self._resident_vst_router is not router:
                return
            self._resident_unhealthy_monotonic = None
            if self._renderer_status.state == "failed":
                return
            failure = next(iter(router.failure_details), {})
            instrument_id = failure.get("instrument_id")
            exit_code = failure.get("exit_code")
            detail = str(
                failure.get("detail")
                or (
                    f"The {instrument_id or 'resident'} orchestra renderer stopped unexpectedly; "
                    f"exit_code={exit_code}, last_stage={failure.get('last_stage')}"
                )
            )
            status = self._renderer_status.model_copy(
                update={
                    "state": "failed",
                    "updated_at_monotonic": time.monotonic(),
                    "message": detail,
                    "error_type": "NativeWorkerExit",
                    "error_detail": detail,
                    "instrument_id": instrument_id,
                    "worker_exit_code": exit_code,
                }
            )
            self._renderer_status = status
            sink = self._resident_trace_sink
        if sink is not None:
            sink.write(
                AudioWorkerTrace(
                    type="audio_worker",
                    monotonic_time=time.monotonic(),
                    zone_id=str(failure.get("zone_id") or "unknown"),
                    action="error",
                    instrument_id=instrument_id,
                    block_frames=int(failure.get("block_frames") or 512),
                    sample_rate=float(failure.get("sample_rate") or 48_000),
                    detail=detail,
                    startup_stage=str(failure.get("last_stage") or "resident"),
                    error_type="NativeWorkerExit",
                    worker_exit_code=exit_code,
                )
            )
        events.publish(OrchestraRendererStatusEvent(type="runtime:renderer_status", status=status))

    def _preload_renderer_worker(self, preload_id: str, program_id: str, trace_path: Path) -> None:
        router: Any | None = None
        trace_sink: JsonlTraceSink | None = None
        try:
            audio_config = self._audio_config_loader()
            try:
                program = mix_store.load_program(
                    "chopin_op11", 2, program_id, require_current_score=True
                )
            except mix_store.MixProgramNotFoundError:
                program = mix_store.create_program(
                    mix_store.MixProgramCreate(
                        piece_id="chopin_op11",
                        movement=2,
                        program_id=program_id,
                        name="Main spatial mix",
                    )
                )
            mix_policy = compile_mix_policy(program, self._mix_zones_factory(audio_config))
            key = self._renderer_key(audio_config, mix_policy)
            trace_sink = JsonlTraceSink(trace_path)

            def observe(progress: dict[str, Any]) -> None:
                reported_state = str(progress.get("state", "loading"))
                # The child reports ready just before its constructor returns.
                # Keep the public state at opening_audio until the manager has
                # installed the router, so Go Live can never race a false-ready
                # badge and start a second cold renderer.
                state = "opening_audio" if reported_state == "ready" else reported_state
                loaded = int(progress.get("loaded", 0))
                total = int(progress.get("total", 0))
                instrument_id = progress.get("instrument_id")
                device_name = next(
                    (
                        zone.output_device_name
                        for zone in audio_config.zones
                        if zone.zone_id == progress.get("zone_id")
                    ),
                    None,
                )
                renderer_name = (
                    "REAPER"
                    if any(
                        getattr(zone, "renderer", "pedalboard") == "reaper"
                        for zone in audio_config.zones
                    )
                    else "BBCSO"
                )
                if state == "opening_audio":
                    message = str(
                        progress.get("detail")
                        or (f"{renderer_name} · opening {device_name or 'CoreAudio'}")
                    )
                elif reported_state == "ready":
                    message = str(
                        progress.get("detail")
                        or (f"{renderer_name} · finalizing {device_name or 'CoreAudio'}")
                    )
                elif state == "failed":
                    message = str(progress.get("detail", "BBCSO failed to load"))
                elif instrument_id:
                    label = str(instrument_id).replace("_", " ")
                    message = (
                        f"Loading {renderer_name} {min(loaded + 1, total)} of {total} · {label}"
                    )
                else:
                    message = f"Starting the {renderer_name} orchestra renderer"
                self._publish_renderer_status(
                    OrchestraRendererStatus(
                        state=state,
                        preload_id=preload_id,
                        zone_id=progress.get("zone_id"),
                        device_name=device_name,
                        instrument_id=instrument_id,
                        loaded_instruments=loaded,
                        total_instruments=total,
                        started_at_monotonic=self.renderer_status().started_at_monotonic,
                        updated_at_monotonic=time.monotonic(),
                        message=message,
                        error_type=progress.get("error_type"),
                        error_detail=progress.get("detail") if state == "failed" else None,
                        trace_path=str(trace_path),
                    )
                )

            router = self._start_vst_router(
                audio_config,
                mix_policy,
                trace_sink,
                TelemetryLevel.COUNTERS,
                startup_observer=observe,
            )
            if router is None:
                raise LiveVstError("the selected mix has no active orchestra audio zone")
            with self._renderer_lock:
                if self._renderer_status.preload_id != preload_id:
                    router.close()
                    trace_sink.close()
                    return
                self._resident_vst_router = router
                self._resident_vst_key = key
                self._resident_trace_sink = trace_sink
                ready_status = self._renderer_status.model_copy(
                    update={
                        "state": "ready",
                        "instrument_id": None,
                        "updated_at_monotonic": time.monotonic(),
                        "message": (
                            f"Orchestra renderer ready on "
                            f"{self._renderer_status.device_name or 'the configured output'}"
                        ),
                    }
                )
                self._renderer_status = ready_status
            events.publish(
                OrchestraRendererStatusEvent(type="runtime:renderer_status", status=ready_status)
            )
            while True:
                time.sleep(0.5)
                with self._renderer_lock:
                    if self._resident_vst_router is not router:
                        return
                if self._resident_router_failed(router):
                    self._mark_resident_renderer_failed(router)
                    return
        except Exception as exc:
            logger.exception("Orchestra renderer preload %s failed", preload_id)
            with self._renderer_lock:
                preload_is_current = self._renderer_status.preload_id == preload_id
            if not preload_is_current:
                if router is not None:
                    router.close()
                if trace_sink is not None:
                    trace_sink.close()
                return
            current = self.renderer_status()
            worker_exit_code = exc.worker_exit_code if isinstance(exc, LiveVstError) else None
            self._publish_renderer_status(
                current.model_copy(
                    update={
                        "state": "unavailable" if "CoreAudio output" in str(exc) else "failed",
                        "updated_at_monotonic": time.monotonic(),
                        "message": str(exc),
                        "error_type": type(exc).__name__,
                        "error_detail": str(exc),
                        "worker_exit_code": worker_exit_code,
                    }
                )
            )
            if router is not None:
                router.close()
            if trace_sink is not None:
                trace_sink.close()

    def _lease_resident_renderer(
        self, audio_config: Any, mix_policy: Any, trace_sink: TraceSink
    ) -> _ResidentVstLease | None:
        key = self._renderer_key(audio_config, mix_policy)
        with self._renderer_lock:
            router = self._resident_vst_router
            if (
                router is None
                or self._resident_vst_key != key
                or self._resident_in_use
                or not router.is_alive
            ):
                return None
            self._resident_in_use = True
            router.set_trace_sink(trace_sink)
            return _ResidentVstLease(self, router)

    def _return_resident_renderer(self, router: Any) -> None:
        try:
            router.panic_and_wait(
                sent_at=time.monotonic(),
                reason="live_run_stopped",
            )
        finally:
            with self._renderer_lock:
                if self._resident_vst_router is router:
                    if self._resident_trace_sink is not None:
                        router.set_trace_sink(self._resident_trace_sink)
                    self._resident_in_use = False

    def _start_vst_router(
        self,
        audio_config: Any,
        mix_policy: Any,
        trace_sink: TraceSink,
        telemetry_level: TelemetryLevel,
        *,
        startup_observer: Callable[[dict[str, Any]], None] | None = None,
    ) -> Any:
        """Call modern factories with progress while preserving simple test doubles."""

        try:
            supports_progress = (
                "startup_observer" in inspect.signature(self._vst_router_factory).parameters
            )
        except (TypeError, ValueError):
            supports_progress = False
        if supports_progress:
            return self._vst_router_factory(
                audio_config,
                mix_policy,
                trace_sink,
                telemetry_level,
                startup_observer=startup_observer,
            )
        return self._vst_router_factory(audio_config, mix_policy, trace_sink, telemetry_level)

    def performance_plan(
        self,
        *,
        bundle_id: str,
        revision: str | None,
        start_measure: int | None = None,
        _bundle_root: Path | None = None,
    ) -> LivePerformancePlanResponse:
        """Resolve section authority, the first handoff, and learned start pace."""

        projection = self._projection(bundle_id, revision, _bundle_root)
        manifest = projection.source.manifest
        profile_path = (
            paths.data_root()
            / "profiles"
            / manifest.work.work_id
            / manifest.work.movement_id
            / "profile.json"
        )
        tempo_bpm = 76.0
        tempo_source = "movement_default"
        take_count = 0
        profile: Interpretation | None = None
        if profile_path.is_file():
            profile = Interpretation.model_validate_json(profile_path.read_text(encoding="utf-8"))
            take_count = profile.take_count
            if profile.base_seconds_per_quarter is not None:
                tempo_bpm = 60.0 / profile.base_seconds_per_quarter
                tempo_source = "performance_profile"

        entry = _runtime_entry_point(projection, start_measure=start_measure)
        orchestra_start_beat = (
            projection.orchestra_start_beat if entry is None else entry.score_beat
        )
        opening_section = projection.bundle.section_map.section_at(orchestra_start_beat)
        follow_beat = next(
            (
                max(orchestra_start_beat, section.start_beat)
                for section in projection.bundle.section_map.sections
                if section.end_beat > orchestra_start_beat
                and section.mode is AccompanimentMode.FOLLOW
            ),
            None,
        )
        follow_prior_take_count = 0
        if profile is not None and follow_beat is not None:
            follow_tick = round(follow_beat * profile.canonical_ppq)
            # Provenance for the UI is local to the first FOLLOW bar. It does
            # not decide authority and never borrows confidence from a distant
            # passage (the category error removed with
            # `_accompaniment_mode_for_passage`).
            first_follow_bar_end = follow_tick + 4 * profile.canonical_ppq
            follow_prior_take_count = max(
                (
                    cell.support
                    for cell in profile.cells
                    if follow_tick <= cell.score_tick < first_follow_bar_end
                ),
                default=0,
            )
        return LivePerformancePlanResponse(
            bundle_id=bundle_id,
            # An explicit rehearsal start always uses the live engine's
            # orchestra_lead_in clock until the performer joins, even when the
            # selected beat is already inside an authored FOLLOW section.
            orchestra_starts_automatically=(
                entry is not None or opening_section.mode.value == "LEAD"
            ),
            orchestra_start=_projection_position_at_beat(orchestra_start_beat, projection),
            first_solo_entry=_projection_position_at_beat(
                projection.first_solo_entry_beat, projection
            ),
            follow_start=(
                None
                if follow_beat is None
                else _projection_position_at_beat(follow_beat, projection)
            ),
            follow_prior_take_count=follow_prior_take_count,
            initial_tempo_bpm=tempo_bpm,
            tempo_source=tempo_source,
            rehearsal_take_count=take_count,
        )

    def start_replay(
        self,
        *,
        bundle_id: str,
        revision: str | None,
        config: RuntimeConfig,
        notes: tuple[PerformedNote, ...],
        start_measure: int | None = None,
        _bundle_root: Path | None = None,
    ) -> RuntimeStatus:
        """Run a deterministic, no-sound replay under the live job lifecycle."""

        config = _resolve_follow_clock(config)
        projection = self._projection(bundle_id, revision, _bundle_root)
        bundle = projection.bundle
        arrival_curve = _interpretation_arrival_curve(projection, config)
        entry = _runtime_entry_point(
            projection,
            start_measure=start_measure,
        )
        replay_notes = (
            _slice_replay_notes(notes, start_beat=entry.score_beat) if entry is not None else notes
        )

        def worker(stop_event: threading.Event) -> str:
            clock = ManualClock()
            output = CapturingOutput()
            trace_sink = JsonlTraceSink(paths.run_trace_dir(config.run_id) / "runtime.jsonl")
            follower = (
                OracleFollower()
                if all(note.score_beat is not None for note in replay_notes)
                else ReferencePitchFollower(bundle.solo_events)
            )
            engine = LiveEngine.from_bundle(
                bundle=bundle,
                config=config,
                clock=clock,
                anchor_ticks=_anchor_ticks(projection),
                follower=follower,
                output=output,
                trace_sink=trace_sink,
                arrival_curve=arrival_curve,
                pace_profile=_pace_profile(projection, config),
            )
            try:
                self._publish(
                    engine.start(start_beat=entry.score_beat if entry is not None else None),
                    projection=projection,
                )
                for note in RecordedNoteReplay(replay_notes):
                    if stop_event.is_set():
                        break
                    clock.wait_until(note.perf_time)
                    self._publish(engine.process_note(note), projection=projection)
                self._publish(engine.stop("replay_complete"), projection=projection)
                return f"Replay completed ({len(replay_notes)} note observations, no sound)"
            except Exception as exc:
                self._publish(engine.fail(str(exc)), projection=projection)
                raise
            finally:
                trace_sink.close()

        self._hardware_control.start_managed(
            kind="live_replay",
            message="Running deterministic no-sound accompaniment replay",
            target=worker,
            session_id=config.run_id,
        )
        # The worker may still be preparing. Publish an honest initial status.
        initial = RuntimeStatus(
            run_id=config.run_id,
            run_mode=config.run_mode,
            phase=RunPhase.PREPARING,
            state_word="Listening",
            monotonic_time=0,
            orchestra_tempo_bpm=config.initial_tempo_bpm,
            orchestra_volume=config.orchestra_volume,
            message=(
                f"Replay preparing from measure {entry.measure}"
                if entry is not None
                else "Replay preparing"
            ),
            coordinate_system=projection.coordinate_system,
            canonical_position=projection.canonical,
            performance_ready=projection.performance_ready,
        )
        with self._lock:
            if self._status is None or self._status.run_id != config.run_id:
                self._status = initial
            return self._status.model_copy(deep=True)

    def start_mix_audition(
        self,
        *,
        piece_id: str,
        movement: int,
        program_id: str,
        expected_revision: int,
        bundle_id: str,
        revision: str | None,
        output_name: str,
        start_tick: int,
        end_tick: int,
        tempo_bpm: float,
        volume: float = 0.20,
        _bundle_root: Path | None = None,
    ) -> RuntimeStatus:
        """Stream one authored region through the same live Yamaha/VST graph.

        This is deterministic score transport for mix iteration, not an offline
        render: MIDI is committed on the wall clock, BBCSO processes stateful
        blocks in its live child process, and CoreAudio plays those blocks while
        they are produced.
        """

        if end_tick <= start_tick:
            raise ValueError("audition end_tick must be greater than start_tick")
        if tempo_bpm <= 0:
            raise ValueError("audition tempo must be positive")
        if not 0 <= volume <= 1:
            raise ValueError("audition volume must be between zero and one")
        program = mix_store.load_program(piece_id, movement, program_id, require_current_score=True)
        if program.revision != expected_revision:
            raise ValueError("selected mix program revision changed before audition startup")
        audio_config = self._audio_config_loader()
        policy = compile_mix_policy(program, self._mix_zones_factory(audio_config))
        projection = self._projection(bundle_id, revision, _bundle_root)
        bundle = projection.bundle
        start_beat = start_tick / 960.0
        end_beat = end_tick / 960.0
        events_to_play = tuple(
            event for event in bundle.accompaniment_events if start_beat <= event.beat < end_beat
        )
        if not events_to_play:
            raise ValueError("the selected mix audition region has no orchestral notes")
        run_id = f"mix-audition-{time.time_ns()}"
        volume_commands = _LatestTempoCommand("volume")
        with self._lock:
            self._volume_commands = volume_commands

        def worker(stop_event: threading.Event) -> str:
            clock = SystemMonotonicClock()
            trace_sink = JsonlTraceSink(paths.run_trace_dir(run_id) / "runtime.jsonl")
            opened_output_port = None
            output: MultiZoneAccompanimentOutput | None = None
            vst_router: LiveVstRouter | None = None
            current_volume = volume
            try:
                yamaha_output: DeadlineAccompanimentOutput | None = None
                if output_name:
                    opened_output_port = self._output_factory(output_name)
                    device_output = MidoAccompanimentOutput(
                        output_name,
                        bundle.instrument_map,
                        port_factory=lambda _name: opened_output_port,
                        event_observer=trace_sink.write,
                        master_volume=1.0,
                        mix_level_resolver=lambda part_id, score_tick: (
                            policy.audio_gain_for_part_at(
                                "yamaha_anchor", part_id, score_tick
                            )
                        ),
                    )
                    yamaha_output = DeadlineAccompanimentOutput(
                        device_output,
                        now=clock.now,
                        output_advance_ms=0,
                    )
                # Auditions share the preloaded renderer with live FOLLOW.  A
                # second CoreMIDI virtual source with the same name is not only
                # wasteful; REAPER may remain subscribed to the resident source
                # and never receive events from the duplicate.
                vst_router = self._lease_resident_renderer(audio_config, policy, trace_sink)
                if vst_router is None:
                    vst_router = self._start_vst_router(
                        audio_config,
                        policy,
                        trace_sink,
                        TelemetryLevel.COUNTERS,
                    )
                output = MultiZoneAccompanimentOutput(yamaha_output, vst_router)
                # Audition is part of the same performance surface: the Sound
                # control must mean the same thing here as it does in FOLLOW.
                output.set_master_volume(volume, sent_at=clock.now())

                beat_seconds = 60.0 / tempo_bpm
                preroll_seconds = 0.2
                origin = clock.now() + preroll_seconds
                end_time = origin + (end_beat - start_beat) * beat_seconds
                pending = deque(events_to_play)
                last_publish = 0.0
                while not stop_event.is_set() and clock.now() < end_time + 0.75:
                    now = clock.now()
                    volume_command = volume_commands.next_pending()
                    if volume_command is not None:
                        command_revision, requested_volume = volume_command
                        output.set_master_volume(requested_volume, sent_at=now)
                        current_volume = requested_volume
                        volume_commands.acknowledge(command_revision)
                    score_beat = min(
                        end_beat,
                        max(start_beat, start_beat + (now - origin) / beat_seconds),
                    )
                    score_tick = max(start_tick, min(end_tick, round(score_beat * 960)))
                    output.apply_mix_automation(score_tick, sent_at=now)
                    while pending:
                        event = pending[0]
                        target = origin + (event.beat - start_beat) * beat_seconds
                        if target - now > 0.1:
                            break
                        pending.popleft()
                        output.send(
                            ScheduledAccompanimentEvent(
                                event=event,
                                perf_time=target,
                                section_mode=AccompanimentMode.LEAD,
                                tempo_bpm=tempo_bpm,
                                duration_seconds=event.duration_beats * beat_seconds,
                                committed_at=now,
                            ),
                            sent_at=now,
                        )
                    if now - last_publish >= 0.02:
                        self._publish(
                            RuntimeStatus(
                                run_id=run_id,
                                run_mode=RunMode.REHEARSAL,
                                phase=RunPhase.ACTIVE,
                                state_word=LiveStateWord.LEADING,
                                monotonic_time=now,
                                score_beat=score_beat,
                                tempo_bpm=tempo_bpm,
                                orchestra_tempo_bpm=tempo_bpm,
                                orchestra_volume=current_volume,
                                section_mode=AccompanimentMode.LEAD,
                                message="Live mix audition",
                                coordinate_system=projection.coordinate_system,
                                canonical_position=projection.canonical,
                                performance_ready=projection.performance_ready,
                            ),
                            projection=projection,
                        )
                        last_publish = now
                    stop_event.wait(0.002)
                output.panic(sent_at=clock.now(), reason="mix_audition_complete")
                completed = RuntimeStatus(
                    run_id=run_id,
                    run_mode=RunMode.REHEARSAL,
                    phase=RunPhase.COMPLETED,
                    state_word=LiveStateWord.SILENT,
                    monotonic_time=clock.now(),
                    score_beat=end_beat,
                    orchestra_tempo_bpm=tempo_bpm,
                    orchestra_volume=current_volume,
                    message="Mix audition complete",
                    coordinate_system=projection.coordinate_system,
                    canonical_position=projection.canonical,
                    performance_ready=projection.performance_ready,
                )
                self._publish(completed, projection=projection)
                return "Mix audition complete"
            except Exception as exc:
                volume_commands.close(str(exc))
                self._publish(
                    RuntimeStatus(
                        run_id=run_id,
                        run_mode=RunMode.REHEARSAL,
                        phase=RunPhase.FAILED,
                        state_word=LiveStateWord.SILENT,
                        monotonic_time=clock.now(),
                        orchestra_tempo_bpm=tempo_bpm,
                        message=str(exc),
                    ),
                    projection=projection,
                )
                raise
            finally:
                volume_commands.close()
                if output is not None:
                    output.close()
                    vst_router = None
                elif vst_router is not None:
                    vst_router.close()
                elif opened_output_port is not None:
                    close = getattr(opened_output_port, "close", None)
                    if callable(close):
                        close()
                trace_sink.close()
                with self._lock:
                    if self._volume_commands is volume_commands:
                        self._volume_commands = None

        try:
            self._hardware_control.start_managed(
                kind="mix_audition",
                message=(
                    f"Auditioning live mix through {output_name} and the room renderer"
                    if output_name
                    else "Auditioning live mix through the room renderer"
                ),
                target=worker,
                session_id=run_id,
            )
        except Exception:
            volume_commands.close()
            with self._lock:
                if self._volume_commands is volume_commands:
                    self._volume_commands = None
            raise
        initial = RuntimeStatus(
            run_id=run_id,
            run_mode=RunMode.REHEARSAL,
            phase=RunPhase.PREPARING,
            state_word=LiveStateWord.LISTENING,
            monotonic_time=time.monotonic(),
            score_beat=start_beat,
            orchestra_tempo_bpm=tempo_bpm,
            orchestra_volume=volume,
            message="Preparing live VST mix audition",
            coordinate_system=projection.coordinate_system,
            canonical_position=projection.canonical,
            performance_ready=projection.performance_ready,
        )
        with self._lock:
            self._status = initial
        return initial.model_copy(deep=True)

    def _resolve_mix_policy(
        self, config: RuntimeConfig, audio_config: Any
    ) -> tuple[Any, RuntimeConfig]:
        """Resolve the spatial mix identically for every live entry point.

        Rehearse-from-a-bar and perform-from-the-top are one runtime; the mix
        must not depend on which one launched the run. A single ``mix_enabled``
        switch (default on) governs both, the piece's default program is used
        unless one is named, and a stale client revision pin is logged rather
        than honoured. Returns the compiled policy (or ``None``) and the config
        stamped with the resolved program identity.

        The mix is a spatial refinement, never a precondition: performance is the
        one capability that must always work. So *any* failure to resolve or
        compile it -- a missing program, a stale score identity, a malformed
        region -- degrades to no mix and the run proceeds, rather than aborting
        the performance over data quality.
        """

        if not config.mix_enabled:
            return None, config
        program_id = config.mix_program_id or _DEFAULT_MIX_PROGRAM_ID
        try:
            mix_program = mix_store.load_program(
                "chopin_op11", 2, program_id, require_current_score=True
            )
            if (
                config.mix_program_revision is not None
                and mix_program.revision != config.mix_program_revision
            ):
                logger.warning(
                    "Requested mix revision %s but current is %s; using current",
                    config.mix_program_revision,
                    mix_program.revision,
                )
            mix_policy = compile_mix_policy(
                mix_program,
                self._mix_zones_factory(audio_config),
                dispatch_horizon_ms=config.dispatch_horizon_ms,
                planning_horizon_ms=config.planning_horizon_ms,
            )
        except Exception as error:  # noqa: BLE001 - the mix must never block a performance
            logger.warning(
                "Spatial mix %r unavailable (%s); performing without it",
                program_id,
                error,
            )
            return None, config
        config = config.model_copy(
            update={
                "mix_program_id": program_id,
                "mix_program_revision": mix_program.revision,
            }
        )
        return mix_policy, config

    def start_follow(
        self,
        *,
        bundle_id: str,
        revision: str | None,
        input_name: str,
        output_name: str | None,
        config: RuntimeConfig,
        follower_method: str = "pthmm",
        start_measure: int | None = None,
        duration_seconds: float | None = None,
        _bundle_root: Path | None = None,
    ) -> RuntimeStatus:
        """Start Yamaha MIDI -> Matchmaker -> FOLLOW scheduler -> Yamaha MIDI.

        Every run is captured under ``config.run_id``. Capture stays ephemeral
        unless a caller explicitly promotes that recording into the take store;
        rehearsal and performance therefore share one engine and one recording
        path without making every performance training evidence.
        """

        if duration_seconds is not None and duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        config = _resolve_follow_clock(config)
        audio_config = self._audio_config_loader()
        mix_policy, config = self._resolve_mix_policy(config, audio_config)
        logger.info("Starting live FOLLOW run with %r clock", config.follow_clock)
        projection = self._projection(bundle_id, revision, _bundle_root)
        bundle = projection.bundle
        arrival_curve = _interpretation_arrival_curve(projection, config)
        entry = _runtime_entry_point(
            projection,
            start_measure=start_measure,
        )
        initial_reference_beat = entry.follower_prior_reference_beat if entry is not None else None
        score_file = projection.follower_reference_path
        if find_spec("matchmaker") is None:
            raise ImportError("Matchmaker live following requires `uv sync --extra live`.")
        tempo_commands = _LatestTempoCommand()
        volume_commands = _LatestTempoCommand("volume")
        output_advance_commands = _LatestTempoCommand("output advance")
        with self._lock:
            self._tempo_commands = tempo_commands
            self._volume_commands = volume_commands
            self._output_advance_commands = output_advance_commands

        initial = RuntimeStatus(
            run_id=config.run_id,
            run_mode=config.run_mode,
            phase=RunPhase.PREPARING,
            state_word="Listening",
            monotonic_time=time.monotonic(),
            orchestra_tempo_bpm=config.initial_tempo_bpm,
            orchestra_volume=config.orchestra_volume,
            orchestra_renderer_state="loading" if mix_policy is not None else "not_loaded",
            message=(
                "Preparing the orchestra renderer in the background"
                if mix_policy is not None
                else (
                    f"Count-off preparing for measure {entry.measure}"
                    if entry is not None
                    else "Live FOLLOW preparing"
                )
            ),
            coordinate_system=projection.coordinate_system,
            canonical_position=projection.canonical,
            performance_ready=projection.performance_ready,
        )
        with self._lock:
            self._status = initial

        def publish_renderer_startup(progress: dict[str, Any]) -> None:
            state = str(progress.get("state", "loading"))
            loaded = int(progress.get("loaded", 0))
            total = int(progress.get("total", 0))
            instrument_id = progress.get("instrument_id")
            renderer_name = (
                "REAPER"
                if any(
                    getattr(zone, "renderer", "pedalboard") == "reaper"
                    for zone in audio_config.zones
                )
                else "BBCSO"
            )
            if state == "opening_audio":
                message = str(
                    progress.get("detail")
                    or (f"{renderer_name} · opening the LG soundbar audio stream")
                )
            elif state == "ready":
                message = str(
                    progress.get("detail")
                    or (f"{renderer_name} orchestra ready on the LG soundbar")
                )
            elif state == "failed":
                message = f"{renderer_name} failed: {progress.get('detail', 'unknown error')}"
            elif instrument_id:
                instrument_label = str(instrument_id).replace("_", " ")
                message = (
                    f"Loading {renderer_name} {min(loaded + 1, total)} of {total}"
                    f" · {instrument_label}"
                )
            else:
                message = f"Starting the {renderer_name} orchestra renderer"
            with self._lock:
                current = self._status
            if (
                current is None
                or current.run_id != config.run_id
                or current.phase is not RunPhase.PREPARING
            ):
                return
            self._publish(
                current.model_copy(
                    update={
                        "monotonic_time": time.monotonic(),
                        "orchestra_renderer_state": state,
                        "orchestra_renderer_zone_id": progress.get("zone_id"),
                        "orchestra_renderer_instrument_id": instrument_id,
                        "orchestra_renderer_loaded_instruments": loaded,
                        "orchestra_renderer_total_instruments": total,
                        "message": message,
                    }
                ),
                projection=projection,
            )

        def worker(stop_event: threading.Event) -> str:
            clock = SystemMonotonicClock()
            follower: ScoreFollower | None = None
            raw_follower: MatchmakerStreamFollower | ProcessFollower | None = None
            output: MultiZoneAccompanimentOutput | None = None
            vst_router: LiveVstRouter | None = None
            engine: LiveEngine | None = None
            trace_sink: JsonlTraceSink | None = None
            opened_output_port = None
            captured_events: list[CapturedMidiEvent] = []
            input_reader: threading.Thread | None = None
            try:
                trace_sink = JsonlTraceSink(paths.run_trace_dir(config.run_id) / "runtime.jsonl")
                if _follower_in_subprocess():
                    # The PTHMM holds the GIL for tens of ms per note and that
                    # cost grows through a performance, starving every other
                    # real-time loop. Isolate it on its own core; the observe()
                    # contract is unchanged (Decision 0011).
                    raw_follower = ProcessFollower(
                        FollowerSpec(
                            score_file=str(score_file),
                            method=follower_method,
                            tempo_bpm=config.initial_tempo_bpm,
                            minimum_lock_updates=2 if entry is not None else 3,
                            initial_reference_beat=initial_reference_beat,
                        )
                    )
                else:
                    raw_follower = MatchmakerStreamFollower(
                        score_file,
                        method=follower_method,
                        tempo_bpm=config.initial_tempo_bpm,
                        minimum_lock_updates=2 if entry is not None else 3,
                        initial_reference_beat=initial_reference_beat,
                    )
                follower = _runtime_follower(raw_follower, projection)
                # Recover a tracker that mis-locks early and would otherwise stay
                # tens of beats behind for the whole take (Decision 0018). Wraps
                # the canonical follower so the watchdog watches the score beat
                # the orchestra actually acts on, and re-searches the raw tracker
                # underneath. Skipped if the tracker cannot re-search.
                relocalize = getattr(raw_follower, "relocalize", None)
                if config.follower_relock_enabled and relocalize is not None:
                    follower = RelockingFollower(
                        follower,
                        relocalize=relocalize,
                        window_seconds=config.follower_relock_window_seconds,
                        min_updates=config.follower_relock_min_updates,
                        min_advance_beats=config.follower_relock_min_advance_beats,
                        cooldown_seconds=config.follower_relock_cooldown_seconds,
                    )
                # ``output_name`` may be empty: the performer chose no MIDI output,
                # so the orchestra sounds only through the live audio zones (e.g.
                # BBCSO to a room speaker) with no MIDI copy to double it. Open the
                # port only when one was named.
                yamaha_output: DeadlineAccompanimentOutput | None = None
                if output_name:
                    opened_output_port = self._output_factory(output_name)
                    device_output = MidoAccompanimentOutput(
                        output_name,
                        bundle.instrument_map,
                        port_factory=lambda _name: opened_output_port,
                        event_observer=trace_sink.write,
                        master_volume=config.orchestra_volume,
                        count_off_channel=config.count_off_channel,
                        mix_level_resolver=(
                            (
                                lambda part_id, score_tick: mix_policy.audible_gain_for_part_at(
                                    "yamaha_anchor",
                                    part_id,
                                    score_tick,
                                    floor=config.min_audible_orchestra_gain,
                                )
                            )
                            if mix_policy is not None
                            else None
                        ),
                    )
                    yamaha_output = DeadlineAccompanimentOutput(
                        device_output,
                        now=clock.now,
                        output_advance_ms=config.output_advance_ms,
                    )
                if mix_policy is not None:
                    vst_router = self._lease_resident_renderer(audio_config, mix_policy, trace_sink)
                    if vst_router is not None:
                        resident = self.renderer_status()
                        publish_renderer_startup(
                            {
                                "state": "ready",
                                "zone_id": resident.zone_id,
                                "instrument_id": None,
                                "loaded": resident.loaded_instruments,
                                "total": resident.total_instruments,
                            }
                        )
                    if vst_router is None:
                        vst_router = self._start_vst_router(
                            audio_config,
                            mix_policy,
                            trace_sink,
                            config.telemetry_level,
                            startup_observer=publish_renderer_startup,
                        )
                if yamaha_output is None and vst_router is None:
                    raise LiveVstError(
                        "no orchestra output: choose a MIDI output, or configure a "
                        "live audio zone the mix routes to"
                    )
                output = MultiZoneAccompanimentOutput(yamaha_output, vst_router)
                # The Yamaha adapter receives the initial master level in its
                # constructor; live audio zones must receive the same value so
                # the global orchestra control has one meaning on every route.
                output.set_master_volume(config.orchestra_volume, sent_at=clock.now())
                engine = LiveEngine.from_bundle(
                    bundle=bundle,
                    config=config,
                    clock=clock,
                    follower=follower,
                    output=output,
                    trace_sink=trace_sink,
                    anchor_ticks=_anchor_ticks(projection),
                    arrival_curve=arrival_curve,
                    pace_profile=_pace_profile(projection, config),
                )
                with _keep_awake(), self._input_factory(input_name) as input_port, RealtimeGC():
                    # A dedicated thread stamps note arrivals so a chord's notes
                    # share one perf_time regardless of how long the follower,
                    # scheduler, or UI publish take. The processing loop below only
                    # drains what that thread has already timestamped.
                    input_queue: deque[tuple[float, int, int]] = deque()
                    # Per-stage timing: cadence (interval between iterations, i.e.
                    # stalls) and work (compute per iteration, i.e. hot spots),
                    # emitted every second as loop_timing trace rows so a load test
                    # shows whether any stage lags or has high compute variance.
                    input_profiler = LoopProfiler("midi_input", clock, trace_sink)
                    processing_profiler = LoopProfiler("processing", clock, trace_sink)
                    process_note_profiler = LoopProfiler("process_note", clock, trace_sink)
                    publish_profiler = LoopProfiler("publish", clock, trace_sink)
                    input_reader = threading.Thread(
                        target=_read_input_into_queue,
                        args=(
                            input_port,
                            clock,
                            input_queue,
                            captured_events,
                            stop_event,
                            input_profiler,
                        ),
                        kwargs={
                            # Reopen the input by name if the device drops mid-run
                            # instead of silently dropping the soloist thereafter.
                            "input_name": input_name,
                            "input_factory": self._input_factory,
                        },
                        name="rubato-midi-input",
                        daemon=True,
                    )
                    input_reader.start()
                    recording_started_at = clock.now()
                    if entry is not None:
                        self._publish(
                            engine.start(
                                start_beat=entry.score_beat,
                                orchestra_lead_in=True,
                            ),
                            projection=projection,
                        )
                    else:
                        self._publish(engine.start(), projection=projection)
                    while not stop_event.is_set():
                        if (
                            duration_seconds is not None
                            and clock.now() - recording_started_at >= duration_seconds
                        ):
                            break
                        command = tempo_commands.next_pending()
                        if command is not None:
                            revision, tempo_bpm = command
                            self._publish(
                                engine.set_lead_tempo(tempo_bpm),
                                projection=projection,
                            )
                            tempo_commands.acknowledge(revision)
                        volume_command = volume_commands.next_pending()
                        if volume_command is not None:
                            revision, volume = volume_command
                            self._publish(
                                engine.set_orchestra_volume(volume),
                                projection=projection,
                            )
                            volume_commands.acknowledge(revision)
                        advance_command = output_advance_commands.next_pending()
                        if advance_command is not None:
                            revision, advance_ms = advance_command
                            self._publish(
                                engine.set_output_advance(advance_ms),
                                projection=projection,
                            )
                            output_advance_commands.acknowledge(revision)
                        # Drain the notes the reader thread already timestamped at
                        # arrival. process_note is per note (reactive output fires
                        # inside it); publish once per batch so a dense passage does
                        # not flood the UI socket and slow the loop.
                        iter_start = clock.now()
                        latest_status: RuntimeStatus | None = None
                        while input_queue:
                            perf_time, note, velocity = input_queue.popleft()
                            note_start = clock.now()
                            latest_status = engine.process_note(
                                PerformedNote(
                                    # Match the scheduler's monotonic clock domain.
                                    # Relative recording timestamps belong only to
                                    # offline replay; live deadlines are absolute.
                                    perf_time=perf_time,
                                    pitch=note,
                                    velocity=velocity,
                                )
                            )
                            process_note_profiler.record(clock.now() - note_start)
                        if latest_status is not None:
                            publish_start = clock.now()
                            self._publish(latest_status, projection=projection)
                            publish_profiler.record(clock.now() - publish_start)
                        tick_status = engine.tick()
                        if mix_policy is not None:
                            automation_time = clock.now()
                            automation_beat = engine.current_score_beat(at=automation_time)
                            if automation_beat is not None:
                                output.apply_mix_automation(
                                    _bounded_runtime_score_tick(automation_beat, projection),
                                    sent_at=automation_time,
                                )
                        self._publish(
                            tick_status,
                            only_if_changed=True,
                            projection=projection,
                        )
                        processing_profiler.record(clock.now() - iter_start)
                        stop_event.wait(0.002)
                    stop_event.set()
                    input_reader.join(timeout=1.0)
                self._publish(engine.stop(), projection=projection)
                return "Live FOLLOW run stopped"
            except Exception as exc:
                tempo_commands.close(str(exc))
                volume_commands.close(str(exc))
                output_advance_commands.close(str(exc))
                if engine is not None:
                    self._publish(engine.fail(str(exc)), projection=projection)
                else:
                    self._publish(
                        RuntimeStatus(
                            run_id=config.run_id,
                            run_mode=config.run_mode,
                            phase=RunPhase.FAILED,
                            state_word="Silent",
                            monotonic_time=clock.now(),
                            message=str(exc),
                        ),
                        projection=projection,
                    )
                raise
            finally:
                # Persist even when the follower or scheduler failed: the
                # partial performance and its run trace are often the best
                # evidence for debugging the failure.
                stop_event.set()
                if input_reader is not None:
                    input_reader.join(timeout=1.0)
                capture_error: Exception | None = None
                try:
                    _write_captured_performance(config.run_id, captured_events)
                except Exception as exc:
                    capture_error = exc
                    logger.exception(
                        "Could not persist live performance %s",
                        config.run_id,
                    )
                tempo_commands.close()
                volume_commands.close()
                output_advance_commands.close()
                if raw_follower is not None:
                    raw_follower.close()
                if output is not None:
                    output.close()
                    vst_router = None
                elif vst_router is not None:
                    vst_router.close()
                elif opened_output_port is not None:
                    close = getattr(opened_output_port, "close", None)
                    if callable(close):
                        close()
                if trace_sink is not None:
                    trace_sink.close()
                with self._lock:
                    if self._tempo_commands is tempo_commands:
                        self._tempo_commands = None
                    if self._volume_commands is volume_commands:
                        self._volume_commands = None
                    if self._output_advance_commands is output_advance_commands:
                        self._output_advance_commands = None
                if capture_error is not None:
                    raise capture_error

        self._hardware_control.start_managed(
            kind="live_follow",
            message=(
                f"Following {input_name} and accompanying on {output_name or 'live audio zones'}"
            ),
            target=worker,
            session_id=config.run_id,
        )
        return initial.model_copy(deep=True)

    def stop(self) -> RuntimeStatus | None:
        self._hardware_control.stop()
        status = self._hardware_control.wait_until_idle(timeout=10.0)
        if status.running:
            raise RuntimeError("Live performance did not stop in time")
        return self.status()

    def update_tempo(self, tempo_bpm: float) -> RuntimeStatus:
        """Apply the latest live LEAD tempo on the engine thread and acknowledge it."""

        if tempo_bpm <= 0:
            raise ValueError("tempo_bpm must be positive")
        with self._lock:
            status = self._status
            commands = self._tempo_commands
            if (
                status is None
                or commands is None
                or status.phase
                in {
                    RunPhase.STOPPING,
                    RunPhase.COMPLETED,
                    RunPhase.FAILED,
                }
            ):
                raise RuntimeError("No live performance is running")
        commands.submit(tempo_bpm)
        applied = self.status()
        if applied is None:
            raise RuntimeError("Live performance stopped before tempo changed")
        return applied

    def update_volume(self, volume: float) -> RuntimeStatus:
        """Apply the latest orchestra mix level on the engine/output thread."""

        if not 0 <= volume <= 1:
            raise ValueError("volume must be between 0 and 1")
        with self._lock:
            status = self._status
            commands = self._volume_commands
            if (
                status is None
                or commands is None
                or status.phase
                in {
                    RunPhase.STOPPING,
                    RunPhase.COMPLETED,
                    RunPhase.FAILED,
                }
            ):
                raise RuntimeError("No live performance is running")
        commands.submit(volume)
        applied = self.status()
        if applied is None:
            raise RuntimeError("Live performance stopped before volume changed")
        return applied

    def update_output_advance(self, output_advance_ms: float) -> RuntimeStatus:
        """Apply the latest Yamaha output-advance on the engine/output thread.

        Bounded to the dispatch horizon so calibration cannot outrun the freeze
        window; the engine re-validates before applying.
        """

        if not 0 <= output_advance_ms <= 100:
            raise ValueError("output_advance_ms must be between 0 and 100")
        with self._lock:
            status = self._status
            commands = self._output_advance_commands
            if (
                status is None
                or commands is None
                or status.phase
                in {
                    RunPhase.STOPPING,
                    RunPhase.COMPLETED,
                    RunPhase.FAILED,
                }
            ):
                raise RuntimeError("No live performance is running")
        commands.submit(output_advance_ms)
        applied = self.status()
        if applied is None:
            raise RuntimeError("Live performance stopped before output advance changed")
        return applied

    def calibrate_output_latency(
        self,
        *,
        input_name: str,
        output_name: str,
        tempo_bpm: float = 90.0,
        beats: int = 16,
        metronome_volume: float = 0.3,
    ) -> CalibrationResult:
        """Measure the Yamaha output+input round-trip with a metronome loopback.

        Runs under the shared hardware lock (no overlap with follow/record/play)
        and blocks until the short click sequence finishes. The performer plays
        one key on each click; the median send->receive offset suggests the
        output-advance to cancel.
        """

        result_box: dict[str, CalibrationResult] = {}
        if not 0.0 <= metronome_volume <= 1.0:
            raise ValueError("metronome_volume must be between zero and one")

        def worker(stop_event: threading.Event) -> str:
            output_port = self._output_factory(output_name)
            input_port = self._input_factory(input_name)
            try:
                result_box["result"] = measure_output_latency(
                    output_port=output_port,
                    input_port=input_port,
                    tempo_bpm=tempo_bpm,
                    beats=beats,
                    click_velocity=round(127 * metronome_volume),
                    stop_event=stop_event,
                )
            finally:
                for port in (input_port, output_port):
                    close = getattr(port, "close", None)
                    if callable(close):
                        close()
            return "Latency calibration complete"

        self._hardware_control.start_managed(
            kind="latency_calibration",
            message="Calibrating Yamaha output latency",
            target=worker,
        )
        # Bound the wait by the click sequence duration plus generous slack.
        timeout = beats * (60.0 / tempo_bpm) + 10.0
        self._hardware_control.wait_until_idle(timeout=timeout)
        result = result_box.get("result")
        if result is None:
            raise RuntimeError("Latency calibration did not complete")
        return result

    def _projection(
        self, bundle_id: str, revision: str | None, explicit_root: Path | None
    ) -> ProvisionalRuntimeProjection:
        return project_bundle_v2_to_provisional_runtime(
            bundle_id=bundle_id,
            revision=revision,
            registry=self._bundle_registry,
            explicit_root=explicit_root,
        )

    def _publish(
        self,
        status: RuntimeStatus,
        *,
        only_if_changed: bool = False,
        projection: ProvisionalRuntimeProjection | None = None,
    ) -> None:
        with self._lock:
            current = self._status
        if status.phase in {RunPhase.COMPLETED, RunPhase.STOPPING}:
            status = status.model_copy(
                update={
                    "orchestra_renderer_state": "not_loaded",
                    "orchestra_renderer_instrument_id": None,
                }
            )
        elif status.phase is RunPhase.FAILED:
            status = status.model_copy(update={"orchestra_renderer_state": "failed"})
        elif (
            current is not None
            and current.run_id == status.run_id
            and status.orchestra_renderer_state == "not_loaded"
            and current.orchestra_renderer_state != "not_loaded"
        ):
            status = status.model_copy(
                update={
                    "orchestra_renderer_state": current.orchestra_renderer_state,
                    "orchestra_renderer_zone_id": current.orchestra_renderer_zone_id,
                    "orchestra_renderer_instrument_id": current.orchestra_renderer_instrument_id,
                    "orchestra_renderer_loaded_instruments": (
                        current.orchestra_renderer_loaded_instruments
                    ),
                    "orchestra_renderer_total_instruments": (
                        current.orchestra_renderer_total_instruments
                    ),
                }
            )
        if projection is not None:
            score_position = _runtime_score_position(status, projection)
            status = status.model_copy(
                update={
                    "coordinate_system": projection.coordinate_system,
                    "canonical_position": projection.canonical,
                    "performance_ready": projection.performance_ready,
                    "score_position": score_position,
                    "message": status.message
                    or "PROVISIONAL: MIDI performance coordinates; not performance-ready",
                }
            )
        with self._lock:
            self._status = status
            previous = self._last_published_status
            if only_if_changed and previous is not None:
                comparable = (status.phase, status.score_beat, status.section_mode)
                old = (previous.phase, previous.score_beat, previous.section_mode)
                if comparable == old:
                    return
                same_state = (
                    status.phase == previous.phase and status.section_mode == previous.section_mode
                )
                if same_state and status.monotonic_time - previous.monotonic_time < 0.05:
                    return
            self._last_published_status = status
        # Hand off to the UI thread; never serialize or fan out inline.
        self._broadcaster.submit(LiveRuntimeStatusEvent(type="runtime:status", status=status))


live_runtime = LiveRuntimeManager()


def _runtime_score_position(
    status: RuntimeStatus,
    projection: ProvisionalRuntimeProjection,
) -> RuntimeScorePosition | None:
    if status.score_beat is None:
        return None
    manifest = projection.source.manifest
    if (manifest.work.work_id, manifest.work.movement_id) != ("chopin_op11", "2"):
        return None
    display_projection = score_projection("chopin_op11", 2)
    if projection.coordinate_system == "canonical_score":
        timeline_position = display_projection.timeline.position_at(
            _bounded_runtime_score_tick(status.score_beat, projection)
        )
        source_tick = display_projection.source_tick_at_score_tick(timeline_position.score_tick)
        position = display_projection.position_at_source_tick(source_tick)
    else:
        position = display_projection.position_at_source_tick(round(status.score_beat * 960))
    return RuntimeScorePosition(
        **position.__dict__,
        mapping_id=display_projection.mapping_id,
        mapping_review_state=display_projection.mapping_review_state.value,
        canonical_position=display_projection.canonical_position,
    )


def _bounded_runtime_score_tick(
    score_beat: float,
    projection: ProvisionalRuntimeProjection,
) -> int:
    """Return a valid automation/display tick even just past the final barline."""

    score_tick = max(0, round(score_beat * 960))
    manifest = projection.source.manifest
    if projection.coordinate_system == "canonical_score" and (
        manifest.work.work_id,
        manifest.work.movement_id,
    ) == ("chopin_op11", "2"):
        end_tick = score_projection("chopin_op11", 2).timeline.end_tick
        return min(score_tick, end_tick - 1)
    return score_tick


def _projection_position_at_beat(
    score_beat: float,
    projection: ProvisionalRuntimeProjection,
) -> RuntimeScorePosition | None:
    return _runtime_score_position(
        RuntimeStatus(
            run_id="live-plan",
            run_mode="performance",
            phase="preparing",
            state_word="Listening",
            monotonic_time=0,
            score_beat=score_beat,
        ),
        projection,
    )


_CanonicalFollower = CanonicalFollower
_runtime_follower = runtime_follower


__all__ = ["LiveRuntimeManager", "live_runtime"]
