"""Reactive beat anchors: hit the orchestra chord *with* the pianist's bass.

A human marks a strong beat (see :class:`aimusic.takes.models.Anchor`). Live, the
instant a pianist note lands on that beat's score position, this fires the
orchestra chord for that beat *reactively* -- before any follower/tempo work --
so it lands with the pianist instead of a tempo-model prediction behind them. The
source recording's natural chord roll is preserved: the chord's first note lands
on the bass, later notes keep their relative offset.

Design notes:

- The firer takes plain canonical ``anchor_ticks`` (not the persisted model), so
  the real-time path has no dependency on the takes/persistence layer.
- It reaches the scheduler only through the public
  :meth:`AccompanimentScheduler.dispatch_reactively` seam, which atomically
  replaces still-pending deadline output and declines once delivery has begun.
- An anchor fires only when the previous confident follower position is near
  the marked beat and the incoming pitch matches the lowest solo pitch there.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence

from aimusic.accompaniment.scheduler import (
    AccompanimentOutput,
    AccompanimentScheduler,
    ScheduledAccompanimentEvent,
)
from aimusic.accompaniment.score_bundle import ScoreBundle, ScoreEvent
from aimusic.accompaniment.section_policy import AccompanimentMode

CANONICAL_PPQ = 960
_TRIGGER_WINDOW_BEATS = 0.25  # absorbs human click imprecision around a notated onset
_CHORD_WINDOW_BEATS = (0.25, 0.6)  # accompaniment span gathered as "this beat's chord"
# Anchors run before observing the triggering note, so the last follower
# position may legitimately be one beat behind. Once it is more than a click
# tolerance past the mark, firing would be a late/repeated-note false positive.
_MAX_PRIOR_POSITION_LAG_BEATS = 1.25
_MAX_PAST_POSITION_BEATS = _TRIGGER_WINDOW_BEATS
_FALLBACK_PERIOD_SECONDS = 1.2

logger = logging.getLogger(__name__)


class AnchorFirer:
    """Fire a marked beat's orchestra chord the instant the pianist's bass lands."""

    def __init__(self, anchor_ticks: Sequence[int], bundle: ScoreBundle) -> None:
        self._chords: dict[float, tuple[ScoreEvent, ...]] = {}
        self._triggers: dict[int, list[float]] = {}  # solo pitch -> anchor beats it can fire
        self._fired: set[float] = set()
        for tick in anchor_ticks:
            beat = tick / CANONICAL_PPQ
            chord = tuple(
                event
                for event in bundle.accompaniment_events
                if beat - _CHORD_WINDOW_BEATS[0] <= event.beat <= beat + _CHORD_WINDOW_BEATS[1]
            )
            if not chord:
                continue
            nearby_solo = tuple(
                event
                for event in bundle.solo_events
                if event.pitch is not None and abs(event.beat - beat) <= _TRIGGER_WINDOW_BEATS
            )
            if not nearby_solo:
                continue
            # An anchor is explicitly a pianist bass marker, not "any pitch
            # somewhere near this beat." Choosing the lowest nearby solo pitch
            # makes octave/chord figures deterministic and sharply reduces
            # ornament/repeated-note false positives.
            trigger_pitch = min(event.pitch for event in nearby_solo if event.pitch is not None)
            self._chords[beat] = chord
            self._triggers.setdefault(trigger_pitch, []).append(beat)
        if self._chords:
            logger.info(
                "Beat anchors armed: %d beat(s) %s",
                len(self._chords),
                sorted(round(b, 2) for b in self._chords),
            )

    @property
    def is_empty(self) -> bool:
        return not self._chords

    @property
    def owned_event_ids(self) -> frozenset[str]:
        """Events whose onset authority belongs exclusively to this firer.

        The ordinary scheduler must not predict these notes first. Otherwise a
        slightly early prediction can begin delivery milliseconds before the
        pianist's bass arrives, making the reactive path correctly refuse a
        double attack but defeating the anchor's musical purpose.
        """

        return frozenset(
            event.event_id for chord in self._chords.values() for event in chord
        )

    @property
    def armed_cues(self) -> tuple[tuple[float, int], ...]:
        """Canonical beat and bass pitch for each usable reactive cue."""

        return tuple(
            sorted(
                (beat, pitch)
                for pitch, beats in self._triggers.items()
                for beat in beats
            )
        )

    def maybe_fire(
        self,
        pitch: int,
        now: float,
        score_beat: float | None,
        scheduler: AccompanimentScheduler,
        output: AccompanimentOutput,
        beat_period: float | None,
        reference_beat_period: float | None = None,
    ) -> bool:
        if score_beat is None:
            return False
        candidates = [
            beat
            for beat in self._triggers.get(pitch, ())
            if beat not in self._fired
            and beat - _MAX_PRIOR_POSITION_LAG_BEATS
            <= score_beat
            <= beat + _MAX_PAST_POSITION_BEATS
        ]
        if not candidates:
            return False
        # Coordinate distance, never insertion order, decides between nearby
        # anchors sharing a bass pitch.
        beat = min(candidates, key=lambda candidate: (abs(candidate - score_beat), candidate))
        return self._fire(
            beat,
            now,
            scheduler,
            output,
            beat_period,
            reference_beat_period,
        )

    def _fire(
        self,
        beat: float,
        now: float,
        scheduler: AccompanimentScheduler,
        output: AccompanimentOutput,
        beat_period: float | None,
        reference_beat_period: float | None,
    ) -> bool:
        chord = self._chords[beat]
        score_period = _positive_finite(beat_period)
        reference_period = _positive_finite(reference_beat_period)
        duration_period = score_period or _FALLBACK_PERIOD_SECONDS
        reference_beats = tuple(_reference_beat(event) for event in chord)
        use_reference_roll = reference_period is not None and all(
            value is not None for value in reference_beats
        )
        if use_reference_roll:
            reference_origin = min(value for value in reference_beats if value is not None)
        else:
            score_origin = min(event.beat for event in chord)
        replacements: list[ScheduledAccompanimentEvent] = []
        pitches: list[int | None] = []
        for event, event_reference_beat in zip(chord, reference_beats, strict=True):
            # Convert exactly once at this seam. Source-performance offsets use
            # the source/reference period; canonical offsets are only a fallback
            # for bundles without source-performance coordinates.
            if use_reference_roll:
                assert event_reference_beat is not None
                roll_offset = (event_reference_beat - reference_origin) * reference_period
            elif score_period is not None:
                roll_offset = (event.beat - score_origin) * score_period
            else:
                # Tracker loss must not smear a roll using an invented tempo.
                roll_offset = 0.0
            duration_seconds = _duration_seconds(
                event,
                score_period=duration_period,
                reference_period=reference_period,
            )
            replacements.append(
                ScheduledAccompanimentEvent(
                    event=event,
                    perf_time=now + roll_offset,
                    section_mode=AccompanimentMode.FOLLOW,
                    tempo_bpm=60.0 / (reference_period or duration_period),
                    reference_beat=event_reference_beat,
                    duration_seconds=max(0.1, duration_seconds),
                    authority_generation=scheduler.authority_generation,
                )
            )
            pitches.append(event.pitch)
        if not scheduler.dispatch_reactively(
            replacements, now=now, output=output, onset_beat=beat
        ):
            logger.info(
                "Anchor skipped beat %.2f: scheduled chord had already begun delivery",
                beat,
            )
            return False
        self._fired.add(beat)
        logger.info("Anchor fired beat %.2f: %d orch notes %s", beat, len(replacements), pitches)
        return True


def _positive_finite(value: float | None) -> float | None:
    if value is None or not math.isfinite(value) or value <= 0:
        return None
    return value


def _reference_beat(event: ScoreEvent) -> float | None:
    value = event.source_refs.get("source_performance_beat")
    return float(value) if isinstance(value, (int, float)) else None


def _duration_seconds(
    event: ScoreEvent,
    *,
    score_period: float,
    reference_period: float | None,
) -> float:
    source_duration = event.source_refs.get("source_performance_duration_beats")
    if (
        reference_period is not None
        and isinstance(source_duration, (int, float))
        and source_duration > 0
    ):
        return float(source_duration) * reference_period
    return event.duration_beats * score_period


__all__ = ["AnchorFirer"]
