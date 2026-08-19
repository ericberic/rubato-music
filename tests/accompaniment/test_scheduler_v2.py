from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from aimusic.accompaniment.runtime_io import CapturingOutput
from aimusic.accompaniment.scheduler import (
    AccompanimentScheduler,
    FlatScoreArrivalCurve,
)
from aimusic.accompaniment.score_bundle import ScoreBundle
from aimusic.accompaniment.section_policy import AccompanimentMode, Section, SectionMap
from aimusic.accompaniment.tempo_model import TempoState

FIXTURE = Path("tests/fixtures/score_bundles/synthetic_accompaniment_capability")


def state(perf_time: float, score_beat: float, period: float = 0.5) -> TempoState:
    return TempoState(
        perf_time=perf_time,
        score_beat=score_beat,
        beat_period_seconds=period,
        tempo_bpm=60 / period,
        confidence=1.0,
    )


def _warped_bundle(curvature: float) -> ScoreBundle:
    source = ScoreBundle.load(FIXTURE)

    def reference_at(score_beat: float) -> float:
        return score_beat + curvature * score_beat**2

    return replace(
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


def _scheduled_event_at_beat(
    bundle: ScoreBundle,
    tempo: TempoState,
    *,
    arrival_curve=None,
):
    scheduler = AccompanimentScheduler(
        bundle,
        lookahead_beats=2.0,
        arrival_curve=arrival_curve,
    )
    return next(event for event in scheduler.schedule(tempo) if event.event.beat == 3.0)


def test_reference_warp_integral_beats_flat_extrapolation_on_ritardando() -> None:
    errors: list[float] = []
    for curvature in (0.05, 0.1, 0.2):
        bundle = _warped_bundle(curvature)
        reference_beat = 1.0 + curvature
        tempo = replace(
            state(0.5 * reference_beat, 1.0),
            reference_beat=reference_beat,
            reference_beat_period_seconds=0.5,
        )
        ground_truth = 0.5 * (3.0 + curvature * 3.0**2)
        curve = _scheduled_event_at_beat(bundle, tempo)
        flat = _scheduled_event_at_beat(
            bundle,
            tempo,
            arrival_curve=FlatScoreArrivalCurve(),
        )

        assert curve.perf_time == pytest.approx(ground_truth)
        errors.append(abs(flat.perf_time - ground_truth))

    assert errors[0] < errors[1] < errors[2]


def test_reference_warp_matches_flat_extrapolation_at_steady_tempo() -> None:
    bundle = _warped_bundle(curvature=0.0)
    tempo = replace(
        state(0.5, 1.0),
        reference_beat=1.0,
        reference_beat_period_seconds=0.5,
    )

    curve = _scheduled_event_at_beat(bundle, tempo)
    flat = _scheduled_event_at_beat(
        bundle,
        tempo,
        arrival_curve=FlatScoreArrivalCurve(),
    )

    assert curve.perf_time == pytest.approx(flat.perf_time)
    assert curve.duration_seconds == pytest.approx(flat.duration_seconds)


def test_mutable_plan_retimes_but_dispatch_horizon_freezes() -> None:
    scheduler = AccompanimentScheduler(
        ScoreBundle.load(FIXTURE),
        planning_horizon_seconds=1.0,
        dispatch_horizon_seconds=0.05,
    )
    output = CapturingOutput()

    first = scheduler.update(state(1.0, 2.0), now=1.0, output=output)
    assert first.planned[0].event.event_id == "accomp_001"
    assert first.planned[0].perf_time == pytest.approx(1.5)

    retimed = scheduler.update(state(1.1, 2.2, 1.0), now=1.1, output=output)
    assert retimed.planned[0].perf_time == pytest.approx(1.9)

    # Once inside the 50 ms dispatch horizon, the event leaves the mutable
    # planner. A fresh estimate cannot make its immutable deadline oscillate.
    frozen = scheduler.update(state(1.86, 2.4, 0.2), now=1.86, output=output)
    assert [item.event.event_id for item in frozen.dispatched] == ["accomp_001"]
    assert output.sent[0][0] == pytest.approx(1.9)


def test_output_advance_dispatches_before_predicted_onset() -> None:
    scheduler = AccompanimentScheduler(
        ScoreBundle.load(FIXTURE),
        planning_horizon_seconds=1.0,
        dispatch_horizon_seconds=0.05,
        output_advance_ms=20,
    )
    output = CapturingOutput(output_advance_ms=20)

    scheduler.update(state(1.0, 2.0), now=1.0, output=output)
    result = scheduler.update(state(1.48, 2.0), now=1.48, output=output)

    assert result.dispatched[0].perf_time == pytest.approx(1.5)
    assert output.sent[0][0] == pytest.approx(1.48)


def test_lead_plan_stops_at_exclusive_section_boundary() -> None:
    scheduler = AccompanimentScheduler(
        ScoreBundle.load(FIXTURE),
        planning_horizon_seconds=3.0,
        dispatch_horizon_seconds=0.05,
    )
    output = CapturingOutput()

    result = scheduler.update(
        state(1.0, 2.0),
        now=1.0,
        output=output,
        mode=AccompanimentMode.LEAD,
        planning_end_beat=4.0,
    )

    assert [item.event.event_id for item in result.planned] == ["accomp_001"]
    assert all(item.event.beat < 4.0 for item in result.planned)


def test_skip_clears_old_plan_and_repeat_rearms_score_events() -> None:
    scheduler = AccompanimentScheduler(
        ScoreBundle.load(FIXTURE),
        planning_horizon_seconds=2.0,
        dispatch_horizon_seconds=0.05,
        jump_threshold_beats=1.5,
    )
    output = CapturingOutput()

    scheduler.update(state(0.0, 0.0), now=0.0, output=output)
    assert output.sent[0][1].event.event_id == "accomp_000"
    skipped = scheduler.update(state(0.5, 4.0), now=0.5, output=output)
    assert skipped.reset_reason == "skip"
    assert all(item.event.beat >= 4 for item in skipped.planned)
    # A forward correction clears mutable plans but lets sounding notes release
    # naturally instead of producing an all-notes-off pop.
    assert output.panics == []

    repeated = scheduler.update(state(1.0, 0.0), now=1.0, output=output)
    assert repeated.reset_reason == "repeat"
    assert [item[1].event.event_id for item in output.sent].count("accomp_000") == 2
    assert output.panics == [(1.0, "score_repeat")]


def test_ordered_transport_emits_an_event_crossed_between_updates_once() -> None:
    scheduler = AccompanimentScheduler(
        ScoreBundle.load(FIXTURE),
        lookahead_beats=0.0,
        planning_horizon_seconds=0.0,
        dispatch_horizon_seconds=0.0,
    )
    output = CapturingOutput()

    before = scheduler.update(state(1.0, 2.9), now=1.0, output=output)
    assert before.planned == ()

    crossed = scheduler.update(state(1.1, 3.1), now=1.1, output=output)
    assert [item.event.event_id for item in crossed.dispatched] == ["accomp_001"]
    repeated_tick = scheduler.update(state(1.2, 3.2), now=1.2, output=output)
    assert repeated_tick.dispatched == ()
    assert [item.event.event_id for _, item in output.sent] == ["accomp_001"]


def test_within_chord_follower_advance_dispatches_crossed_planned_event() -> None:
    source = ScoreBundle.load(FIXTURE)
    crossed_event = replace(
        source.accompaniment_events[1],
        source_refs={
            **source.accompaniment_events[1].source_refs,
            "source_performance_beat": 10.0,
            "source_performance_duration_beats": 2.0,
        },
    )
    bundle = replace(
        source,
        events=tuple(
            crossed_event if event.event_id == crossed_event.event_id else event
            for event in source.events
        ),
    )
    scheduler = AccompanimentScheduler(
        bundle,
        planning_horizon_seconds=1.0,
        dispatch_horizon_seconds=0.05,
    )
    output = CapturingOutput()
    before = replace(
        state(1.0, 2.9),
        reference_beat=9.0,
        reference_beat_period_seconds=0.5,
    )
    after = replace(
        state(1.02, 3.1),
        reference_beat=10.5,
        reference_beat_period_seconds=0.5,
    )

    planned = scheduler.update(before, now=1.0, output=output)
    assert [item.event.event_id for item in planned.planned] == [crossed_event.event_id]
    crossed = scheduler.update(after, now=1.02, output=output)

    assert crossed.expired_event_ids == ()
    assert [item.event.event_id for item in crossed.dispatched] == [crossed_event.event_id]
    assert crossed.dispatched[0].perf_time == pytest.approx(1.02)


def test_relock_does_not_catch_up_events_crossed_while_transport_was_held() -> None:
    scheduler = AccompanimentScheduler(
        ScoreBundle.load(FIXTURE),
        lookahead_beats=0.0,
        planning_horizon_seconds=0.0,
        dispatch_horizon_seconds=0.0,
    )
    output = CapturingOutput()

    scheduler.update(state(1.0, 2.9), now=1.0, output=output)
    scheduler.pause(score_beat=2.9)

    relocked = scheduler.update(state(2.0, 3.2), now=2.0, output=output)

    assert relocked.authority_generation > 0
    assert relocked.expired_event_ids == ()
    assert relocked.dispatched == ()
    assert output.sent == []
    later = scheduler.update(state(2.1, 3.3), now=2.1, output=output)
    assert later.dispatched == ()
    assert output.sent == []


def test_reference_derived_past_deadline_expires_instead_of_bursting_after_relock() -> None:
    source = ScoreBundle.load(FIXTURE)
    stale = replace(
        source.accompaniment_events[1],
        beat=3.3,
        source_refs={
            **source.accompaniment_events[1].source_refs,
            "source_performance_beat": 5.0,
        },
    )
    bundle = replace(
        source,
        events=tuple(
            stale if event.event_id == stale.event_id else event for event in source.events
        ),
    )
    scheduler = AccompanimentScheduler(
        bundle,
        planning_horizon_seconds=1.0,
        dispatch_horizon_seconds=0.05,
    )
    output = CapturingOutput()
    relocked = scheduler.update(
        replace(
            state(10.0, 3.2),
            reference_beat=10.0,
            reference_beat_period_seconds=0.5,
        ),
        now=10.0,
        output=output,
    )

    assert relocked.expired_event_ids == (stale.event_id,)
    assert relocked.dispatched == ()
    assert output.sent == []


def test_reanchor_invalidates_mutable_plan_and_stamps_new_generation() -> None:
    scheduler = AccompanimentScheduler(
        ScoreBundle.load(FIXTURE),
        planning_horizon_seconds=2.0,
    )
    output = CapturingOutput()
    first = scheduler.update(state(1.0, 2.0), now=1.0, output=output)
    old_generation = first.planned[0].authority_generation

    cancelled = scheduler.reanchor("test_clock_correction")
    second = scheduler.update(state(2.0, 2.0), now=2.0, output=output)

    assert cancelled
    assert second.authority_generation > old_generation
    assert second.authority_reason == "test_clock_correction"
    assert all(item.authority_generation == second.authority_generation for item in second.planned)


def test_hold_cancels_plan_and_stop_panics_once() -> None:
    scheduler = AccompanimentScheduler(
        ScoreBundle.load(FIXTURE),
        planning_horizon_seconds=2.0,
        dispatch_horizon_seconds=0.05,
    )
    output = CapturingOutput()
    planned = scheduler.update(state(1.0, 2.0), now=1.0, output=output)
    assert planned.planned

    held = scheduler.update(state(1.1, 2.1), now=1.1, output=output, mode=AccompanimentMode.HOLD)
    assert held.cancelled_event_ids == ("accomp_001", "accomp_002")
    assert output.panics == [(1.1, "section_hold")]

    scheduler.update(state(1.2, 2.2), now=1.2, output=output, mode=AccompanimentMode.STOP)
    scheduler.update(state(1.3, 2.3), now=1.3, output=output, mode=AccompanimentMode.STOP)
    assert output.panics[-1] == (1.2, "section_stop")
    assert len(output.panics) == 2


def test_gentle_hold_cancels_plan_without_panicking() -> None:
    """A follower-dropout hold stops scheduling but must not chop sounding notes.

    ``hold_panic=False`` is how the engine enters a dropout hold: the pianist fell
    silent where sound was due, but any orchestra chord still ringing releases on
    its own note-off rather than being slammed off by an all-notes-off. This is
    the cut that silenced the m.45 downbeat. A real STOP still panics.
    """

    scheduler = AccompanimentScheduler(
        ScoreBundle.load(FIXTURE),
        planning_horizon_seconds=2.0,
        dispatch_horizon_seconds=0.05,
    )
    output = CapturingOutput()
    assert scheduler.update(state(1.0, 2.0), now=1.0, output=output).planned

    held = scheduler.update(
        state(1.1, 2.1),
        now=1.1,
        output=output,
        mode=AccompanimentMode.HOLD,
        hold_panic=False,
    )
    assert held.cancelled_event_ids == ("accomp_001", "accomp_002")
    assert held.panic_reason is None
    assert output.panics == []

    # A genuine STOP still panics even after a gentle hold.
    scheduler.update(state(1.2, 2.2), now=1.2, output=output, mode=AccompanimentMode.STOP)
    assert output.panics == [(1.2, "section_stop")]


def test_backward_jump_releases_stop_latch_and_resumes_dispatch() -> None:
    scheduler = AccompanimentScheduler(
        ScoreBundle.load(FIXTURE),
        planning_horizon_seconds=2.0,
        dispatch_horizon_seconds=0.05,
        jump_threshold_beats=1.5,
    )
    output = CapturingOutput()

    scheduler.update(state(0.0, 4.0), now=0.0, output=output)
    stopped = scheduler.update(state(0.1, 4.1), now=0.1, output=output, mode=AccompanimentMode.STOP)
    assert stopped.dispatched == ()
    assert output.panics == [(0.1, "section_stop")]

    repeated = scheduler.update(state(1.0, 0.0), now=1.0, output=output)

    assert repeated.reset_reason == "repeat"
    assert [item.event.event_id for item in repeated.dispatched] == ["accomp_000"]
    assert output.panics[-1] == (1.0, "score_repeat")


def test_backward_jump_does_not_release_explicit_cancel_latch() -> None:
    scheduler = AccompanimentScheduler(
        ScoreBundle.load(FIXTURE),
        planning_horizon_seconds=2.0,
        dispatch_horizon_seconds=0.05,
        jump_threshold_beats=1.5,
    )
    output = CapturingOutput()

    scheduler.update(state(0.0, 4.0), now=0.0, output=output)
    scheduler.cancel(now=0.1, output=output, reason="user_stop")
    repeated = scheduler.update(state(1.0, 0.0), now=1.0, output=output)

    assert repeated.reset_reason == "repeat"
    assert repeated.dispatched == ()
    assert [item[1].event.event_id for item in output.sent] == []

    scheduler.resume()
    resumed = scheduler.update(state(1.1, 0.0), now=1.1, output=output)
    assert [item.event.event_id for item in resumed.dispatched] == ["accomp_000"]


def test_committed_note_dispatches_during_temporary_follower_uncertainty() -> None:
    scheduler = AccompanimentScheduler(
        ScoreBundle.load(FIXTURE),
        planning_horizon_seconds=1.0,
        dispatch_horizon_seconds=0.05,
    )
    output = CapturingOutput()
    scheduler.update(state(1.0, 2.0), now=1.0, output=output)

    uncertain = replace(state(1.5, 2.0), confidence=0.0)
    result = scheduler.update(uncertain, now=1.5, output=output)

    assert [event.event.event_id for event in result.dispatched] == ["accomp_001"]
    assert output.sent[0][0] == pytest.approx(1.5)


def test_shared_reference_coordinate_owns_onset_and_duration_timing() -> None:
    source = ScoreBundle.load(FIXTURE)
    accompaniment = replace(
        source.accompaniment_events[1],
        source_refs={
            **source.accompaniment_events[1].source_refs,
            "source_performance_beat": 10.0,
            "source_performance_duration_beats": 2.0,
        },
    )
    bundle = replace(
        source,
        events=tuple(
            event if event.event_id != accompaniment.event_id else accompaniment
            for event in source.events
        ),
    )
    scheduler = AccompanimentScheduler(bundle, planning_horizon_seconds=1.0)
    output = CapturingOutput()
    tempo = replace(
        state(1.0, 2.0),
        reference_beat=9.0,
        reference_beat_period_seconds=0.25,
    )

    result = scheduler.update(tempo, now=1.0, output=output)
    planned = next(item for item in result.planned if item.event.event_id == "accomp_001")

    assert planned.perf_time == pytest.approx(1.25)
    assert planned.duration_seconds == pytest.approx(0.5)


def test_sounding_release_retimes_with_the_reference_clock() -> None:
    source = ScoreBundle.load(FIXTURE)
    accompaniment = replace(
        source.accompaniment_events[0],
        source_refs={
            **source.accompaniment_events[0].source_refs,
            "source_performance_beat": 0.0,
            "source_performance_duration_beats": 2.0,
        },
    )
    bundle = replace(
        source,
        events=tuple(
            accompaniment if event.event_id == accompaniment.event_id else event
            for event in source.events
        ),
    )
    scheduler = AccompanimentScheduler(
        bundle,
        planning_horizon_seconds=1.0,
        output_advance_ms=50.0,
    )
    output = CapturingOutput(output_advance_ms=50.0)

    scheduler.update(
        replace(
            state(1.0, 0.0),
            reference_beat=0.0,
            reference_beat_period_seconds=0.25,
        ),
        now=1.0,
        output=output,
    )
    scheduler.update(
        replace(
            state(1.1, 0.1),
            reference_beat=0.1,
            reference_beat_period_seconds=0.5,
        ),
        now=1.1,
        output=output,
    )

    event_id, release_at = output.release_retimes[-1]
    assert event_id == accompaniment.event_id
    assert release_at == pytest.approx(2.0)


def test_release_retime_does_not_chase_wall_clock_when_output_is_buffered() -> None:
    """A buffered renderer's score clock may intentionally trail wall time.

    Clamping a score-derived release to the control loop's wall-clock ``now``
    moves the deadline forward on every tick.  The audio timeline can then
    never catch it, so every note sustains until panic.  The output adapter
    owns its rendered cursor and must receive the unmodified score deadline.
    """

    source = ScoreBundle.load(FIXTURE)
    accompaniment = replace(
        source.accompaniment_events[0],
        source_refs={
            **source.accompaniment_events[0].source_refs,
            "source_performance_beat": 0.0,
            "source_performance_duration_beats": 2.0,
        },
    )
    bundle = replace(
        source,
        events=tuple(
            accompaniment if event.event_id == accompaniment.event_id else event
            for event in source.events
        ),
    )
    scheduler = AccompanimentScheduler(
        bundle,
        planning_horizon_seconds=1.0,
        output_advance_ms=50.0,
    )
    output = CapturingOutput(output_advance_ms=50.0)

    scheduler.update(
        replace(
            state(1.0, 0.0),
            reference_beat=0.0,
            reference_beat_period_seconds=0.25,
        ),
        now=1.0,
        output=output,
    )
    scheduler.update(
        replace(
            state(1.025, 0.1),
            reference_beat=0.1,
            reference_beat_period_seconds=0.25,
        ),
        now=2.0,
        output=output,
    )

    event_id, release_at = output.release_retimes[-1]
    assert event_id == accompaniment.event_id
    assert release_at == pytest.approx(1.45)
    assert release_at < 2.0


def test_reference_map_derivative_reports_local_canonical_beat_period() -> None:
    source = ScoreBundle.load(FIXTURE)
    first = replace(
        source.events[0],
        beat=0.0,
        source_refs={**source.events[0].source_refs, "source_performance_beat": 0.0},
    )
    second = replace(
        source.events[1],
        beat=2.0,
        source_refs={**source.events[1].source_refs, "source_performance_beat": 1.0},
    )
    scheduler = AccompanimentScheduler(replace(source, events=(first, second)))

    # One reference beat spans two canonical beats here. The reference clock
    # remains 0.5 s/beat (preserving source rubato), while canonical display
    # and diagnostics correctly report 0.25 s/beat.
    assert scheduler.score_beat_period_at_reference_beat(0.5, 0.5) == pytest.approx(0.25)


def test_score_metronome_calibrates_denser_reference_clock() -> None:
    source = ScoreBundle.load(FIXTURE)
    events = (
        replace(
            source.solo_events[0],
            beat=0.0,
            source_refs={"source_performance_beat": 0.0},
        ),
        replace(
            source.accompaniment_events[0],
            beat=1.0,
            source_refs={"source_performance_beat": 1.0},
        ),
        replace(
            source.solo_events[1],
            beat=4.0,
            source_refs={"source_performance_beat": 10.0},
        ),
        replace(
            source.accompaniment_events[1],
            beat=5.0,
            source_refs={
                "source_performance_beat": 12.5,
                "source_performance_duration_beats": 2.5,
            },
        ),
        replace(
            source.accompaniment_events[2],
            beat=7.0,
            source_refs={"source_performance_beat": 17.5},
        ),
        replace(
            source.solo_events[2],
            beat=8.0,
            source_refs={"source_performance_beat": 20.0},
        ),
    )
    bundle = replace(
        source,
        events=tuple(sorted(events, key=lambda event: (event.beat, event.event_id))),
        section_map=SectionMap(
            piece_id="reference-clock-interlude",
            sections=(
                Section("solo-before", 0.0, 4.0, AccompanimentMode.FOLLOW),
                Section("interlude", 4.0, 8.0, AccompanimentMode.LEAD),
                Section("solo-after", 8.0, 12.0, AccompanimentMode.FOLLOW),
            ),
        ),
    )
    bundle.validate()
    scheduler = AccompanimentScheduler(bundle, planning_horizon_seconds=2.0)
    output = CapturingOutput()

    reference_period = scheduler.reference_period_for_score_tempo(
        start_score_beat=4.0,
        end_score_beat=8.0,
        score_tempo_bpm=90.0,
    )

    # Ten expressive reference units span four printed score quarters. At
    # quarter=90 the section must last 2.667 s, not 6.667 s.
    assert reference_period == pytest.approx((4 * 60 / 90) / 10)
    assert scheduler.score_beat_period_at_reference_beat(12.5, reference_period) == pytest.approx(
        60 / 90
    )

    before_boundary = scheduler.update(
        replace(
            state(1.0, 3.0),
            reference_beat=7.5,
            reference_beat_period_seconds=reference_period,
        ),
        now=1.0,
        output=output,
        mode=AccompanimentMode.FOLLOW,
    )
    assert all(item.event.beat < 4.0 for item in before_boundary.planned)

    in_interlude = scheduler.update(
        replace(
            state(2.0, 4.0),
            reference_beat=10.0,
            reference_beat_period_seconds=reference_period,
        ),
        now=2.0,
        output=output,
        mode=AccompanimentMode.LEAD,
    )
    first = next(item for item in in_interlude.planned if item.event.beat == 5.0)
    assert first.perf_time == pytest.approx(2.0 + 60 / 90)


def test_sounding_note_gets_a_real_note_off_at_a_stop_boundary() -> None:
    """A sustained note must not ring on when the orchestra section stops.

    Live, a sustained orchestra note whose release fell inside the cadenza hung
    ~10 s: entering the FREE/STOP region dropped its pending release and left it
    to the panic's all-notes-off, which the Yamaha did not honour. The boundary
    now retimes every sounding note's release to now, emitting an explicit
    note-off before the panic backstops it.
    """

    scheduler = AccompanimentScheduler(
        ScoreBundle.load(FIXTURE),
        planning_horizon_seconds=2.0,
        dispatch_horizon_seconds=0.05,
    )
    output = CapturingOutput()

    # Dispatch a note so it is sounding (its release is pending).
    scheduler.update(state(0.0, 0.0), now=0.0, output=output)
    sounding = [e.event.event_id for _, e in output.sent]
    assert sounding, "expected a note to be dispatched and sounding"

    # Cross into a STOP section while it is still ringing.
    scheduler.update(state(0.1, 0.1), now=0.1, output=output, mode=AccompanimentMode.STOP)

    assert output.panics and output.panics[-1][1] == "section_stop"
    released = {event_id for event_id, _ in output.release_retimes}
    assert set(sounding) <= released, (
        "sounding notes must be released with an explicit note-off at the stop "
        f"boundary; released={released} sounding={sounding}"
    )
