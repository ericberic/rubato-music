from __future__ import annotations

import pytest

from aimusic.accompaniment.following import FollowerUpdate
from aimusic.accompaniment.tempo_model import (
    EntryPaceAcquisition,
    OnlineTempoModel,
    TimingSeed,
)


def update(perf_time: float, score_beat: float) -> FollowerUpdate:
    return FollowerUpdate(perf_time=perf_time, score_beat=score_beat, confidence=1.0)


def test_tempo_model_ignores_tiny_score_progress_to_avoid_spikes() -> None:
    model = OnlineTempoModel(initial_tempo_bpm=120.0, smoothing_alpha=1.0)

    assert model.update(update(0.0, 10.0)).beat_period_seconds == pytest.approx(0.5)
    assert model.update(update(0.5, 10.05)).beat_period_seconds == pytest.approx(0.5)
    state = model.update(update(1.0, 11.0))

    assert state.beat_period_seconds == pytest.approx(1.0)
    assert state.tempo_bpm == pytest.approx(60.0)


def test_authored_timing_seed_is_not_follower_confidence() -> None:
    model = OnlineTempoModel(initial_tempo_bpm=120.0, smoothing_alpha=1.0)

    seeded = model.seed_timing(
        TimingSeed(
            perf_time=10.0,
            score_beat=172.0,
            beat_period_seconds=0.75,
            reference_beat=434.0,
            reference_beat_period_seconds=0.3,
        )
    )

    assert seeded.confidence == 0.0
    assert seeded.coasting is True
    assert seeded.beat_period_seconds == pytest.approx(0.75)
    assert seeded.reference_beat_period_seconds == pytest.approx(0.3)
    assert model.last_observation.decision == "timing_seed"


def test_tempo_model_ignores_small_backward_jitter_without_poisoning_reference() -> None:
    model = OnlineTempoModel(initial_tempo_bpm=120.0, smoothing_alpha=1.0)

    model.update(update(0.0, 10.0))
    model.update(update(0.5, 11.0))
    jitter_state = model.update(update(0.6, 10.9))
    jitter_observation = model.last_observation
    recovery_state = model.update(update(1.0, 12.0))

    assert jitter_state.beat_period_seconds == pytest.approx(0.5)
    assert jitter_state.score_beat == pytest.approx(11.0)
    assert jitter_observation.position_action == "clamp_backward_jitter"
    assert recovery_state.beat_period_seconds == pytest.approx(0.5)


def test_tempo_model_resets_reference_on_large_backward_jump() -> None:
    model = OnlineTempoModel(initial_tempo_bpm=120.0, smoothing_alpha=1.0)

    model.update(update(0.0, 10.0))
    model.update(update(0.5, 11.0))
    reset_state = model.update(update(1.0, 7.0))
    forward_after_reset = model.update(update(1.6, 8.0))

    assert reset_state.beat_period_seconds == pytest.approx(0.5)
    assert forward_after_reset.beat_period_seconds == pytest.approx(0.6)


def test_chord_note_positions_cannot_create_a_tempo_spike() -> None:
    model = OnlineTempoModel(initial_tempo_bpm=60.0, smoothing_alpha=1.0)

    model.update(update(0.0, 48.0))
    model.update(update(1.0, 48.605))
    chord_note = model.update(update(1.035, 48.999))

    assert chord_note.tempo_bpm == pytest.approx(36.3, rel=0.02)
    assert model.last_observation.decision == "insufficient_progress"
    assert model.last_observation.accepted is False


def test_implausibly_fast_candidate_is_rejected_until_a_real_beat_baseline() -> None:
    model = OnlineTempoModel(initial_tempo_bpm=80.0, smoothing_alpha=1.0)

    model.update(update(0.0, 84.0))
    spike = model.update(update(0.04, 85.0))
    recovered = model.update(update(0.75, 85.0))

    assert spike.tempo_bpm == pytest.approx(80.0)
    assert recovered.tempo_bpm == pytest.approx(80.0)
    assert model.last_observation.decision == "accepted"


# A slow (~50 BPM) entrance sampled every 0.15 s, long enough to satisfy the
# production window (>=6 onsets over >=1.3 s). Beat 152 + 0.8333 beat/s * t.
_SLOW_ENTRY = [(round(i * 0.15, 3), 152.0 + i * 0.15 * (50.0 / 60.0)) for i in range(11)]


def _acquire(onsets: list[tuple[float, float]], **kwargs: float) -> EntryPaceAcquisition:
    acquisition = EntryPaceAcquisition(**kwargs)
    for perf_time, beat in onsets:
        acquisition.observe(perf_time=perf_time, canonical_beat=beat, reference_beat=beat)
    return acquisition


def test_entry_pace_acquisition_withholds_estimate_until_window_is_musical() -> None:
    # Two matches spanning ~0 beats/0.05 s (the old handoff trigger) carry no
    # pace, and a still-short 0.9 s window over-reads the follower's catch-up.
    assert _acquire([(0.0, 152.0), (0.05, 152.02)]).estimate() is None
    assert _acquire(_SLOW_ENTRY[:6]).estimate() is None  # only ~0.75 s elapsed


def test_entry_pace_acquisition_fits_slow_piano_not_the_lead_in_seed() -> None:
    acquisition = _acquire(_SLOW_ENTRY)
    estimate = acquisition.estimate()
    assert estimate is not None
    assert estimate.tempo_bpm == pytest.approx(50.0, abs=1.0)
    # Phase is read from the fit, so it can be projected past the last onset.
    assert estimate.canonical_beat_at(1.8) == pytest.approx(153.5, abs=0.05)


def test_entry_pace_acquisition_is_robust_to_a_single_follower_jump() -> None:
    # One spurious leap must not tilt the pace; Theil-Sen rejects the minority.
    onsets = list(_SLOW_ENTRY)
    onsets[5] = (onsets[5][0], onsets[5][1] + 1.2)  # a one-onset forward jump
    estimate = _acquire(onsets).estimate()
    assert estimate is not None
    assert estimate.tempo_bpm == pytest.approx(50.0, abs=6.0)


def test_entry_pace_acquisition_ignores_non_advancing_timestamps() -> None:
    acquisition = _acquire([(0.0, 152.0), (0.0, 152.5), (-0.1, 153.0)])
    assert acquisition.onset_count == 1


def test_entry_pace_acquisition_force_settles_for_less() -> None:
    acquisition = _acquire([(0.0, 152.0), (0.2, 152.1)])
    assert acquisition.estimate() is None
    forced = acquisition.estimate(force=True)
    assert forced is not None
    assert forced.tempo_bpm == pytest.approx(30.0, abs=1.0)
