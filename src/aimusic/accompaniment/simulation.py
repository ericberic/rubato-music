"""Deterministic simulated-online harness for accompaniment runtime tests."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from aimusic.accompaniment.following import FollowerUpdate, PerformedNote, ScoreFollower
from aimusic.accompaniment.scheduler import AccompanimentScheduler, ScheduledAccompanimentEvent
from aimusic.accompaniment.score_bundle import ScoreBundle, ScoreEvent
from aimusic.accompaniment.tempo_model import OnlineTempoModel, TempoState

BeatPeriodFn = Callable[[float], float]
VelocityFn = Callable[[ScoreEvent], int]


@dataclass(frozen=True)
class SimulationTrace:
    """Outputs captured from a simulated online run."""

    performed_notes: tuple[PerformedNote, ...]
    follower_updates: tuple[FollowerUpdate, ...]
    tempo_states: tuple[TempoState, ...]
    scheduled_events: tuple[ScheduledAccompanimentEvent, ...]


def generate_synthetic_performance(
    solo_events: Iterable[ScoreEvent],
    *,
    start_perf_time: float = 0.0,
    beat_period_fn: BeatPeriodFn | None = None,
    velocity_fn: VelocityFn | None = None,
) -> tuple[PerformedNote, ...]:
    """Create performed solo notes with known score beats for oracle simulations."""

    events = tuple(sorted(solo_events, key=lambda event: (event.beat, event.event_id)))
    if not events:
        return ()

    period_for = beat_period_fn or (lambda _beat: 0.5)
    velocity_for = velocity_fn or (lambda event: event.velocity or 64)
    current_time = start_perf_time
    previous_beat = events[0].beat
    performed: list[PerformedNote] = []

    for event in events:
        beat_delta = event.beat - previous_beat
        if beat_delta < 0:
            raise ValueError("solo_events must be sortable by non-decreasing beat")
        current_time += beat_delta * period_for(previous_beat)
        if event.pitch is None:
            previous_beat = event.beat
            continue
        performed.append(
            PerformedNote(
                perf_time=current_time,
                pitch=event.pitch,
                velocity=velocity_for(event),
                score_beat=event.beat,
                event_id=event.event_id,
            )
        )
        previous_beat = event.beat
    return tuple(performed)


def hide_score_beats(performed_notes: Iterable[PerformedNote]) -> tuple[PerformedNote, ...]:
    """Return performed notes without oracle-only score-beat annotations."""

    return tuple(
        dataclasses.replace(note, score_beat=None, event_id=None)
        for note in performed_notes
    )


def run_simulated_online(
    bundle: ScoreBundle,
    performed_notes: Iterable[PerformedNote],
    follower: ScoreFollower,
    tempo_model: OnlineTempoModel,
    scheduler: AccompanimentScheduler,
) -> SimulationTrace:
    """Replay performed notes through follower, tempo model, and scheduler."""

    notes = tuple(sorted(performed_notes, key=lambda note: note.perf_time))
    follower_updates: list[FollowerUpdate] = []
    tempo_states: list[TempoState] = []
    scheduled_events: list[ScheduledAccompanimentEvent] = []

    # Touching bundle here makes the runtime dependency explicit for future trace metadata.
    _ = bundle.metadata.piece_id

    for note in notes:
        update = follower.observe(note)
        if update is None:
            continue
        follower_updates.append(update)
        tempo_state = tempo_model.update(update)
        tempo_states.append(tempo_state)
        scheduled_events.extend(scheduler.schedule(tempo_state))

    return SimulationTrace(
        performed_notes=notes,
        follower_updates=tuple(follower_updates),
        tempo_states=tuple(tempo_states),
        scheduled_events=tuple(scheduled_events),
    )
