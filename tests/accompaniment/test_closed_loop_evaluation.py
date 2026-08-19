from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from aimusic.accompaniment.closed_loop_evaluation import (
    ClosedLoopEvaluationConfig,
    evaluate_closed_loop,
    load_aligned_midi_take,
    write_closed_loop_report,
)
from aimusic.accompaniment.following import FollowerUpdate, OracleFollower, PerformedNote
from aimusic.accompaniment.predictive_follow import (
    InterpretationArrivalCurve,
    LteTempoModel,
)
from aimusic.accompaniment.runtime_contracts import RuntimeConfig
from aimusic.accompaniment.runtime_io import MemoryTraceSink
from aimusic.accompaniment.scheduler import (
    CurveEventTiming,
    FlatScoreArrivalCurve,
    ReferenceWarpArrivalCurve,
)
from aimusic.accompaniment.score_bundle import ScoreBundle, ScoreEvent
from aimusic.accompaniment.section_policy import AccompanimentMode
from aimusic.accompaniment.tempo_model import OnlineTempoModel, TempoState

BUNDLE = Path("tests/fixtures/score_bundles/synthetic_accompaniment_capability")
TAKE = Path("tests/fixtures/closed_loop_take_v1.json")
LIVE_TRACE = Path("tests/fixtures/closed_loop_live_trace_v1.jsonl")
MIDI_TAKE = Path("tests/fixtures/simple.mid")


def test_evaluation_config_cannot_claim_follower_evidence() -> None:
    with pytest.raises(ValueError):
        ClosedLoopEvaluationConfig.model_validate(
            {"follower_evidence": "recorded_matchmaker_trace"}
        )


class _ReferenceOracleFollower(OracleFollower):
    """Test follower shaped like live `_CanonicalFollower` output."""

    def observe(self, note: PerformedNote) -> FollowerUpdate | None:
        if note.score_beat is None:
            return None
        return FollowerUpdate(
            perf_time=note.perf_time,
            score_beat=note.score_beat,
            reference_beat=note.score_beat,
            confidence=1.0,
            raw_state={"follower": "fixed-aligned-take"},
        )


class _WarpOracleFollower(OracleFollower):
    def __init__(self, reference_at) -> None:
        self._reference_at = reference_at

    def observe(self, note: PerformedNote) -> FollowerUpdate | None:
        if note.score_beat is None:
            return None
        return FollowerUpdate(
            perf_time=note.perf_time,
            score_beat=note.score_beat,
            reference_beat=self._reference_at(note.score_beat),
            confidence=1.0,
            raw_state={"follower": "fixed-warped-take"},
        )


class _AnchorOnlyDelayCurve:
    """Inject a local scheduler miss without corrupting the following warp."""

    curve_id = "test-anchor-only-delay-v1"

    def __init__(self, bundle: ScoreBundle) -> None:
        self._base = ReferenceWarpArrivalCurve(bundle.events)

    def timing_for(
        self,
        event: ScoreEvent,
        tempo_state: TempoState,
        mode: AccompanimentMode,
    ) -> CurveEventTiming | None:
        timing = self._base.timing_for(event, tempo_state, mode)
        if timing is None or event.beat != 3.0:
            return timing
        return replace(timing, elapsed_seconds=timing.elapsed_seconds + 0.2)


def _notes() -> tuple[PerformedNote, ...]:
    payload = json.loads(TAKE.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    return tuple(PerformedNote(**row) for row in payload["notes"])


def _take_warp():
    points = tuple(
        (note.score_beat, 2.0 * (note.perf_time - _notes()[0].perf_time))
        for note in _notes()
        if note.score_beat is not None
    )

    def reference_at(score_beat: float) -> float:
        for (left_beat, left_ref), (right_beat, right_ref) in zip(points, points[1:]):
            if score_beat <= right_beat:
                ratio = (score_beat - left_beat) / (right_beat - left_beat)
                return left_ref + ratio * (right_ref - left_ref)
        left_beat, left_ref = points[-2]
        right_beat, right_ref = points[-1]
        return left_ref + (score_beat - left_beat) * (
            (right_ref - left_ref) / (right_beat - left_beat)
        )

    source = ScoreBundle.load(BUNDLE)
    bundle = replace(
        source,
        events=tuple(
            replace(
                event,
                source_refs={
                    **event.source_refs,
                    "source_performance_beat": reference_at(event.beat),
                    "source_performance_duration_beats": (
                        reference_at(event.beat + event.duration_beats) - reference_at(event.beat)
                    ),
                },
            )
            for event in source.events
        ),
    )
    return bundle, reference_at


def _reference_mapped_bundle() -> ScoreBundle:
    source = ScoreBundle.load(BUNDLE)
    return replace(
        source,
        events=tuple(
            replace(
                event,
                source_refs={
                    **event.source_refs,
                    "source_performance_beat": event.beat,
                    "source_performance_duration_beats": event.duration_beats,
                },
            )
            for event in source.events
        ),
    )


def _evaluate(clock: str, *, trace_sink=None, output_advance_ms: float = 0):
    return evaluate_closed_loop(
        bundle=ScoreBundle.load(BUNDLE),
        notes=_notes(),
        follower_factory=_ReferenceOracleFollower,
        runtime_config=RuntimeConfig(
            run_id=f"closed-loop-{clock}",
            follow_clock=clock,
            initial_tempo_bpm=120,
            planning_horizon_ms=1500,
            dispatch_horizon_ms=100,
            output_advance_ms=output_advance_ms,
        ),
        evaluation_config=ClosedLoopEvaluationConfig(seed=20260726),
        landmarks={"broadening_arrival": 5.0},
        trace_sink=trace_sink,
    )


def test_curve_aware_closed_loop_removes_flat_ritardando_bias() -> None:
    bundle, reference_at = _take_warp()

    def evaluate(*, flat: bool, tempo_model_factory=None):
        return evaluate_closed_loop(
            bundle=bundle,
            notes=_notes(),
            follower_factory=lambda: _WarpOracleFollower(reference_at),
            runtime_config=RuntimeConfig(
                run_id="curve-aware-ab",
                follow_clock="reactive",
                initial_tempo_bpm=120,
                planning_horizon_ms=2000,
                dispatch_horizon_ms=100,
            ),
            evaluation_config=ClosedLoopEvaluationConfig(seed=20260726),
            landmarks={"broadening_arrival": 5.0},
            tempo_model_factory=tempo_model_factory,
            arrival_curve=FlatScoreArrivalCurve() if flat else None,
        )

    curve = evaluate(flat=False)
    flat = evaluate(flat=True)
    zero_lead = evaluate(
        flat=False,
        tempo_model_factory=lambda: LteTempoModel(
            base=OnlineTempoModel(initial_tempo_bpm=120),
            project_reference_beat=reference_at,
            max_lead_seconds=0.0,
        ),
    )

    assert curve.input_digest == flat.input_digest == zero_lead.input_digest
    assert curve.schema_version == 3
    assert curve.execution_domain == "deterministic_virtual_clock"
    assert curve.hardware_delivery_measured is False
    assert curve.follower_evidence == "synthetic_oracle"
    assert curve.arrival_curve == "reference-warp-v1"
    assert flat.arrival_curve == "flat-score-v1"
    assert curve.per_beat_aggregate.max_abs_error_ms == pytest.approx(0.0, abs=1e-8)
    assert flat.per_beat_aggregate.max_abs_error_ms > 250.0
    assert zero_lead.per_beat_aggregate.max_abs_error_ms == pytest.approx(0.0, abs=1e-8)


def test_anchor_onset_already_rephases_lte_without_separate_clock_coupling() -> None:
    bundle, reference_at = _take_warp()
    source_post_anchor = next(event for event in bundle.accompaniment_events if event.beat == 5.0)
    immediate_post_anchor = replace(
        source_post_anchor,
        event_id="accomp_immediate_post_anchor",
        beat=4.0,
        pitch=71,
        source_refs={
            **source_post_anchor.source_refs,
            "source_performance_beat": reference_at(4.0),
            "source_performance_duration_beats": reference_at(5.0) - reference_at(4.0),
        },
    )
    bundle = replace(
        bundle,
        events=tuple(
            sorted(
                (*bundle.events, immediate_post_anchor),
                key=lambda event: (event.beat, event.event_id),
            )
        ),
    )

    def evaluate(*, anchored: bool, trace_sink=None):
        return evaluate_closed_loop(
            bundle=bundle,
            notes=_notes(),
            follower_factory=lambda: _WarpOracleFollower(reference_at),
            runtime_config=RuntimeConfig(
                run_id="anchor-phase-need",
                follow_clock="lte",
                initial_tempo_bpm=120,
                planning_horizon_ms=2000,
                dispatch_horizon_ms=100,
            ),
            evaluation_config=ClosedLoopEvaluationConfig(seed=20260726),
            landmarks={
                "anchor": 3.0,
                "immediate_post_anchor": 4.0,
                "post_anchor": 5.0,
            },
            trace_sink=trace_sink,
            anchor_ticks=(3 * 960,) if anchored else (),
            arrival_curve=_AnchorOnlyDelayCurve(bundle),
        )

    trace = MemoryTraceSink()
    anchored = evaluate(anchored=True, trace_sink=trace)
    unanchored = evaluate(anchored=False)
    anchored_landmarks = {metric.name: metric for metric in anchored.landmarks}
    unanchored_landmarks = {metric.name: metric for metric in unanchored.landmarks}

    assert anchored.input_digest == unanchored.input_digest
    assert anchored_landmarks["anchor"].signed_error_ms == pytest.approx(0.0, abs=1e-8)
    assert unanchored_landmarks["anchor"].signed_error_ms == pytest.approx(200.0, abs=1e-8)
    assert anchored_landmarks["immediate_post_anchor"].signed_error_ms == pytest.approx(
        0.0,
        abs=1e-8,
    )
    assert anchored_landmarks["immediate_post_anchor"].orchestra_perf_time == pytest.approx(
        unanchored_landmarks["immediate_post_anchor"].orchestra_perf_time,
        abs=1e-12,
    )
    assert anchored_landmarks["post_anchor"].signed_error_ms == pytest.approx(0.0, abs=1e-8)
    assert anchored_landmarks["post_anchor"].orchestra_perf_time == pytest.approx(
        unanchored_landmarks["post_anchor"].orchestra_perf_time,
        abs=1e-12,
    )

    anchor_input = next(
        row for row in trace.records if row.type == "input" and row.perf_time == pytest.approx(1.9)
    )
    anchor_tempo = next(
        row for row in trace.records if row.type == "tempo" and row.score_beat == 3.0
    )
    anchor_output = next(
        row
        for row in trace.records
        if row.type == "midi_output" and row.action == "note_on" and row.event_id == "accomp_001"
    )
    assert anchor_output.target_perf_time == pytest.approx(anchor_input.perf_time)
    assert anchor_output.monotonic_time == pytest.approx(anchor_input.perf_time)
    assert anchor_tempo.perf_time == pytest.approx(anchor_input.perf_time)


def test_dispersion_gain_closed_loop_degrades_exactly_to_reference() -> None:
    bundle = _reference_mapped_bundle()
    reference_curve = ReferenceWarpArrivalCurve(bundle.events)
    interval_periods = (0.5, 0.6, 0.8, 1.0, 1.3)

    def expected_period(score_tick: float) -> float:
        interval = min(len(interval_periods) - 1, max(0, int(score_tick // 960)))
        return interval_periods[interval]

    def evaluate(arrival_curve=None, *, trace_sink=None):
        return evaluate_closed_loop(
            bundle=bundle,
            notes=_notes(),
            follower_factory=_ReferenceOracleFollower,
            runtime_config=RuntimeConfig(
                run_id="dispersion-trust-ab",
                follow_clock="reactive",
                initial_tempo_bpm=120,
                planning_horizon_ms=2000,
                dispatch_horizon_ms=100,
            ),
            evaluation_config=ClosedLoopEvaluationConfig(seed=20260726),
            landmarks={"broadening_arrival": 5.0},
            arrival_curve=arrival_curve,
            trace_sink=trace_sink,
        )

    trace = MemoryTraceSink()
    trusted = evaluate(
        InterpretationArrivalCurve(
            reference_curve=reference_curve,
            expected_period_at_tick=expected_period,
            dispersion_at_tick=lambda _tick: 0.0,
            curve_id="test-low-dispersion",
        ),
        trace_sink=trace,
    )
    reference = evaluate(reference_curve)
    high_dispersion = evaluate(
        InterpretationArrivalCurve(
            reference_curve=reference_curve,
            expected_period_at_tick=expected_period,
            dispersion_at_tick=lambda tick: expected_period(tick) * 0.2,
            curve_id="test-high-dispersion",
        )
    )
    missing_dispersion = evaluate(
        InterpretationArrivalCurve(
            reference_curve=reference_curve,
            expected_period_at_tick=expected_period,
            dispersion_at_tick=lambda _tick: None,
            curve_id="test-missing-dispersion",
        )
    )

    assert trusted.input_digest == reference.input_digest
    assert trusted.per_beat_aggregate.max_abs_error_ms == pytest.approx(0.0, abs=1e-8)
    assert reference.per_beat_aggregate.max_abs_error_ms > 250.0
    assert [event.delivered_perf_time for event in high_dispersion.delivered_events] == (
        pytest.approx(
            [event.delivered_perf_time for event in reference.delivered_events],
            abs=1e-12,
        )
    )
    assert [event.delivered_perf_time for event in missing_dispersion.delivered_events] == (
        pytest.approx(
            [event.delivered_perf_time for event in reference.delivered_events],
            abs=1e-12,
        )
    )
    assert any(
        row.type == "scheduler" and row.arrival_curve_id == "test-low-dispersion"
        for row in trace.records
    )


def test_fixed_take_is_bit_exact_across_repeated_closed_loop_runs() -> None:
    first = _evaluate("reactive")
    second = _evaluate("reactive")

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.metric_version == "closed-loop-onset-v1"
    assert first.seed == 20260726
    assert len(first.input_digest) == 64
    assert first.per_beat_aggregate.count == 3
    assert first.landmark_aggregate.count == 1


def test_fixed_recorded_midi_take_runs_twice_with_identical_output() -> None:
    notes = load_aligned_midi_take(
        MIDI_TAKE,
        score_beat_by_note_index={0: 0.0, 1: 2.0},
    )

    def evaluate():
        return evaluate_closed_loop(
            bundle=ScoreBundle.load(BUNDLE),
            notes=notes,
            follower_factory=_ReferenceOracleFollower,
            runtime_config=RuntimeConfig(
                run_id="fixed-midi",
                follow_clock="reactive",
                initial_tempo_bpm=120,
                planning_horizon_ms=1500,
                dispatch_horizon_ms=100,
            ),
            evaluation_config=ClosedLoopEvaluationConfig(seed=20260726),
        )

    first = evaluate()
    second = evaluate()

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert [event.event_id for event in first.delivered_events] == [
        "accomp_000",
        "accomp_001",
        "accomp_002",
    ]


def test_scheduler_output_matches_frozen_live_trace_for_same_take() -> None:
    trace = MemoryTraceSink()
    report = _evaluate("reactive", trace_sink=trace)
    frozen_live_rows = [
        json.loads(line) for line in LIVE_TRACE.read_text(encoding="utf-8").splitlines() if line
    ]
    midi_rows = [
        row for row in trace.records if row.type == "midi_output" and row.action == "note_on"
    ]

    assert [event.event_id for event in report.delivered_events] == [
        row["event_id"] for row in frozen_live_rows
    ]
    assert [event.delivered_perf_time for event in report.delivered_events] == pytest.approx(
        [row["monotonic_time"] for row in frozen_live_rows],
        abs=1e-12,
    )
    assert [row.monotonic_time for row in midi_rows] == pytest.approx(
        [row["monotonic_time"] for row in frozen_live_rows],
        abs=1e-12,
    )
    assert {row.type for row in trace.records} >= {
        "input",
        "follower",
        "tempo",
        "policy",
        "scheduler",
        "midi_output",
        "state",
    }


def test_reactive_vs_lte_is_a_stable_ab_on_identical_input() -> None:
    reactive = _evaluate("reactive")
    lte = _evaluate("lte")
    lte_again = _evaluate("lte")

    assert reactive.input_digest == lte.input_digest
    assert lte.model_dump(mode="json") == lte_again.model_dump(mode="json")
    assert [event.event_id for event in reactive.delivered_events] == [
        event.event_id for event in lte.delivered_events
    ]
    assert [event.delivered_perf_time for event in reactive.delivered_events] != [
        event.delivered_perf_time for event in lte.delivered_events
    ]
    assert reactive.landmarks[0].name == "broadening_arrival"
    assert lte.landmarks[0].name == "broadening_arrival"


def test_metrics_are_measured_after_virtual_deadline_output() -> None:
    baseline = _evaluate("reactive")
    advanced = _evaluate("reactive", output_advance_ms=50)

    assert advanced.delivered_events[0].delivered_perf_time == 0
    assert [
        event.target_perf_time - event.delivered_perf_time
        for event in advanced.delivered_events[1:]
    ] == pytest.approx([0.05, 0.05])
    assert advanced.landmarks[0].signed_error_ms == pytest.approx(
        baseline.landmarks[0].signed_error_ms - 50
    )


def test_versioned_report_serialization_is_byte_stable(tmp_path: Path) -> None:
    first_path = write_closed_loop_report(_evaluate("reactive"), tmp_path / "first.json")
    second_path = write_closed_loop_report(_evaluate("reactive"), tmp_path / "second.json")

    assert first_path.read_bytes() == second_path.read_bytes()
