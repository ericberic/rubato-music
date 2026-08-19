"""The LTE FOLLOW clock leads a broadening; reactive does not.

LTE keeps the reactive pace estimate and adds a bounded, non-negative leading
phase: it moves the orchestra *earlier* only when the pianist is arriving later
than the stable pace predicts (a ritardando), and otherwise collapses to the
reactive clock. These tests pin those invariants.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from aimusic.accompaniment.following import FollowerUpdate, OracleFollower, PerformedNote
from aimusic.accompaniment.predictive_follow import (
    InterpretationArrivalCurve,
    LteTempoModel,
    dispersion_trust_gain,
)
from aimusic.accompaniment.runtime_contracts import RuntimeConfig
from aimusic.accompaniment.runtime_io import CapturingOutput, ManualClock
from aimusic.accompaniment.scheduler import ReferenceWarpArrivalCurve
from aimusic.accompaniment.score_bundle import ScoreBundle
from aimusic.accompaniment.section_policy import AccompanimentMode
from aimusic.accompaniment.tempo_model import OnlineTempoModel

FIXTURE = Path("tests/fixtures/score_bundles/synthetic_accompaniment_capability")

_NO_REFERENCE_MAP = lambda _beat: None  # noqa: E731


def _upd(perf_time: float, beat: float) -> FollowerUpdate:
    # Reference position supplied (as the live canonical follower does), 1:1 with score.
    return FollowerUpdate(perf_time=perf_time, score_beat=beat, confidence=1.0, reference_beat=beat)


def _lte() -> LteTempoModel:
    return LteTempoModel(
        base=OnlineTempoModel(initial_tempo_bpm=120.0),
        project_reference_beat=_NO_REFERENCE_MAP,
    )


def _reference_mapped_bundle() -> ScoreBundle:
    source = ScoreBundle.load(FIXTURE)
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


def test_dispersion_trust_gain_is_continuous_and_bounded() -> None:
    assert dispersion_trust_gain(0.5, 0.0) == 1.0
    assert dispersion_trust_gain(0.5, 0.0375) == pytest.approx(0.5)
    assert dispersion_trust_gain(0.5, 0.075) == 0.0
    assert dispersion_trust_gain(0.5, 1.0) == 0.0
    assert dispersion_trust_gain(0.5, None) == 0.0
    assert dispersion_trust_gain(None, 0.0) == 0.0


def test_interpretation_curve_blends_prediction_to_reference_continuously() -> None:
    bundle = _reference_mapped_bundle()
    reference = ReferenceWarpArrivalCurve(bundle.events)
    event = next(event for event in bundle.accompaniment_events if event.beat == 3.0)
    tempo = replace(
        OnlineTempoModel(initial_tempo_bpm=120).update(_upd(0.5, 1.0)),
        reference_beat_period_seconds=0.5,
    )

    def timing(dispersion: float | None):
        curve = InterpretationArrivalCurve(
            reference_curve=reference,
            expected_period_at_tick=lambda _tick: 0.4,
            dispersion_at_tick=lambda _tick: dispersion,
        )
        return curve.timing_for(event, tempo, AccompanimentMode.FOLLOW)

    trusted = timing(0.0)
    half_trusted = timing(0.03)
    high_dispersion = timing(0.2)
    missing_dispersion = timing(None)
    reactive = reference.timing_for(event, tempo, AccompanimentMode.FOLLOW)

    assert trusted is not None
    assert half_trusted is not None
    assert high_dispersion is not None
    assert missing_dispersion is not None
    assert reactive is not None
    assert trusted.elapsed_seconds == pytest.approx(0.8)
    assert half_trusted.elapsed_seconds == pytest.approx(0.9)
    assert high_dispersion.elapsed_seconds == pytest.approx(reactive.elapsed_seconds)
    assert missing_dispersion.elapsed_seconds == pytest.approx(reactive.elapsed_seconds)


def test_interpretation_curve_never_retimes_autonomous_lead() -> None:
    bundle = _reference_mapped_bundle()
    reference = ReferenceWarpArrivalCurve(bundle.events)
    curve = InterpretationArrivalCurve(
        reference_curve=reference,
        expected_period_at_tick=lambda _tick: 0.1,
        dispersion_at_tick=lambda _tick: 0.0,
    )
    event = next(event for event in bundle.accompaniment_events if event.beat == 3.0)
    tempo = replace(
        OnlineTempoModel(initial_tempo_bpm=120).update(_upd(0.5, 1.0)),
        reference_beat_period_seconds=0.5,
    )

    predicted = curve.timing_for(event, tempo, AccompanimentMode.LEAD)
    authored = reference.timing_for(event, tempo, AccompanimentMode.LEAD)

    assert predicted == authored


def test_lte_leads_a_ritardando() -> None:
    model = _lte()
    # Gaps grow (0.5, 0.6, 0.8, 1.0, 1.3): each note arrives later than the
    # lagging pace estimate predicts, so the lead should build up.
    times = [0.0, 0.5, 1.1, 1.9, 2.9, 4.2]
    state = None
    for beat, t in enumerate(times):
        state = model.update(_upd(t, float(beat)))
    assert state is not None
    # The anchor now leads the detected note (orchestra scheduled earlier)...
    assert state.perf_time < times[-1]
    # ...but never by more than the bound.
    assert times[-1] - state.perf_time <= 0.15 + 1e-9


def test_lte_collapses_to_reactive_when_steady() -> None:
    model = _lte()
    # A steady 0.5 s pulse matches the 120 bpm prior: no lateness, no lead.
    state = None
    for beat in range(6):
        state = model.update(_upd(beat * 0.5, float(beat)))
    assert state is not None
    assert state.perf_time == pytest.approx(2.5, abs=1e-6)  # == detected time


def test_lte_never_lags_worse_than_reactive() -> None:
    # An accelerando makes lateness negative; the lead stays clamped at zero, so
    # the anchor is never pushed later than the detected note.
    model = _lte()
    times = [0.0, 0.7, 1.2, 1.6, 1.9, 2.1]
    for beat, t in enumerate(times):
        state = model.update(_upd(t, float(beat)))
        assert state.perf_time <= t + 1e-9


def test_lte_passes_through_without_a_reference_map() -> None:
    # No reference position and no map to project one: pure reactive passthrough.
    model = _lte()
    follower = OracleFollower()
    note = PerformedNote(perf_time=1.0, pitch=60, velocity=64, score_beat=3.0)
    state = model.update(follower.observe(note))
    assert state.reference_beat is None
    assert state.perf_time == pytest.approx(1.0)


def _build_engine(config: RuntimeConfig):
    from aimusic.accompaniment.live_engine import LiveEngine

    return LiveEngine.from_bundle(
        bundle=ScoreBundle.load(FIXTURE),
        config=config,
        clock=ManualClock(0.0),
        follower=OracleFollower(),
        output=CapturingOutput(),
    )


def test_from_bundle_defaults_to_lte_clock() -> None:
    engine = _build_engine(RuntimeConfig(run_id="default-clock"))
    assert isinstance(engine.tempo_model, LteTempoModel)


def test_from_bundle_selects_reactive_clock_when_configured() -> None:
    engine = _build_engine(RuntimeConfig(run_id="reactive-clock", follow_clock="reactive"))
    assert isinstance(engine.tempo_model, OnlineTempoModel)


def _pace_profile(period: float, *, support: int = 3):
    from aimusic.accompaniment.predictive_follow import PaceProfile

    return PaceProfile(
        smoothed_period_at=lambda _beat: period,
        support_at=lambda _beat: support,
    )


def test_rehearsal_anchor_holds_rehearsed_pace_against_follower_jitter() -> None:
    from aimusic.accompaniment.predictive_follow import RehearsalAnchoredTempoModel
    from aimusic.accompaniment.tempo_model import OnlineTempoModel

    # Rehearsal says 1.2 s/quarter (50 BPM). The pianist plays a steady ~50 with
    # follower jitter; the anchored pace must hold near 50, not swing.
    base = OnlineTempoModel(initial_tempo_bpm=120.0, smoothing_alpha=1.0)
    model = RehearsalAnchoredTempoModel(base=base, profile=_pace_profile(1.2))

    # Steady 50 BPM (0.5 beat per 0.6 s) with +/- jitter on each detected beat.
    jitter = [0.0, -0.1, 0.1, -0.1, 0.1, -0.1, 0.1, 0.0]
    steps = [(round(i * 0.6, 2), 100.0 + i * 0.5 + jitter[i]) for i in range(8)]

    tempos, reactive_tempos = [], []
    reactive = OnlineTempoModel(initial_tempo_bpm=120.0, smoothing_alpha=1.0)
    for t, beat in steps:
        tempos.append(
            model.update(FollowerUpdate(perf_time=t, score_beat=beat, confidence=1.0)).tempo_bpm
        )
        reactive_tempos.append(
            reactive.update(FollowerUpdate(perf_time=t, score_beat=beat, confidence=1.0)).tempo_bpm
        )

    # Anchored pace settles near the rehearsed 50 and swings far less than reactive.
    assert 40.0 <= tempos[-1] <= 60.0, tempos
    assert (max(tempos) - min(tempos)) < (max(reactive_tempos) - min(reactive_tempos))


def test_rehearsal_anchor_degrades_to_reactive_where_unsupported() -> None:
    from aimusic.accompaniment.predictive_follow import PaceProfile, RehearsalAnchoredTempoModel
    from aimusic.accompaniment.tempo_model import OnlineTempoModel

    unsupported = PaceProfile(smoothed_period_at=lambda _b: 1.2, support_at=lambda _b: 0)
    base = OnlineTempoModel(initial_tempo_bpm=120.0, smoothing_alpha=1.0)
    model = RehearsalAnchoredTempoModel(base=base, profile=unsupported)

    model.update(FollowerUpdate(perf_time=0.0, score_beat=10.0, confidence=1.0))
    state = model.update(FollowerUpdate(perf_time=1.0, score_beat=11.0, confidence=1.0))
    # No support -> pace is exactly the reactive 1.0 s/beat (60 BPM), unanchored.
    assert state.tempo_bpm == pytest.approx(60.0)


def test_rehearsal_anchor_seed_sets_scale_from_clamp() -> None:
    from aimusic.accompaniment.predictive_follow import RehearsalAnchoredTempoModel
    from aimusic.accompaniment.tempo_model import OnlineTempoModel, TimingSeed

    base = OnlineTempoModel(initial_tempo_bpm=120.0, smoothing_alpha=1.0)
    model = RehearsalAnchoredTempoModel(base=base, profile=_pace_profile(1.2))
    # Clamp says pianist is at 1.0 s/beat while rehearsal is 1.2 -> scale 0.833.
    seeded = model.seed_timing(
        TimingSeed(perf_time=0.0, score_beat=100.0, beat_period_seconds=1.0)
    )
    assert seeded.beat_period_seconds == pytest.approx(1.0, abs=0.02)
