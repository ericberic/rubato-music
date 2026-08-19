from __future__ import annotations

from pathlib import Path

import pytest

from aimusic.accompaniment import ScoreBundle
from aimusic.accompaniment.evaluation import evaluate_scheduled_events
from aimusic.accompaniment.following import ReferencePitchFollower
from aimusic.accompaniment.scheduler import AccompanimentScheduler
from aimusic.accompaniment.simulation import (
    generate_synthetic_performance,
    hide_score_beats,
    run_simulated_online,
)
from aimusic.accompaniment.tempo_model import OnlineTempoModel

FIXTURE = Path("tests/fixtures/score_bundles/synthetic_accompaniment_capability")


def synthetic_rubato_period(previous_beat: float) -> float:
    """Synthetic soloist plays steady, then broadens after beat 3."""

    return 0.5 if previous_beat < 3.0 else 0.75


def test_hidden_scorebeat_playback_follows_and_schedules_accompaniment() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    performed_with_truth = generate_synthetic_performance(
        bundle.solo_events,
        start_perf_time=2.0,
        beat_period_fn=synthetic_rubato_period,
        velocity_fn=lambda event: (event.velocity or 64) + 4,
    )
    performed_without_truth = hide_score_beats(performed_with_truth)

    trace = run_simulated_online(
        bundle=bundle,
        performed_notes=performed_without_truth,
        follower=ReferencePitchFollower(bundle.solo_events),
        tempo_model=OnlineTempoModel(initial_tempo_bpm=120.0, smoothing_alpha=1.0),
        scheduler=AccompanimentScheduler(bundle, lookahead_beats=1.0),
    )

    assert all(note.score_beat is None for note in trace.performed_notes)
    assert [note.perf_time for note in trace.performed_notes] == pytest.approx(
        [2.0, 2.5, 3.0, 3.5, 4.25, 5.0]
    )
    assert [note.velocity for note in trace.performed_notes] == [64, 68, 72, 76, 80, 84]

    assert [update.score_beat for update in trace.follower_updates] == [0, 1, 2, 3, 4, 5]
    assert [update.raw_state["source"] for update in trace.follower_updates] == [
        "pitch_match",
        "pitch_match",
        "pitch_match",
        "pitch_match",
        "pitch_match",
        "pitch_match",
    ]

    assert [state.beat_period_seconds for state in trace.tempo_states] == pytest.approx(
        [0.5, 0.5, 0.5, 0.5, 0.75, 0.75]
    )
    assert [event.event.event_id for event in trace.scheduled_events] == [
        "accomp_000",
        "accomp_001",
        "accomp_002",
    ]

    evaluation = evaluate_scheduled_events(
        trace.scheduled_events,
        {
            "accomp_000": 2.0,
            "accomp_001": 3.5,
            "accomp_002": 5.0,
        },
        tolerance_seconds=0.001,
    )

    assert evaluation.passed
    assert evaluation.timing_errors_seconds == pytest.approx(
        {
            "accomp_000": 0.0,
            "accomp_001": 0.0,
            "accomp_002": 0.0,
        }
    )
    assert evaluation.max_abs_error_seconds == pytest.approx(0.0)
