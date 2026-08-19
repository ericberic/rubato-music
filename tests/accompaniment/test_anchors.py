"""Reactive beat anchors fire the marked chord on the pianist's bass."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from aimusic.accompaniment.anchors import AnchorFirer
from aimusic.accompaniment.midi_output import DeadlineAccompanimentOutput
from aimusic.accompaniment.runtime_io import CapturingOutput
from aimusic.accompaniment.scheduler import (
    AccompanimentScheduler,
    ScheduledAccompanimentEvent,
)
from aimusic.accompaniment.score_bundle import ScoreBundle
from aimusic.accompaniment.section_policy import AccompanimentMode
from aimusic.accompaniment.tempo_model import TempoState

FIXTURE = Path("tests/fixtures/score_bundles/synthetic_accompaniment_capability")
# beat 3 in the fixture has solo pitch 65 (the trigger) and accompaniment pitch 55.
_ANCHOR_TICK = 3 * 960
_BASS_PITCH = 65


def _firer_and_scheduler() -> tuple[AnchorFirer, AccompanimentScheduler, CapturingOutput]:
    bundle = ScoreBundle.load(FIXTURE)
    firer = AnchorFirer([_ANCHOR_TICK], bundle)
    return (
        firer,
        AccompanimentScheduler(bundle, reactive_event_ids=firer.owned_event_ids),
        CapturingOutput(),
    )


def _fire(firer, scheduler, output, *, pitch, score_beat, now=10.0) -> None:
    firer.maybe_fire(pitch, now, score_beat, scheduler, output, beat_period=0.5)


def test_anchor_fires_matching_chord_reactively() -> None:
    firer, scheduler, output = _firer_and_scheduler()
    assert not firer.is_empty

    _fire(firer, scheduler, output, pitch=_BASS_PITCH, score_beat=3.0)

    fired = [event for _t, event in output.sent]
    assert [e.event.event_id for e in fired] == ["accomp_001"]
    assert fired[0].perf_time == pytest.approx(10.0)  # the chord lands on the bass
    # ...and the scheduler will not play it a second time on its normal pass.
    assert "accomp_001" in scheduler._dispatched_event_ids


def test_anchor_does_not_fire_on_a_non_anchor_pitch() -> None:
    firer, scheduler, output = _firer_and_scheduler()
    _fire(firer, scheduler, output, pitch=99, score_beat=3.0)
    assert output.sent == []


def test_anchor_does_not_fire_when_the_follower_is_elsewhere() -> None:
    firer, scheduler, output = _firer_and_scheduler()
    # Correct pitch, but the follower places the pianist far from the anchor beat.
    _fire(firer, scheduler, output, pitch=_BASS_PITCH, score_beat=0.0)
    assert output.sent == []


def test_anchor_fires_only_once() -> None:
    firer, scheduler, output = _firer_and_scheduler()
    _fire(firer, scheduler, output, pitch=_BASS_PITCH, score_beat=3.0)
    _fire(firer, scheduler, output, pitch=_BASS_PITCH, score_beat=3.0, now=10.5)
    assert len(output.sent) == 1


def test_anchor_uses_the_lowest_solo_pitch_as_its_explicit_trigger() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    higher_chord_note = replace(
        bundle.solo_events[-3],
        event_id="solo_003_higher",
        pitch=72,
    )
    bundle = replace(
        bundle,
        events=tuple(sorted((*bundle.events, higher_chord_note), key=lambda event: event.beat)),
    )
    firer = AnchorFirer([_ANCHOR_TICK], bundle)
    scheduler = AccompanimentScheduler(bundle)
    output = CapturingOutput()

    _fire(firer, scheduler, output, pitch=72, score_beat=3.0)
    assert output.sent == []
    _fire(firer, scheduler, output, pitch=_BASS_PITCH, score_beat=3.0)
    assert [item.event.event_id for _, item in output.sent] == ["accomp_001"]


def test_anchor_replaces_a_committed_deadline_instead_of_double_dispatching() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    firer = AnchorFirer([_ANCHOR_TICK], bundle)
    scheduler = AccompanimentScheduler(
        bundle,
        planning_horizon_seconds=1.0,
        dispatch_horizon_seconds=0.1,
    )
    captured = CapturingOutput()
    output = DeadlineAccompanimentOutput(
        captured,
        now=lambda: 10.0,
        autostart=False,
    )
    tempo = TempoState(
        perf_time=10.0,
        score_beat=2.9,
        beat_period_seconds=1.0,
        tempo_bpm=60.0,
        confidence=1.0,
    )

    committed = scheduler.update(tempo, now=10.0, output=output)
    assert [item.event.event_id for item in committed.dispatched] == ["accomp_001"]
    assert output.pending_count == 1

    assert firer.maybe_fire(
        _BASS_PITCH,
        10.02,
        3.0,
        scheduler,
        output,
        beat_period=1.0,
    )
    assert output.pending_count == 1
    output.flush_due(10.02)
    output.flush_due(11.0)

    assert len(captured.sent) == 1
    assert captured.sent[0][1].event.event_id == "accomp_001"
    assert captured.sent[0][1].perf_time == pytest.approx(10.02)


def test_anchor_owned_chord_waits_for_bass_instead_of_dispatching_predictively() -> None:
    """Regression: m.44 beats 2-3 began just before their reactive bass notes."""

    bundle = ScoreBundle.load(FIXTURE)
    firer = AnchorFirer([_ANCHOR_TICK], bundle)
    scheduler = AccompanimentScheduler(
        bundle,
        planning_horizon_seconds=1.0,
        dispatch_horizon_seconds=0.1,
        reactive_event_ids=firer.owned_event_ids,
    )
    output = CapturingOutput()
    tempo = TempoState(
        perf_time=10.0,
        score_beat=2.9,
        beat_period_seconds=1.0,
        tempo_bpm=60.0,
        confidence=1.0,
    )

    update = scheduler.update(tempo, now=10.0, output=output)
    assert update.planned == ()
    assert update.dispatched == ()
    assert output.sent == []

    assert firer.maybe_fire(
        _BASS_PITCH,
        10.2,
        3.0,
        scheduler,
        output,
        beat_period=1.0,
    )
    assert [item.event.event_id for _, item in output.sent] == ["accomp_001"]


def test_anchor_roll_uses_reference_coordinate_and_reference_period_once() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    first = replace(
        bundle.accompaniment_events[1],
        source_refs={"source_performance_beat": 100.0},
    )
    rolled = replace(
        first,
        event_id="accomp_roll",
        beat=3.1,
        pitch=56,
        source_refs={"source_performance_beat": 100.05},
    )
    events = tuple(
        sorted(
            (event for event in bundle.events if event.event_id != first.event_id),
            key=lambda event: event.beat,
        )
    )
    bundle = replace(
        bundle,
        events=tuple(sorted((*events, first, rolled), key=lambda event: event.beat)),
    )
    firer = AnchorFirer([_ANCHOR_TICK], bundle)
    scheduler = AccompanimentScheduler(bundle)
    output = CapturingOutput()

    assert firer.maybe_fire(
        _BASS_PITCH,
        10.0,
        3.0,
        scheduler,
        output,
        beat_period=2.0,
        reference_beat_period=0.5,
    )

    by_id = {item.event.event_id: item for _, item in output.sent}
    assert by_id["accomp_001"].perf_time == pytest.approx(10.0)
    assert by_id["accomp_roll"].perf_time == pytest.approx(10.025)


def test_anchor_collapses_roll_when_period_is_not_finite() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    first = bundle.accompaniment_events[1]
    rolled = replace(first, event_id="accomp_roll", beat=3.1, pitch=56)
    bundle = replace(
        bundle,
        events=tuple(sorted((*bundle.events, rolled), key=lambda event: event.beat)),
    )
    firer = AnchorFirer([_ANCHOR_TICK], bundle)
    scheduler = AccompanimentScheduler(bundle)
    output = CapturingOutput()

    assert firer.maybe_fire(
        _BASS_PITCH,
        10.0,
        3.0,
        scheduler,
        output,
        beat_period=float("inf"),
    )
    assert {item.perf_time for _, item in output.sent} == {10.0}


def test_anchor_firer_is_empty_when_no_chord_covers_the_beat() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    # Beat 1 has a solo note but no accompaniment chord nearby.
    firer = AnchorFirer([1 * 960], bundle)
    assert firer.is_empty


def _sounding(event_id: str, beat: float, duration_beats: float):
    """A ScoreEvent stand-in reusing the fixture's required fields."""
    bundle = ScoreBundle.load(FIXTURE)
    return replace(
        bundle.accompaniment_events[0],
        event_id=event_id,
        beat=beat,
        duration_beats=duration_beats,
    )


def _scheduled(event, perf_time):
    return ScheduledAccompanimentEvent(
        event=event,
        perf_time=perf_time,
        section_mode=AccompanimentMode.FOLLOW,
        tempo_bpm=120.0,
    )


def test_reactive_dispatch_releases_prior_note_ending_on_the_downbeat() -> None:
    # Regression: a reactive chord (e.g. m45 beat 1) fires at the pianist's actual
    # onset, which can arrive before the tempo model retires the previous note
    # (m44 beat 4). That prior note must be released now so it does not bleed
    # under the new attack; a genuine suspension that sustains past the downbeat
    # must keep sounding.
    bundle = ScoreBundle.load(FIXTURE)
    scheduler = AccompanimentScheduler(bundle)
    output = CapturingOutput()

    prior = _sounding("prior_beat4", beat=2.0, duration_beats=1.0)  # ends AT the downbeat
    suspension = _sounding("suspension", beat=2.0, duration_beats=4.0)  # sustains well past
    downbeat = _sounding("downbeat_chord", beat=3.0, duration_beats=1.0)

    # Sound the two prior notes (no onset_beat -> no prior-release sweep).
    scheduler.dispatch_reactively(
        [_scheduled(prior, 9.0), _scheduled(suspension, 9.0)],
        now=9.0,
        output=output,
        onset_beat=None,
    )
    assert {"prior_beat4", "suspension"} <= set(scheduler._active_releases)

    # Fire the reactive downbeat chord.
    assert scheduler.dispatch_reactively(
        [_scheduled(downbeat, 10.0)],
        now=10.0,
        output=output,
        onset_beat=3.0,
    )

    released = {event_id for event_id, _ in output.release_retimes}
    assert "prior_beat4" in released  # cut at the downbeat
    assert "suspension" not in released  # keeps its own release
    assert "prior_beat4" not in scheduler._active_releases
    assert "suspension" in scheduler._active_releases
