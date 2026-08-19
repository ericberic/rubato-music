"""Bit-exact closed-loop evaluation through the production accompaniment path."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from statistics import fmean
from typing import Literal

from pydantic import ConfigDict, Field

from aimusic.accompaniment.following import (
    OracleFollower,
    PerformedNote,
    ReferencePitchFollower,
    ScoreFollower,
)
from aimusic.accompaniment.live_engine import LiveEngine
from aimusic.accompaniment.midi_output import DeadlineAccompanimentOutput
from aimusic.accompaniment.offline_alignment import extract_note_events
from aimusic.accompaniment.runtime_contracts import (
    MidiOutputTrace,
    RuntimeConfig,
    StrictModel,
)
from aimusic.accompaniment.runtime_io import (
    ManualClock,
    MemoryTraceSink,
    TraceSink,
)
from aimusic.accompaniment.runtime_projection import (
    ProvisionalRuntimeProjection,
    runtime_follower,
)
from aimusic.accompaniment.scheduler import (
    ArrivalTimeCurve,
    ReferenceWarpArrivalCurve,
    ScheduledAccompanimentEvent,
)
from aimusic.accompaniment.score_bundle import ScoreBundle
from aimusic.accompaniment.tempo_model import TempoModel

FollowerFactory = Callable[[], ScoreFollower]
TempoModelFactory = Callable[[], TempoModel]
METRIC_VERSION = "closed-loop-onset-v1"
FollowerEvidence = Literal[
    "unspecified",
    "synthetic_oracle",
    "deterministic_test_follower",
]


class ClosedLoopEvaluationConfig(StrictModel):
    """Versioned deterministic event-loop and metric configuration."""

    schema_version: Literal[1] = 1
    seed: int = 0
    control_tick_seconds: float = Field(default=0.002, gt=0, le=0.1)
    tail_seconds: float | None = Field(default=None, ge=0, le=30)


class SoloTimingPoint(StrictModel):
    """Known aligned solo time at one canonical score position."""

    score_beat: float = Field(ge=0)
    perf_time: float = Field(ge=0)


class DeliveredEventMetric(StrictModel):
    """One orchestra note observed after the deadline-output seam."""

    event_id: str
    score_beat: float = Field(ge=0)
    pitch: int | None = Field(default=None, ge=0, le=127)
    target_perf_time: float
    delivered_perf_time: float


class BeatOnsetMetric(StrictModel):
    """First orchestral onset and its error for one canonical quarter beat."""

    score_beat: float = Field(ge=0)
    event_ids: tuple[str, ...]
    solo_perf_time: float
    orchestra_perf_time: float
    signed_error_ms: float
    abs_error_ms: float = Field(ge=0)


class LandmarkOnsetMetric(BeatOnsetMetric):
    """Named score landmark using the same versioned beat metric."""

    name: str


class AggregateOnsetMetric(StrictModel):
    """Small deterministic aggregate over a declared metric collection."""

    count: int = Field(ge=0)
    mean_signed_error_ms: float | None = None
    mean_abs_error_ms: float | None = Field(default=None, ge=0)
    max_abs_error_ms: float | None = Field(default=None, ge=0)


class ClosedLoopEvaluationReport(StrictModel):
    """Stable artifact used to compare runtime variants on identical input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[3] = 3
    metric_version: Literal["closed-loop-onset-v1"] = METRIC_VERSION
    execution_domain: Literal["deterministic_virtual_clock"] = "deterministic_virtual_clock"
    hardware_delivery_measured: Literal[False] = False
    follower_evidence: FollowerEvidence
    seed: int
    input_digest: str
    runtime_config: RuntimeConfig
    evaluation_config: ClosedLoopEvaluationConfig
    arrival_curve: str
    delivered_events: tuple[DeliveredEventMetric, ...]
    per_beat: tuple[BeatOnsetMetric, ...]
    per_beat_aggregate: AggregateOnsetMetric
    landmarks: tuple[LandmarkOnsetMetric, ...]
    landmark_aggregate: AggregateOnsetMetric


@dataclass(frozen=True)
class _DeliveredEvent:
    delivered_at: float
    event: ScheduledAccompanimentEvent


class _DeterministicMidiOutput:
    """No-sound output delegate that records the virtual adapter boundary."""

    def __init__(self, clock: ManualClock, trace_sink: TraceSink) -> None:
        self._clock = clock
        self._trace_sink = trace_sink
        self.delivered: list[_DeliveredEvent] = []

    def send(self, event: ScheduledAccompanimentEvent, *, sent_at: float) -> None:
        delivered_at = self._clock.now()
        self.delivered.append(_DeliveredEvent(delivered_at=delivered_at, event=event))
        self._trace_sink.write(
            MidiOutputTrace(
                type="midi_output",
                monotonic_time=delivered_at,
                action="note_on",
                event_id=event.event.event_id,
                pitch=event.event.pitch,
                velocity=event.event.velocity,
                target_perf_time=event.perf_time,
                committed_at=event.committed_at,
                requested_send_time=sent_at,
                output_lateness_ms=(delivered_at - event.perf_time) * 1000,
            )
        )

    def cancel_pending(self, event_ids: Iterable[str]) -> bool:
        return not tuple(event_ids)

    def retime_release(self, event_id: str, *, scheduled_at: float) -> bool:
        _ = scheduled_at
        return any(delivered.event.event.event_id == event_id for delivered in self.delivered)

    def panic(self, *, sent_at: float, reason: str) -> None:
        self._trace_sink.write(
            MidiOutputTrace(
                type="midi_output",
                monotonic_time=self._clock.now(),
                action="panic",
                requested_send_time=sent_at,
                reason=reason,
            )
        )

    def set_master_volume(self, volume: float, *, sent_at: float) -> None:
        _ = (volume, sent_at)

    def set_output_advance(self, output_advance_ms: float) -> None:
        _ = output_advance_ms

    def close(self) -> None:
        pass


def evaluate_closed_loop(
    *,
    bundle: ScoreBundle,
    notes: Iterable[PerformedNote],
    follower_factory: FollowerFactory,
    runtime_config: RuntimeConfig,
    evaluation_config: ClosedLoopEvaluationConfig | None = None,
    solo_timing: Iterable[SoloTimingPoint] | None = None,
    landmarks: Mapping[str, float] | None = None,
    tempo_model_factory: TempoModelFactory | None = None,
    trace_sink: TraceSink | None = None,
    anchor_ticks: Iterable[int] = (),
    arrival_curve: ArrivalTimeCurve | None = None,
) -> ClosedLoopEvaluationReport:
    """Replay one aligned take through Follower→Tempo→Scheduler→deadline output.

    There are no wall-clock reads, sleeps, or threads. Recorded timestamps are
    translated to a zero-based virtual clock exactly as live replay translates
    file-relative time into the process monotonic domain.
    """

    config = evaluation_config or ClosedLoopEvaluationConfig()
    ordered_notes = tuple(
        note
        for _, note in sorted(
            enumerate(notes),
            key=lambda item: (item[1].perf_time, item[0]),
        )
    )
    if not ordered_notes:
        raise ValueError("closed-loop evaluation requires at least one performed note")
    input_origin = ordered_notes[0].perf_time
    normalized_notes = tuple(
        replace(note, perf_time=note.perf_time - input_origin) for note in ordered_notes
    )
    timing_points = _normalized_solo_timing(
        normalized_notes,
        solo_timing=solo_timing,
        input_origin=input_origin,
    )
    sink = trace_sink or MemoryTraceSink()
    clock = ManualClock()
    midi_output = _DeterministicMidiOutput(clock, sink)
    output = DeadlineAccompanimentOutput(
        midi_output,
        now=clock.now,
        output_advance_ms=runtime_config.output_advance_ms,
        autostart=False,
    )
    selected_arrival_curve = (
        arrival_curve if arrival_curve is not None else ReferenceWarpArrivalCurve(bundle.events)
    )
    follower = follower_factory()
    follower_evidence = _follower_evidence(follower)
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=runtime_config,
        clock=clock,
        follower=follower,
        tempo_model=tempo_model_factory() if tempo_model_factory is not None else None,
        output=output,
        trace_sink=sink,
        anchor_ticks=tuple(anchor_ticks),
        arrival_curve=selected_arrival_curve,
    )
    engine.start()
    tail_seconds = (
        config.tail_seconds
        if config.tail_seconds is not None
        else max(1.0, runtime_config.planning_horizon_ms / 1000)
    )
    end_time = normalized_notes[-1].perf_time + tail_seconds
    _drive_virtual_runtime(
        engine=engine,
        output=output,
        clock=clock,
        notes=normalized_notes,
        end_time=end_time,
        control_tick_seconds=config.control_tick_seconds,
    )
    engine.stop("evaluation_complete")
    output.close()

    delivered = tuple(
        DeliveredEventMetric(
            event_id=item.event.event.event_id,
            score_beat=item.event.event.beat,
            pitch=item.event.event.pitch,
            target_perf_time=item.event.perf_time,
            delivered_perf_time=item.delivered_at,
        )
        for item in midi_output.delivered
    )
    per_beat = _beat_metrics(delivered, timing_points)
    by_beat = {metric.score_beat: metric for metric in per_beat}
    landmark_metrics = tuple(
        LandmarkOnsetMetric(name=name, **by_beat[float(math.floor(beat))].model_dump())
        for name, beat in sorted((landmarks or {}).items())
        if float(math.floor(beat)) in by_beat
    )
    return ClosedLoopEvaluationReport(
        seed=config.seed,
        follower_evidence=follower_evidence,
        input_digest=_input_digest(normalized_notes, timing_points),
        runtime_config=runtime_config,
        evaluation_config=config,
        arrival_curve=selected_arrival_curve.curve_id,
        delivered_events=delivered,
        per_beat=per_beat,
        per_beat_aggregate=_aggregate_metrics(per_beat),
        landmarks=landmark_metrics,
        landmark_aggregate=_aggregate_metrics(landmark_metrics),
    )


def evaluate_projection_closed_loop(
    *,
    projection: ProvisionalRuntimeProjection,
    notes: Iterable[PerformedNote],
    raw_follower_factory: FollowerFactory,
    runtime_config: RuntimeConfig,
    **kwargs,
) -> ClosedLoopEvaluationReport:
    """Evaluate with the exact source-performance→canonical live follower seam."""

    return evaluate_closed_loop(
        bundle=projection.bundle,
        notes=notes,
        follower_factory=lambda: runtime_follower(raw_follower_factory(), projection),
        runtime_config=runtime_config,
        **kwargs,
    )


def load_aligned_midi_take(
    midi_path: Path | str,
    *,
    score_beat_by_note_index: Mapping[int, float],
) -> tuple[PerformedNote, ...]:
    """Load a recorded MIDI take with explicit canonical alignment evidence.

    The MIDI owns performed time/pitch/velocity. The supplied mapping owns only
    canonical score identity for matched note-on indices; unmatched notes remain
    valid follower input with ``score_beat=None``.
    """

    return tuple(
        PerformedNote(
            perf_time=note.time_seconds,
            pitch=note.pitch,
            velocity=note.velocity,
            score_beat=score_beat_by_note_index.get(note.index),
            event_id=f"midi_note_{note.index}",
        )
        for note in extract_note_events(midi_path)
    )


def write_closed_loop_report(
    report: ClosedLoopEvaluationReport,
    path: Path | str,
) -> Path:
    """Persist the versioned metrics artifact with byte-stable formatting."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return destination


def _follower_evidence(follower: ScoreFollower) -> FollowerEvidence:
    """Derive report provenance from the follower that actually executed."""

    if isinstance(follower, OracleFollower):
        return "synthetic_oracle"
    if isinstance(follower, ReferencePitchFollower):
        return "deterministic_test_follower"
    return "unspecified"


def _drive_virtual_runtime(
    *,
    engine: LiveEngine,
    output: DeadlineAccompanimentOutput,
    clock: ManualClock,
    notes: tuple[PerformedNote, ...],
    end_time: float,
    control_tick_seconds: float,
) -> None:
    note_index = 0
    tick_index = 0
    epsilon = 1e-12
    while True:
        next_note = notes[note_index].perf_time if note_index < len(notes) else math.inf
        next_tick = tick_index * control_tick_seconds
        next_output = output.next_deadline
        if next_output is None:
            next_output = math.inf
        next_time = min(next_note, next_tick, max(clock.now(), next_output), end_time)
        if next_time > end_time + epsilon:
            break
        if next_time > clock.now():
            clock.advance_to(next_time)
        output.flush_due(clock.now())
        while note_index < len(notes) and notes[note_index].perf_time <= clock.now() + epsilon:
            engine.process_note(notes[note_index])
            engine.tick()
            note_index += 1
        if next_tick <= clock.now() + epsilon:
            engine.tick()
            tick_index += 1
        output.flush_due(clock.now())
        if clock.now() >= end_time - epsilon:
            break


def _normalized_solo_timing(
    notes: tuple[PerformedNote, ...],
    *,
    solo_timing: Iterable[SoloTimingPoint] | None,
    input_origin: float,
) -> tuple[SoloTimingPoint, ...]:
    if solo_timing is None:
        raw_points = tuple(
            SoloTimingPoint(score_beat=note.score_beat, perf_time=note.perf_time)
            for note in notes
            if note.score_beat is not None
        )
    else:
        raw_points = tuple(
            SoloTimingPoint(
                score_beat=point.score_beat,
                perf_time=point.perf_time - input_origin,
            )
            for point in solo_timing
        )
    by_beat: dict[float, float] = {}
    for point in raw_points:
        prior = by_beat.get(point.score_beat)
        by_beat[point.score_beat] = (
            point.perf_time if prior is None else min(prior, point.perf_time)
        )
    points = tuple(
        SoloTimingPoint(score_beat=beat, perf_time=perf_time)
        for beat, perf_time in sorted(by_beat.items())
    )
    if len(points) < 2:
        raise ValueError("onset metrics require at least two aligned solo timing points")
    return points


def _beat_metrics(
    delivered: tuple[DeliveredEventMetric, ...],
    solo_timing: tuple[SoloTimingPoint, ...],
) -> tuple[BeatOnsetMetric, ...]:
    grouped: dict[float, list[DeliveredEventMetric]] = {}
    for event in delivered:
        beat = float(math.floor(event.score_beat + 1e-9))
        grouped.setdefault(beat, []).append(event)
    metrics: list[BeatOnsetMetric] = []
    for beat, events in sorted(grouped.items()):
        orchestra_time = min(event.delivered_perf_time for event in events)
        solo_time = _interpolate_solo_time(beat, solo_timing)
        signed_error_ms = (orchestra_time - solo_time) * 1000
        metrics.append(
            BeatOnsetMetric(
                score_beat=beat,
                event_ids=tuple(sorted(event.event_id for event in events)),
                solo_perf_time=solo_time,
                orchestra_perf_time=orchestra_time,
                signed_error_ms=signed_error_ms,
                abs_error_ms=abs(signed_error_ms),
            )
        )
    return tuple(metrics)


def _interpolate_solo_time(
    score_beat: float,
    points: tuple[SoloTimingPoint, ...],
) -> float:
    if score_beat <= points[0].score_beat:
        left, right = points[0], points[1]
    elif score_beat >= points[-1].score_beat:
        left, right = points[-2], points[-1]
    else:
        right_index = next(
            index for index, point in enumerate(points) if point.score_beat >= score_beat
        )
        left, right = points[right_index - 1], points[right_index]
    span = right.score_beat - left.score_beat
    if span <= 0:
        return left.perf_time
    ratio = (score_beat - left.score_beat) / span
    return left.perf_time + ratio * (right.perf_time - left.perf_time)


def _aggregate_metrics(
    metrics: tuple[BeatOnsetMetric, ...] | tuple[LandmarkOnsetMetric, ...],
) -> AggregateOnsetMetric:
    if not metrics:
        return AggregateOnsetMetric(count=0)
    return AggregateOnsetMetric(
        count=len(metrics),
        mean_signed_error_ms=fmean(metric.signed_error_ms for metric in metrics),
        mean_abs_error_ms=fmean(metric.abs_error_ms for metric in metrics),
        max_abs_error_ms=max(metric.abs_error_ms for metric in metrics),
    )


def _input_digest(
    notes: tuple[PerformedNote, ...],
    solo_timing: tuple[SoloTimingPoint, ...],
) -> str:
    payload = {
        "notes": [
            {
                "perf_time": note.perf_time,
                "pitch": note.pitch,
                "velocity": note.velocity,
                "score_beat": note.score_beat,
                "event_id": note.event_id,
            }
            for note in notes
        ],
        "solo_timing": [point.model_dump(mode="json") for point in solo_timing],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "BeatOnsetMetric",
    "AggregateOnsetMetric",
    "ClosedLoopEvaluationConfig",
    "ClosedLoopEvaluationReport",
    "DeliveredEventMetric",
    "LandmarkOnsetMetric",
    "SoloTimingPoint",
    "evaluate_closed_loop",
    "evaluate_projection_closed_loop",
    "load_aligned_midi_take",
    "write_closed_loop_report",
]
