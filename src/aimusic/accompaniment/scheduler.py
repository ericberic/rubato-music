"""Causal, retimable accompaniment scheduling.

The compatibility :meth:`schedule` API returns events once when they enter a
beat window.  The live API, :meth:`update`, keeps a mutable planning window,
freezes only the near-term dispatch window, and emits events through an output
port when they become due.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Protocol

from aimusic.accompaniment.score_bundle import ScoreBundle, ScoreEvent
from aimusic.accompaniment.section_policy import AccompanimentMode
from aimusic.accompaniment.tempo_model import TempoState

# When a reactive chord fires, previous notes whose written end lands within this
# much of the downbeat are cut so they do not bleed under the new attack. Small
# enough that genuine suspensions (ending well past the downbeat) keep sounding.
_REACTIVE_PRIOR_RELEASE_SLACK_BEATS = 0.25


@dataclass(frozen=True)
class ScheduledAccompanimentEvent:
    """A score event assigned to an absolute performance time."""

    event: ScoreEvent
    perf_time: float
    section_mode: AccompanimentMode
    tempo_bpm: float
    reference_beat: float | None = None
    duration_seconds: float | None = None
    authority_generation: int = 0
    committed_at: float | None = None


@dataclass(frozen=True)
class CurveEventTiming:
    """Integrated timing for one future event along an arrival-time curve."""

    elapsed_seconds: float
    duration_seconds: float
    tempo_bpm: float
    reference_beat: float | None = None


class ArrivalTimeCurve(Protocol):
    """Injectable source of integrated future-event timing."""

    curve_id: str

    def timing_for(
        self,
        event: ScoreEvent,
        tempo_state: TempoState,
        mode: AccompanimentMode,
    ) -> CurveEventTiming | None:
        """Integrate from the current position to ``event`` or return no estimate."""


class ReferenceWarpArrivalCurve:
    """Integrate canonical future positions through the reference performance.

    The shared reference coordinate is proportional to performed time. For a
    piecewise-linear canonical-to-reference warp, integrating its derivative is
    exactly the reference-coordinate difference. Multiplying that difference
    once by the live reference-period scale preserves every intervening
    accelerando and ritardando without flattening the lookahead.
    """

    curve_id = "reference-warp-v1"

    def __init__(self, events: tuple[ScoreEvent, ...] = ()) -> None:
        _reference_to_score, self._score_to_reference = _reference_timeline(events)

    @property
    def score_knots(self) -> tuple[float, ...]:
        """Canonical knots that define the piecewise reference warp."""

        return tuple(score for score, _reference in self._score_to_reference)

    def reference_beat_at_score_beat(self, score_beat: float) -> float | None:
        """Project canonical position to the reference coordinate once."""

        return _interpolate_pairs(self._score_to_reference, score_beat)

    def reference_beat_for_event(self, event: ScoreEvent) -> float | None:
        """Read an event's explicit reference identity without inferring one."""

        return _event_reference_beat(event)

    def timing_for(
        self,
        event: ScoreEvent,
        tempo_state: TempoState,
        mode: AccompanimentMode,
    ) -> CurveEventTiming | None:
        _ = mode
        reference_period = tempo_state.reference_beat_period_seconds
        if reference_period is None or not math.isfinite(reference_period) or reference_period <= 0:
            return None
        start_reference = tempo_state.reference_beat
        event_reference = _event_reference_beat(event)
        if start_reference is None or event_reference is None:
            return None

        source_duration = _event_reference_duration(event)
        duration_seconds = (
            source_duration * reference_period
            if source_duration is not None
            else event.duration_beats * tempo_state.beat_period_seconds
        )
        return CurveEventTiming(
            elapsed_seconds=(event_reference - start_reference) * reference_period,
            duration_seconds=duration_seconds,
            tempo_bpm=60.0 / reference_period,
            reference_beat=event_reference,
        )


class FlatScoreArrivalCurve:
    """Canonical scalar-period baseline for deterministic A/B evaluation."""

    curve_id = "flat-score-v1"

    def timing_for(
        self,
        event: ScoreEvent,
        tempo_state: TempoState,
        mode: AccompanimentMode,
    ) -> CurveEventTiming:
        _ = mode
        return CurveEventTiming(
            elapsed_seconds=(event.beat - tempo_state.score_beat) * tempo_state.beat_period_seconds,
            duration_seconds=event.duration_beats * tempo_state.beat_period_seconds,
            tempo_bpm=tempo_state.tempo_bpm,
        )


class AccompanimentOutput(Protocol):
    """Hardware-independent deadline-output seam owned by the scheduler."""

    def send(self, event: ScheduledAccompanimentEvent, *, sent_at: float) -> None:
        """Commit an immutable event for exact deadline delivery."""

    def cancel_pending(self, event_ids: Iterable[str]) -> bool:
        """Cancel all named committed events atomically, or leave all untouched.

        Returns ``False`` when any named event is no longer pending. This lets a
        reactive replacement decline safely after the deadline worker has begun
        delivery instead of double-playing a note that can no longer be recalled.
        """

    def retime_release(self, event_id: str, *, scheduled_at: float) -> bool:
        """Move a committed note's device release while it is still sounding.

        Note-on delivery is immutable once committed, but note duration is not:
        a later tempo observation can move the notated end of a sustained note.
        ``scheduled_at`` is the output-adapter deadline, after output advance.
        Returns ``False`` after the note has already released or been retriggered.
        """

    def panic(self, *, sent_at: float, reason: str) -> None:
        """Silence all sounding notes immediately."""

    def set_master_volume(self, volume: float, *, sent_at: float) -> None:
        """Set the mix-level multiplier without flattening note expression."""

    def set_output_advance(self, output_advance_ms: float) -> None:
        """Retune the repeatable hardware output-advance for future events."""


@dataclass(frozen=True)
class SchedulerUpdate:
    """Observable result of one scheduler control tick."""

    planned: tuple[ScheduledAccompanimentEvent, ...]
    dispatched: tuple[ScheduledAccompanimentEvent, ...]
    cancelled_event_ids: tuple[str, ...] = ()
    expired_event_ids: tuple[str, ...] = ()
    authority_generation: int = 0
    authority_reason: str | None = None
    reset_reason: str | None = None
    panic_reason: str | None = None


class AccompanimentScheduler:
    """Plan, retime, commit, and causally dispatch accompaniment events.

    ``planning_horizon_seconds`` is the mutable lookahead. Events remain
    mutable until they cross ``dispatch_horizon_seconds`` and are atomically
    transferred to the deadline output. The output queue then owns that
    immutable prefix.
    """

    def __init__(
        self,
        bundle: ScoreBundle,
        lookahead_beats: float = 2.0,
        *,
        planning_horizon_seconds: float = 0.5,
        dispatch_horizon_seconds: float = 0.05,
        output_advance_ms: float = 0.0,
        jump_threshold_beats: float = 2.0,
        late_event_tolerance_seconds: float = 0.05,
        arrival_curve: ArrivalTimeCurve | None = None,
        reactive_event_ids: Iterable[str] = (),
    ) -> None:
        if lookahead_beats < 0:
            raise ValueError("lookahead_beats must be non-negative")
        if planning_horizon_seconds < 0:
            raise ValueError("planning_horizon_seconds must be non-negative")
        if dispatch_horizon_seconds < 0:
            raise ValueError("dispatch_horizon_seconds must be non-negative")
        if dispatch_horizon_seconds > planning_horizon_seconds:
            raise ValueError("dispatch horizon cannot exceed planning horizon")
        if output_advance_ms < 0:
            raise ValueError("output_advance_ms must be non-negative")
        if jump_threshold_beats <= 0:
            raise ValueError("jump_threshold_beats must be positive")
        if late_event_tolerance_seconds < 0:
            raise ValueError("late_event_tolerance_seconds must be non-negative")
        self._events = tuple(sorted(bundle.accompaniment_events, key=_event_sort_key))
        event_ids = {event.event_id for event in self._events}
        self._reactive_event_ids = frozenset(reactive_event_ids)
        unknown_reactive_ids = self._reactive_event_ids - event_ids
        if unknown_reactive_ids:
            raise ValueError(
                "reactive event ids are not present in the accompaniment: "
                + ", ".join(sorted(unknown_reactive_ids))
            )
        self._reference_to_score, self._score_to_reference = _reference_timeline(bundle.events)
        self._arrival_curve = (
            arrival_curve if arrival_curve is not None else ReferenceWarpArrivalCurve(bundle.events)
        )
        self._section_map = bundle.section_map
        self._lookahead_beats = lookahead_beats
        self._planning_horizon_seconds = planning_horizon_seconds
        self._dispatch_horizon_seconds = dispatch_horizon_seconds
        self._output_advance_seconds = output_advance_ms / 1000.0
        self._jump_threshold_beats = jump_threshold_beats
        self._late_event_tolerance_seconds = late_event_tolerance_seconds
        self._scheduled_event_ids: set[str] = set()
        self._dispatched_event_ids: set[str] = set()
        self._expired_event_ids: set[str] = set()
        self._planned: dict[str, ScheduledAccompanimentEvent] = {}
        self._active_releases: dict[str, ScoreEvent] = {}
        self._authority_generation = 0
        self._pending_authority_reason: str | None = None
        self._previous_score_beat: float | None = None
        self._previous_mode: AccompanimentMode | None = None
        self._stopped = False
        self._stopped_by_section = False

    @property
    def first_accompaniment_beat(self) -> float:
        """First sounding accompaniment location, excluding file setup silence."""

        return self._events[0].beat

    @property
    def authority_generation(self) -> int:
        return self._authority_generation

    @property
    def arrival_curve_id(self) -> str:
        return self._arrival_curve.curve_id

    def reference_beat_at_score_beat(self, score_beat: float) -> float | None:
        """Project canonical score position onto the shared reference MIDI."""

        return _interpolate_pairs(self._score_to_reference, score_beat)

    def score_beat_at_reference_beat(self, reference_beat: float) -> float | None:
        """Project the shared reference MIDI position onto canonical score time."""

        return _interpolate_pairs(self._reference_to_score, reference_beat)

    def score_beat_period_at_reference_beat(
        self, reference_beat: float, reference_beat_period_seconds: float
    ) -> float:
        """Return local canonical seconds/beat without flattening source rubato.

        The reference clock deliberately advances uniformly through the source
        performance.  The piecewise source-to-score map carries the expressive
        dilation, so its local derivative converts that source clock into the
        canonical rate reported to policy, traces, and the score cursor.
        """

        score_per_reference = _interpolation_slope(self._reference_to_score, reference_beat)
        if score_per_reference is None or score_per_reference <= 0:
            return reference_beat_period_seconds
        return reference_beat_period_seconds / score_per_reference

    def reference_period_for_score_tempo(
        self,
        *,
        start_score_beat: float,
        end_score_beat: float,
        score_tempo_bpm: float,
    ) -> float:
        """Calibrate a reference clock to a notated quarter-note metronome mark.

        Movement II retains expressive timing in a source-performance
        coordinate whose beat density is not necessarily one-to-one with
        canonical score quarters.  The performer-facing ``♩ = N`` control is a
        score-tempo control, so one autonomous section must take
        ``score_span * 60 / N`` seconds while the source clock preserves the
        relative timing inside that section.
        """

        if end_score_beat <= start_score_beat:
            raise ValueError("end_score_beat must be greater than start_score_beat")
        if score_tempo_bpm <= 0:
            raise ValueError("score_tempo_bpm must be positive")
        start_reference = self.reference_beat_at_score_beat(start_score_beat)
        end_reference = self.reference_beat_at_score_beat(end_score_beat)
        if start_reference is None or end_reference is None or end_reference <= start_reference:
            return 60.0 / score_tempo_bpm
        score_span = end_score_beat - start_score_beat
        reference_span = end_reference - start_reference
        section_seconds = score_span * 60.0 / score_tempo_bpm
        return section_seconds / reference_span

    def schedule(self, tempo_state: TempoState) -> tuple[ScheduledAccompanimentEvent, ...]:
        """Compatibility beat-window scheduler used by existing simulations."""

        if tempo_state.confidence <= 0:
            return ()
        active_section = self._section_map.section_at(tempo_state.score_beat)
        horizon_beat = min(
            tempo_state.score_beat + self._lookahead_beats,
            active_section.end_beat,
        )
        scheduled: list[ScheduledAccompanimentEvent] = []
        for event in self._events:
            if (
                event.event_id in self._reactive_event_ids
                or event.event_id in self._scheduled_event_ids
                or event.beat < tempo_state.score_beat
            ):
                continue
            if event.beat >= active_section.end_beat or event.beat > horizon_beat:
                break
            mode = self._section_map.mode_at(event.beat)
            if mode in {AccompanimentMode.HOLD, AccompanimentMode.STOP}:
                continue
            item = self._schedule_event(event, tempo_state, mode)
            scheduled.append(item)
            self._scheduled_event_ids.add(event.event_id)
        return tuple(scheduled)

    def update(
        self,
        tempo_state: TempoState,
        *,
        now: float,
        output: AccompanimentOutput,
        mode: AccompanimentMode | None = None,
        planning_end_beat: float | None = None,
        hold_panic: bool = True,
    ) -> SchedulerUpdate:
        """Advance the live scheduler by one causal control tick.

        ``hold_panic`` controls only the blunt all-notes-off a HOLD emits on the
        beat it takes over. A follower dropout that merely means "the tracker is
        briefly unsure" must not slam every orchestra note off mid-phrase, so the
        engine passes ``hold_panic=False`` for a gentle dropout hold; sustaining
        notes then release on their own scheduled note-offs instead of being cut.
        A genuine STOP still panics regardless.

        A gentle hold does NOT leave notes hanging: a note-off is committed to the
        output at its note-on's send time (``MidoAccompanimentOutput.send`` pushes
        it onto a deadline heap that a dedicated worker fires), independent of any
        later scheduler tick. The scheduler's ``_active_releases`` bookkeeping only
        *retimes* a sounding release when the tempo moves; skipping it in HOLD lets
        the note ring to its already-committed end, which is the point. (A
        ``CapturingOutput`` test double models no such worker, so asserting note
        hang against it is a false positive.)
        """

        if now < 0:
            raise ValueError("now must be non-negative")
        active_section = self._section_map.section_at(tempo_state.score_beat)
        active_mode = mode or active_section.mode
        authority_reason = self._pending_authority_reason
        self._pending_authority_reason = None
        authority_cancelled: tuple[str, ...] = ()
        if self._previous_mode is not None and self._previous_mode is not active_mode:
            authority_reason = (
                f"mode:{self._previous_mode.value.lower()}->{active_mode.value.lower()}"
            )
            authority_cancelled = self._advance_authority(authority_reason)
            self._pending_authority_reason = None
        if planning_end_beat is None:
            planning_end_beat = active_section.end_beat
        else:
            planning_end_beat = min(planning_end_beat, active_section.end_beat)
        boundary_cancelled = tuple(
            sorted(
                event_id
                for event_id, planned in self._planned.items()
                if planned.event.beat >= planning_end_beat
            )
        )
        for event_id in boundary_cancelled:
            del self._planned[event_id]
        reset_reason: str | None = None
        jump_cancelled: tuple[str, ...] = ()
        panic_reason: str | None = None
        timing_available = tempo_state.confidence > 0 or tempo_state.coasting
        if timing_available and active_mode is not AccompanimentMode.LEAD:
            # LEAD position advances from the scheduler's own monotonic clock.
            # A delayed control tick is not a musical jump and must never panic
            # or cut sustaining orchestra notes.
            reset_reason, jump_cancelled = self._reset_for_jump(tempo_state.score_beat)
            if reset_reason is not None:
                authority_reason = self._pending_authority_reason
                self._pending_authority_reason = None
            if reset_reason == "repeat":
                panic_reason = "score_repeat"
                self._flush_active_releases(now=now, output=output)
                output.panic(sent_at=now, reason=panic_reason)

        if self._stopped or active_mode in {AccompanimentMode.HOLD, AccompanimentMode.STOP}:
            cancelled = tuple(
                sorted(
                    {
                        *authority_cancelled,
                        *boundary_cancelled,
                        *jump_cancelled,
                        *self._cancel_planned(),
                    }
                )
            )
            if active_mode is AccompanimentMode.STOP and not self._stopped:
                panic_reason = "section_stop"
                self._flush_active_releases(now=now, output=output)
                output.panic(sent_at=now, reason=panic_reason)
                self._stopped = True
                self._stopped_by_section = True
            elif (
                active_mode is AccompanimentMode.HOLD
                and self._previous_mode is not AccompanimentMode.HOLD
                and hold_panic
            ):
                panic_reason = "section_hold"
                self._flush_active_releases(now=now, output=output)
                output.panic(sent_at=now, reason=panic_reason)
            self._previous_score_beat = tempo_state.score_beat
            self._previous_mode = active_mode
            return SchedulerUpdate(
                planned=(),
                dispatched=(),
                cancelled_event_ids=cancelled,
                authority_generation=self._authority_generation,
                authority_reason=authority_reason,
                reset_reason=reset_reason,
                panic_reason=panic_reason,
            )

        expired: list[str] = []
        if timing_available:
            self._retime_active_releases(tempo_state, output=output)
        due_before = now + max(
            self._dispatch_horizon_seconds,
            self._output_advance_seconds,
        )
        # Commit the prefix that crossed the boundary under the previous
        # control estimate before applying a new estimate to the still-mutable
        # suffix. This is the producer/consumer ownership transfer.
        dispatched = self._transfer_due(
            now=now,
            due_before=due_before,
            output=output,
            expired=expired,
        )
        if timing_available:
            # Low-confidence tracker output may not move the plan or trigger a
            # jump panic.  Already committed events still dispatch below at
            # their frozen deadlines; otherwise one uncertain piano note can
            # hold an orchestral onset for a full relock window.
            # Output advance is itself a commitment: an event cannot remain mutable
            # after the output deadline at which it may be emitted.
            # Every plan remains mutable until the transfer below. There is no
            # separate "frozen but cancellable" zone.
            for event_id, planned in tuple(self._planned.items()):
                replacement = self._schedule_event(
                    planned.event,
                    tempo_state,
                    self._section_map.mode_at(planned.event.beat),
                )
                if self._is_expired_deadline(replacement, now):
                    if self._crossed_ordinary_event(planned.event, tempo_state, active_mode):
                        self._planned[event_id] = replace(replacement, perf_time=now)
                    else:
                        del self._planned[event_id]
                        self._expire(event_id, expired)
                else:
                    self._planned[event_id] = replacement

            # The ordered score transport owns event completeness. If two
            # ordinary position observations straddle an event that was not in
            # the prior planning window, enqueue it at its derived past
            # deadline so the due-output pass emits it exactly once. A declared
            # skip intentionally resets the transport instead of bursting every
            # crossed note.
            if (
                reset_reason is None
                and self._previous_score_beat is not None
                and self._previous_mode is active_mode
                and tempo_state.score_beat > self._previous_score_beat
            ):
                crossing_start = max(
                    self._previous_score_beat,
                    active_section.start_beat,
                )
                for event in self._events:
                    if event.event_id in self._reactive_event_ids:
                        continue
                    if event.beat <= crossing_start:
                        continue
                    if event.beat >= tempo_state.score_beat:
                        break
                    if self._is_spent(event.event_id) or event.event_id in self._planned:
                        continue
                    if self._section_map.mode_at(event.beat) is not active_mode:
                        continue
                    planned = self._schedule_event(event, tempo_state, active_mode)
                    if self._is_expired_deadline(planned, now):
                        # A normal follower update may jump across a chord as
                        # several notes of the pianist's attack arrive. That is
                        # transport progress, not a score skip: emit the crossed
                        # chord now rather than silently deleting it.
                        self._planned[event.event_id] = replace(planned, perf_time=now)
                    else:
                        self._planned[event.event_id] = planned

            # A time-only 500 ms window is shorter than one beat at Larghetto
            # tempo. Keep a symbolic lookahead as well so normal follower
            # advances cannot silently step over an unscheduled orchestra note.
            horizon_beat = tempo_state.score_beat + max(
                self._lookahead_beats,
                self._planning_horizon_seconds / tempo_state.beat_period_seconds,
            )
            if planning_end_beat is not None:
                horizon_beat = min(horizon_beat, planning_end_beat)
            for event in self._events:
                if event.event_id in self._reactive_event_ids:
                    continue
                if self._is_spent(event.event_id) or event.event_id in self._planned:
                    continue
                if event.beat < tempo_state.score_beat:
                    continue
                if event.beat > horizon_beat:
                    break
                if planning_end_beat is not None and event.beat >= planning_end_beat:
                    break
                event_mode = self._section_map.mode_at(event.beat)
                if event_mode not in {AccompanimentMode.HOLD, AccompanimentMode.STOP}:
                    planned = self._schedule_event(event, tempo_state, event_mode)
                    if self._is_expired_deadline(planned, now):
                        self._expire(event.event_id, expired)
                    else:
                        self._planned[event.event_id] = planned

        # Transfer immutable events to a dedicated deadline output before their
        # wall-clock onset. The output worker—not the follower loop—owns the
        # final wait and any configured hardware advance.
        dispatched.extend(
            self._transfer_due(
                now=now,
                due_before=due_before,
                output=output,
                expired=expired,
            )
        )

        if timing_available:
            self._previous_score_beat = tempo_state.score_beat
        self._previous_mode = active_mode
        return SchedulerUpdate(
            planned=tuple(sorted(self._planned.values(), key=_scheduled_sort_key)),
            dispatched=tuple(dispatched),
            cancelled_event_ids=tuple(
                sorted(
                    {
                        *authority_cancelled,
                        *boundary_cancelled,
                        *jump_cancelled,
                    }
                )
            ),
            expired_event_ids=tuple(expired),
            authority_generation=self._authority_generation,
            authority_reason=authority_reason,
            reset_reason=reset_reason,
            panic_reason=panic_reason,
        )

    def cancel(self, *, now: float, output: AccompanimentOutput, reason: str) -> tuple[str, ...]:
        """Cancel future output and panic; used for user stop and device loss."""

        cancelled = self._cancel_planned()
        self._advance_authority(f"cancel:{reason}", cancel_planned=False)
        output.panic(sent_at=now, reason=reason)
        self._active_releases.clear()
        self._stopped = True
        self._stopped_by_section = False
        return cancelled

    def pause(self, *, score_beat: float | None = None) -> tuple[str, ...]:
        """Cancel future plans without silencing notes that already sound.

        This is the musical handoff used when an orchestra-led opening reaches
        the pianist's entry.  A panic here would chop off the cue's final
        sustained note, while retaining planned future events would let the
        orchestra run away before the follower has located the pianist.
        """

        cancelled = self._cancel_planned()
        self._advance_authority("pause", cancel_planned=False)
        self._previous_mode = AccompanimentMode.HOLD
        if score_beat is not None:
            self._previous_score_beat = score_beat
        return cancelled

    def resume(self) -> tuple[str, ...]:
        """Allow planning after an explicit stop; already dispatched events stay spent."""

        self._stopped = False
        self._stopped_by_section = False
        return self._advance_authority("resume")

    def reanchor(self, reason: str) -> tuple[str, ...]:
        """Invalidate mutable plans after a discontinuous clock correction."""

        return self._advance_authority(reason)

    def mark_dispatched(self, event_ids: Iterable[str]) -> None:
        """Retire events an external path has emitted, so the plan won't repeat them.

        The reactive beat-anchor firer emits a marked beat's chord itself; this
        removes those events from the mutable plan and records them spent so the
        normal deadline pass cannot dispatch them a second time.
        """

        for event_id in event_ids:
            self._planned.pop(event_id, None)
            self._dispatched_event_ids.add(event_id)

    def dispatch_reactively(
        self,
        events: Iterable[ScheduledAccompanimentEvent],
        *,
        now: float,
        output: AccompanimentOutput,
        onset_beat: float | None = None,
    ) -> bool:
        """Atomically supersede any committed deadlines with reactive events.

        The scheduler owns the mutable/committed boundary. A beat anchor may
        replace an event that is still mutable, or a committed event that the
        deadline output can cancel atomically. Once any chord event has begun
        delivery, the whole reactive chord is declined so it cannot double-play
        or partially replace the original voicing.
        """

        replacements = tuple(events)
        event_ids = tuple(item.event.event_id for item in replacements)
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("reactive dispatch event ids must be unique")
        already_committed = tuple(
            event_id for event_id in event_ids if event_id in self._dispatched_event_ids
        )
        if already_committed and not output.cancel_pending(already_committed):
            return False
        for event_id in event_ids:
            self._planned.pop(event_id, None)
            self._expired_event_ids.discard(event_id)
            self._dispatched_event_ids.add(event_id)
        # A reactive chord fires at the pianist's actual onset, which can arrive
        # before the tempo model retires the previous beat's notes. Any still-
        # sounding note whose written end is at/before this downbeat (e.g. m44's
        # 4th beat when the m45 chord fires) must come off now, or it bleeds
        # under the new attack. Notes written to sustain past the downbeat keep
        # their own release.
        if onset_beat is not None:
            reserved = set(event_ids)
            release_at = now - self._output_advance_seconds
            for prior_id, prior_event in tuple(self._active_releases.items()):
                if prior_id in reserved:
                    continue
                prior_end = prior_event.beat + prior_event.duration_beats
                if (
                    prior_event.beat < onset_beat
                    and prior_end <= onset_beat + _REACTIVE_PRIOR_RELEASE_SLACK_BEATS
                ):
                    output.retime_release(prior_id, scheduled_at=release_at)
                    del self._active_releases[prior_id]
        for replacement in replacements:
            output.send(replacement, sent_at=now)
            self._active_releases[replacement.event.event_id] = replacement.event
        return True

    def _cancel_planned(self) -> tuple[str, ...]:
        cancelled = tuple(sorted(self._planned))
        self._planned.clear()
        return cancelled

    def _reset_for_jump(
        self,
        score_beat: float,
    ) -> tuple[str | None, tuple[str, ...]]:
        previous = self._previous_score_beat
        if previous is None:
            return None, ()
        delta = score_beat - previous
        if abs(delta) < self._jump_threshold_beats:
            return None, ()
        cancelled = self._advance_authority("score_repeat" if delta < 0 else "score_skip")
        if self._stopped_by_section:
            self._stopped = False
            self._stopped_by_section = False
        if delta < 0:
            # Repeated material is a new occurrence. Events at/after the target
            # may sound again; earlier events remain spent.
            self._dispatched_event_ids = {
                event_id
                for event_id in self._dispatched_event_ids
                if self._event_by_id(event_id).beat < score_beat
            }
            self._expired_event_ids = {
                event_id
                for event_id in self._expired_event_ids
                if self._event_by_id(event_id).beat < score_beat
            }
            return "repeat", cancelled
        return "skip", cancelled

    def _is_spent(self, event_id: str) -> bool:
        return event_id in self._dispatched_event_ids or event_id in self._expired_event_ids

    def _advance_authority(
        self,
        reason: str,
        *,
        cancel_planned: bool = True,
    ) -> tuple[str, ...]:
        cancelled = self._cancel_planned() if cancel_planned else ()
        self._authority_generation += 1
        self._pending_authority_reason = reason
        return cancelled

    def _is_expired_deadline(
        self,
        planned: ScheduledAccompanimentEvent,
        now: float,
    ) -> bool:
        return planned.perf_time < now - self._late_event_tolerance_seconds - 1e-9

    def _crossed_ordinary_event(
        self,
        event: ScoreEvent,
        tempo_state: TempoState,
        active_mode: AccompanimentMode,
    ) -> bool:
        previous = self._previous_score_beat
        return (
            previous is not None
            and self._previous_mode is active_mode
            and previous < event.beat < tempo_state.score_beat
            and abs(tempo_state.score_beat - previous) < self._jump_threshold_beats
        )

    def _flush_active_releases(
        self, *, now: float, output: AccompanimentOutput
    ) -> None:
        """Send a real note-off for every sounding note, then forget them.

        A section boundary used to drop pending releases and trust the panic's
        all-notes-off to silence what was sounding. On the Yamaha that left a
        sustained orchestra note ringing ~10 s into the cadenza: CC123 did not
        reliably release it. Retiming each active release to *now* emits an
        explicit note-off per note, which the device honours, before the panic
        fires as a backstop.
        """

        for event_id in tuple(self._active_releases):
            output.retime_release(event_id, scheduled_at=now)
        self._active_releases.clear()

    def _retime_active_releases(
        self,
        tempo_state: TempoState,
        *,
        output: AccompanimentOutput,
    ) -> None:
        """Keep sounding note ends on the same mutable score clock as onsets."""

        for event_id, event in tuple(self._active_releases.items()):
            scheduled = self._schedule_event(
                event,
                tempo_state,
                self._section_map.mode_at(event.beat),
            )
            duration = scheduled.duration_seconds
            if duration is None:  # pragma: no cover - scheduler always supplies it
                continue
            release_at = scheduled.perf_time + duration - self._output_advance_seconds
            if not output.retime_release(
                event_id,
                scheduled_at=release_at,
            ):
                del self._active_releases[event_id]

    def _expire(self, event_id: str, expired: list[str]) -> None:
        if event_id not in self._expired_event_ids:
            self._expired_event_ids.add(event_id)
            expired.append(event_id)

    def _transfer_due(
        self,
        *,
        now: float,
        due_before: float,
        output: AccompanimentOutput,
        expired: list[str],
    ) -> list[ScheduledAccompanimentEvent]:
        dispatched: list[ScheduledAccompanimentEvent] = []
        for event_id, planned in sorted(
            tuple(self._planned.items()),
            key=lambda item: _scheduled_sort_key(item[1]),
        ):
            if planned.authority_generation != self._authority_generation:
                self._expire(event_id, expired)
                del self._planned[event_id]
                continue
            if self._is_expired_deadline(planned, now):
                self._expire(event_id, expired)
                del self._planned[event_id]
                continue
            if planned.perf_time > due_before:
                continue
            output.send(planned, sent_at=now)
            dispatched.append(planned)
            self._dispatched_event_ids.add(event_id)
            self._active_releases[event_id] = planned.event
            del self._planned[event_id]
        return dispatched

    def _event_by_id(self, event_id: str) -> ScoreEvent:
        return next(event for event in self._events if event.event_id == event_id)

    def _schedule_event(
        self,
        event: ScoreEvent,
        tempo_state: TempoState,
        mode: AccompanimentMode,
    ) -> ScheduledAccompanimentEvent:
        """Map one event through the strongest available shared timeline.

        Movement II's solo reference and orchestra were split from the same
        Oguri MIDI, so their original beat coordinate is an exact alignment
        seam.  Canonical beats still own score identity and the cursor; the
        shared reference coordinate owns orchestral onset/duration timing.
        """

        curve_timing = self._arrival_curve.timing_for(event, tempo_state, mode)
        if curve_timing is not None:
            return ScheduledAccompanimentEvent(
                event=event,
                perf_time=tempo_state.perf_time + curve_timing.elapsed_seconds,
                section_mode=mode,
                tempo_bpm=curve_timing.tempo_bpm,
                reference_beat=curve_timing.reference_beat,
                duration_seconds=curve_timing.duration_seconds,
                authority_generation=self._authority_generation,
            )
        return replace(
            _schedule_event(event, tempo_state, mode),
            authority_generation=self._authority_generation,
        )


def _schedule_event(
    event: ScoreEvent, tempo_state: TempoState, mode: AccompanimentMode
) -> ScheduledAccompanimentEvent:
    beat_delta = event.beat - tempo_state.score_beat
    return ScheduledAccompanimentEvent(
        event=event,
        perf_time=tempo_state.perf_time + beat_delta * tempo_state.beat_period_seconds,
        section_mode=mode,
        tempo_bpm=tempo_state.tempo_bpm,
        duration_seconds=event.duration_beats * tempo_state.beat_period_seconds,
    )


def _event_reference_beat(event: ScoreEvent) -> float | None:
    value = event.source_refs.get("source_performance_beat")
    return float(value) if isinstance(value, (int, float)) else None


def _event_reference_duration(event: ScoreEvent) -> float | None:
    value = event.source_refs.get("source_performance_duration_beats")
    return float(value) if isinstance(value, (int, float)) and value > 0 else None


def _reference_timeline(
    events: tuple[ScoreEvent, ...],
) -> tuple[tuple[tuple[float, float], ...], tuple[tuple[float, float], ...]]:
    """Build monotonic interpolation tables between source and score beats."""

    by_reference: dict[float, list[float]] = {}
    for event in events:
        reference = _event_reference_beat(event)
        if reference is not None:
            by_reference.setdefault(reference, []).append(event.beat)
    reference_to_score = tuple(
        (reference, sum(beats) / len(beats)) for reference, beats in sorted(by_reference.items())
    )
    by_score: dict[float, list[float]] = {}
    for reference, score in reference_to_score:
        by_score.setdefault(score, []).append(reference)
    score_to_reference = tuple(
        (score, sum(references) / len(references)) for score, references in sorted(by_score.items())
    )
    return reference_to_score, score_to_reference


def _interpolate_pairs(pairs: tuple[tuple[float, float], ...], position: float) -> float | None:
    if not pairs:
        return None
    if len(pairs) == 1:
        return pairs[0][1]
    xs = tuple(pair[0] for pair in pairs)
    right = bisect.bisect_right(xs, position)
    if right == 0:
        left, right = 0, 1
    elif right >= len(pairs):
        left, right = len(pairs) - 2, len(pairs) - 1
    else:
        left = right - 1
    x0, y0 = pairs[left]
    x1, y1 = pairs[right]
    if x1 == x0:
        return y0
    return y0 + (position - x0) * (y1 - y0) / (x1 - x0)


def _interpolation_slope(pairs: tuple[tuple[float, float], ...], position: float) -> float | None:
    """Slope of the same segment selected by :func:`_interpolate_pairs`."""

    if len(pairs) < 2:
        return None
    xs = tuple(pair[0] for pair in pairs)
    right = bisect.bisect_right(xs, position)
    if right == 0:
        left, right = 0, 1
    elif right >= len(pairs):
        left, right = len(pairs) - 2, len(pairs) - 1
    else:
        left = right - 1
    x0, y0 = pairs[left]
    x1, y1 = pairs[right]
    if x1 == x0:
        return None
    return (y1 - y0) / (x1 - x0)


def _event_sort_key(event: ScoreEvent) -> tuple[float, str]:
    return event.beat, event.event_id


def _scheduled_sort_key(event: ScheduledAccompanimentEvent) -> tuple[float, float, str]:
    return event.perf_time, event.event.beat, event.event.event_id
