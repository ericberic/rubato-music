from __future__ import annotations

from pathlib import Path

import pytest

from aimusic.accompaniment import ScoreBundle
from aimusic.accompaniment.following import OracleFollower, PerformedNote, ReferencePitchFollower
from aimusic.accompaniment.scheduler import AccompanimentScheduler
from aimusic.accompaniment.simulation import generate_synthetic_performance, run_simulated_online
from aimusic.accompaniment.tempo_model import OnlineTempoModel

FIXTURE = Path("tests/fixtures/score_bundles/tiny_chopin_excerpt")


def test_oracle_simulated_online_schedules_accompaniment_from_rubato_input() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    performed = generate_synthetic_performance(
        bundle.solo_events,
        start_perf_time=10.0,
        beat_period_fn=lambda _beat: 0.6,
        velocity_fn=lambda event: (event.velocity or 64) + 5,
    )

    trace = run_simulated_online(
        bundle=bundle,
        performed_notes=performed,
        follower=OracleFollower(),
        tempo_model=OnlineTempoModel(initial_tempo_bpm=126.0, smoothing_alpha=1.0),
        scheduler=AccompanimentScheduler(bundle, lookahead_beats=1.0),
    )

    assert [note.perf_time for note in trace.performed_notes] == pytest.approx([10.0, 10.6])
    assert [note.velocity for note in trace.performed_notes] == [77, 81]
    assert [update.score_beat for update in trace.follower_updates] == [4.0, 5.0]
    assert [state.beat_period_seconds for state in trace.tempo_states] == pytest.approx(
        [60 / 126, 0.6]
    )
    assert [event.event.event_id for event in trace.scheduled_events] == [
        "accomp_001",
        "accomp_002",
    ]
    assert [event.perf_time for event in trace.scheduled_events] == pytest.approx([10.0, 11.2])
    assert trace.scheduled_events[-1].tempo_bpm == pytest.approx(100.0)


def test_oracle_follower_ignores_notes_without_score_beat() -> None:
    follower = OracleFollower()

    assert follower.observe(PerformedNote(perf_time=0.0, pitch=60, velocity=64)) is None


def test_reference_pitch_follower_infers_score_beats_without_ground_truth() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    performed_with_truth = generate_synthetic_performance(
        bundle.solo_events,
        start_perf_time=10.0,
        beat_period_fn=lambda _beat: 0.6,
    )
    performed_without_truth = tuple(
        PerformedNote(
            perf_time=note.perf_time,
            pitch=note.pitch,
            velocity=note.velocity,
        )
        for note in performed_with_truth
    )

    trace = run_simulated_online(
        bundle=bundle,
        performed_notes=performed_without_truth,
        follower=ReferencePitchFollower(bundle.solo_events),
        tempo_model=OnlineTempoModel(initial_tempo_bpm=126.0, smoothing_alpha=1.0),
        scheduler=AccompanimentScheduler(bundle, lookahead_beats=1.0),
    )

    assert all(note.score_beat is None for note in trace.performed_notes)
    assert [update.score_beat for update in trace.follower_updates] == [4.0, 5.0]
    assert [update.raw_state["source"] for update in trace.follower_updates] == [
        "pitch_match",
        "pitch_match",
    ]
    assert [state.beat_period_seconds for state in trace.tempo_states] == pytest.approx(
        [60 / 126, 0.6]
    )
    assert [event.event.event_id for event in trace.scheduled_events] == [
        "accomp_001",
        "accomp_002",
    ]
    assert [event.perf_time for event in trace.scheduled_events] == pytest.approx([10.0, 11.2])


def test_reference_pitch_follower_can_anchor_then_infer_missing_score_beats() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    performed_with_truth = generate_synthetic_performance(
        bundle.solo_events,
        start_perf_time=10.0,
        beat_period_fn=lambda _beat: 0.6,
    )
    anchored_then_hidden = (
        performed_with_truth[0],
        PerformedNote(
            perf_time=performed_with_truth[1].perf_time,
            pitch=performed_with_truth[1].pitch,
            velocity=performed_with_truth[1].velocity,
        ),
    )

    trace = run_simulated_online(
        bundle=bundle,
        performed_notes=anchored_then_hidden,
        follower=ReferencePitchFollower(bundle.solo_events),
        tempo_model=OnlineTempoModel(initial_tempo_bpm=126.0, smoothing_alpha=1.0),
        scheduler=AccompanimentScheduler(bundle, lookahead_beats=1.0),
    )

    assert [note.score_beat for note in trace.performed_notes] == [4.0, None]
    assert [update.score_beat for update in trace.follower_updates] == [4.0, 5.0]
    assert [update.raw_state["source"] for update in trace.follower_updates] == [
        "provided_score_beat",
        "pitch_match",
    ]
    assert trace.scheduled_events[-1].perf_time == pytest.approx(11.2)
