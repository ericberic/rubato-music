"""Predictive FOLLOW clock: linear tempo expectation with a leading phase (LTE).

Why the reactive clock lags where it matters
--------------------------------------------
The shipped FOLLOW clock is reactive: it anchors the orchestra to the *last
place the follower detected the pianist* and extrapolates forward. Two things
follow from that anchoring, and both were visible in the measure-44 trace:

1. The orchestra can only sound an arrival beat once the follower has *detected*
   the pianist crossing it -- roughly one processing delay after the note is
   played. On a broadening climax that delay is audible.
2. Re-anchoring to each detected note means the clock never commits to a
   predicted arrival; it always snaps back to where the pianist already was.

Following the reference performance's rubato *shape* (which the reactive clock
already does when a reference position is present) fixes neither: shape-following
sets the spacing between future events, but the whole plan is still pinned to the
pianist's detected position.

What LTE adds
-------------
This clock keeps the reactive model's stable pace estimate (seconds per
reference beat, which does not lag a ritardando because the rubato lives in the
score<->reference map, not in the pace) and adds a **phase lock that leads**.

On each confident note it predicts the arrival time from its own running anchor,
measures the error against when the note was actually detected, and advances the
anchor by only *part* of that error. The unclosed part is the lead: the orchestra
plays to the model's predicted arrival instead of waiting for detection. With a
lead fraction of 0 the anchor snaps to the note and the behaviour is exactly
reactive; with a positive fraction the orchestra anticipates by that share of the
detection/trajectory error, bounded so a misprediction cannot run away.

Expectation source and its limit
--------------------------------
The phase lead below uses the aligned reference performance. When the fitted
rehearsal Interpretation is available, :class:`InterpretationArrivalCurve`
adds Eric's repeatable future tempo shape at the scheduler boundary. Per-cell
dispersion continuously gates that fitted curve back to the reference/reactive
baseline, and unsupported evidence has zero trust. The bounded phase lead and
the dispersion-gated future curve remain separate controls: one addresses
follower/detection phase, the other expectation geometry.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable
from dataclasses import dataclass, replace

from aimusic.accompaniment.following import FollowerUpdate
from aimusic.accompaniment.scheduler import (
    CurveEventTiming,
    ReferenceWarpArrivalCurve,
)
from aimusic.accompaniment.score_bundle import ScoreEvent
from aimusic.accompaniment.section_policy import AccompanimentMode
from aimusic.accompaniment.tempo_model import (
    OnlineTempoModel,
    TempoModel,
    TempoObservation,
    TempoState,
    TimingSeed,
)


def dispersion_trust_gain(
    expected_period_seconds: float | None,
    dispersion_seconds: float | None,
    *,
    tolerance_ratio: float = 0.15,
) -> float:
    """Map relative tempo MAD continuously onto bounded predictive trust.

    Missing, invalid, or under-supported dispersion is represented by ``None``
    and degrades fully to the reactive/reference curve. At or above the
    tolerance ratio, the gain is exactly zero; zero dispersion is gain one.
    """

    if tolerance_ratio <= 0:
        raise ValueError("tolerance_ratio must be positive")
    if (
        expected_period_seconds is None
        or dispersion_seconds is None
        or not math.isfinite(expected_period_seconds)
        or not math.isfinite(dispersion_seconds)
        or expected_period_seconds <= 0
        or dispersion_seconds < 0
    ):
        return 0.0
    relative_dispersion = dispersion_seconds / expected_period_seconds
    return min(1.0, max(0.0, 1.0 - relative_dispersion / tolerance_ratio))


class InterpretationArrivalCurve:
    """Blend a fitted canonical tempo curve with the reference-warp baseline.

    The prior is keyed in canonical 960-PPQ ticks. The reference curve owns the
    canonical-to-reference seam; this wrapper converts each integration sample
    to a canonical tick once, obtains expected period and dispersion, and blends
    local elapsed time by a gain in ``[0, 1]``. Gain zero is byte-for-byte the
    reference/reactive interval, so missing or high-dispersion evidence cannot
    alter that baseline.
    """

    def __init__(
        self,
        *,
        reference_curve: ReferenceWarpArrivalCurve,
        expected_period_at_tick: Callable[[float], float | None],
        dispersion_at_tick: Callable[[float], float | None],
        canonical_ppq: int = 960,
        grid_step_ticks: int = 480,
        tolerance_ratio: float = 0.15,
        curve_id: str = "interpretation-dispersion-v1",
    ) -> None:
        if canonical_ppq <= 0:
            raise ValueError("canonical_ppq must be positive")
        if grid_step_ticks <= 0:
            raise ValueError("grid_step_ticks must be positive")
        if tolerance_ratio <= 0:
            raise ValueError("tolerance_ratio must be positive")
        if not curve_id:
            raise ValueError("curve_id must not be empty")
        self._reference_curve = reference_curve
        self._expected_period_at_tick = expected_period_at_tick
        self._dispersion_at_tick = dispersion_at_tick
        self._canonical_ppq = canonical_ppq
        self._grid_step_beats = grid_step_ticks / canonical_ppq
        self._tolerance_ratio = tolerance_ratio
        self.curve_id = curve_id
        # Prefix-sum tables over canonical cells. Built here, at run setup, and
        # never during a performance: the build walks the whole score and was
        # measured at ~600 ms, which as a lazy first-note cost showed up as a
        # single enormous process_note outlier.
        self._boundaries: tuple[float, ...] = ()
        self._boundary_refs: tuple[float | None, ...] = ()
        self._prefix_a: tuple[float, ...] = (0.0,)
        self._prefix_b: tuple[float, ...] = (0.0,)
        self._ensure_tables()

    def trust_gain_at(self, score_beat: float) -> float:
        """Read fitted evidence at one canonical position."""

        canonical_tick = score_beat * self._canonical_ppq
        return dispersion_trust_gain(
            self._expected_period_at_tick(canonical_tick),
            self._dispersion_at_tick(canonical_tick),
            tolerance_ratio=self._tolerance_ratio,
        )

    def timing_for(
        self,
        event: ScoreEvent,
        tempo_state: TempoState,
        mode: AccompanimentMode,
    ) -> CurveEventTiming | None:
        reactive = self._reference_curve.timing_for(event, tempo_state, mode)
        if (
            reactive is None
            or mode is not AccompanimentMode.FOLLOW
            or event.beat <= tempo_state.score_beat
        ):
            return reactive

        reference_period = tempo_state.reference_beat_period_seconds
        start_reference = tempo_state.reference_beat
        end_reference = self._reference_curve.reference_beat_for_event(event)
        if reference_period is None or start_reference is None or end_reference is None:
            return reactive

        # Every segment contributes ``A + B * reference_period`` where A and B
        # depend only on score geometry and the (static) fitted profile. The
        # interior segments of any query are always the same canonical grid/knot
        # cells, so their A and B are prefix-summed once at construction and a
        # query costs two lookups instead of re-integrating the whole span. Only
        # the two partial end cells are computed live, because they carry the
        # live reference beat and the event's own reference identity.
        self._ensure_tables()
        start_beat, end_beat = tempo_state.score_beat, event.beat
        boundaries = self._boundaries
        first = bisect.bisect_right(boundaries, start_beat)
        last = bisect.bisect_left(boundaries, end_beat) - 1

        if first > last:
            # Both endpoints fall inside one canonical cell: a single segment.
            terms = self._segment_terms(start_beat, end_beat, start_reference, end_reference)
            if terms is None:
                return reactive
            total_a, total_b = terms
        else:
            head = self._segment_terms(
                start_beat, boundaries[first], start_reference, self._boundary_refs[first]
            )
            tail = self._segment_terms(
                boundaries[last], end_beat, self._boundary_refs[last], end_reference
            )
            if head is None or tail is None:
                return reactive
            total_a = head[0] + (self._prefix_a[last] - self._prefix_a[first]) + tail[0]
            total_b = head[1] + (self._prefix_b[last] - self._prefix_b[first]) + tail[1]

        return replace(reactive, elapsed_seconds=total_a + total_b * reference_period)

    def _segment_terms(
        self,
        left: float,
        right: float,
        left_reference: float | None,
        right_reference: float | None,
    ) -> tuple[float, float] | None:
        """Split one cell's contribution into (static, reference-scaled) parts."""

        if left_reference is None or right_reference is None:
            return None
        reference_delta = right_reference - left_reference
        canonical_tick = ((left + right) / 2) * self._canonical_ppq
        expected_period = self._expected_period_at_tick(canonical_tick)
        gain = dispersion_trust_gain(
            expected_period,
            self._dispersion_at_tick(canonical_tick),
            tolerance_ratio=self._tolerance_ratio,
        )
        if expected_period is not None and math.isfinite(expected_period) and expected_period > 0:
            return gain * (right - left) * expected_period, (1.0 - gain) * reference_delta
        # Without a fitted period the cell degrades to the reactive interval,
        # which is entirely reference-scaled.
        return 0.0, reference_delta

    def _ensure_tables(self) -> None:
        """Build canonical cell boundaries and their prefix sums once."""

        if self._boundaries:
            return
        knots = self._reference_curve.score_knots
        if not knots:
            self._boundaries = ()
            self._boundary_refs = ()
            self._prefix_a = (0.0,)
            self._prefix_b = (0.0,)
            return
        points = set(knots)
        grid = math.floor(knots[0] / self._grid_step_beats) * self._grid_step_beats
        while grid <= knots[-1]:
            points.add(grid)
            grid += self._grid_step_beats
        boundaries = tuple(sorted(points))
        refs = tuple(
            self._reference_curve.reference_beat_at_score_beat(beat) for beat in boundaries
        )
        prefix_a = [0.0]
        prefix_b = [0.0]
        for index in range(len(boundaries) - 1):
            terms = self._segment_terms(
                boundaries[index], boundaries[index + 1], refs[index], refs[index + 1]
            )
            a, b = terms if terms is not None else (0.0, 0.0)
            prefix_a.append(prefix_a[-1] + a)
            prefix_b.append(prefix_b[-1] + b)
        self._boundaries = boundaries
        self._boundary_refs = refs
        self._prefix_a = tuple(prefix_a)
        self._prefix_b = tuple(prefix_b)

    def _integration_points(self, start_beat: float, end_beat: float) -> tuple[float, ...]:
        points = {start_beat, end_beat}
        first_grid = math.floor(start_beat / self._grid_step_beats) + 1
        grid = first_grid * self._grid_step_beats
        while grid < end_beat:
            points.add(grid)
            grid += self._grid_step_beats
        points.update(
            knot for knot in self._reference_curve.score_knots if start_beat < knot < end_beat
        )
        return tuple(sorted(points))


class LteTempoModel:
    """Reactive pace estimate plus a bounded, leading phase lock.

    ``project_reference_beat`` maps a canonical score beat onto the shared
    reference timeline, returning ``None`` when the bundle has no reference map;
    it is satisfied by
    :meth:`~aimusic.accompaniment.scheduler.AccompanimentScheduler.reference_beat_at_score_beat`.

    The tracker's live position is jittery (it can jump a beat and then pause),
    so a per-note phase correction chases that jitter and oscillates. The lead is
    therefore a *slowly adapting, non-negative, bounded* offset: it grows only
    when the pianist is genuinely arriving later than the stable pace predicts
    (a broadening / ritardando), it is smoothed so tracker jitter averages out,
    and it is clamped to ``[0, max_lead_seconds]`` so it can only ever move the
    orchestra earlier -- never later than the reactive clock, and never runaway.

    ``lead_fraction`` is the share of the observed lateness the lead targets;
    ``lead_gain`` is the EMA rate at which the offset adapts; ``min_progress_ref``
    ignores tiny/backward reference steps (chord spread, jitter) when measuring
    lateness. These defaults are un-tuned starting points, not searched values.
    """

    def __init__(
        self,
        *,
        base: OnlineTempoModel,
        project_reference_beat: Callable[[float], float | None],
        lead_fraction: float = 0.5,
        lead_gain: float = 0.3,
        max_lead_seconds: float = 0.15,
        min_progress_ref: float = 0.3,
    ) -> None:
        if not 0.0 <= lead_fraction <= 1.0:
            raise ValueError("lead_fraction must be in [0, 1]")
        if not 0.0 < lead_gain <= 1.0:
            raise ValueError("lead_gain must be in (0, 1]")
        if max_lead_seconds < 0:
            raise ValueError("max_lead_seconds must be non-negative")
        if min_progress_ref <= 0:
            raise ValueError("min_progress_ref must be positive")
        self._base = base
        self._project_reference_beat = project_reference_beat
        self._lead_fraction = lead_fraction
        self._lead_gain = lead_gain
        self._max_lead_seconds = max_lead_seconds
        self._min_progress_ref = min_progress_ref
        self._anchor_time: float | None = None
        self._anchor_ref: float | None = None
        self._lead_seconds = 0.0

    @property
    def last_observation(self) -> TempoObservation:
        return self._base.last_observation

    def reset_timing_reference(self) -> None:
        # A new autonomous LEAD invalidates the phase lock; re-acquire it from
        # the next confident note and let the lead decay back in from zero.
        self._base.reset_timing_reference()
        self._anchor_time = None
        self._anchor_ref = None
        self._lead_seconds = 0.0

    def seed_timing(self, seed: TimingSeed) -> TempoState:
        """Establish cue-derived phase without treating it as piano evidence."""

        state = self._base.seed_timing(seed)
        self._anchor_time = seed.perf_time
        self._anchor_ref = seed.reference_beat
        self._lead_seconds = 0.0
        return state

    def update(self, follower_update: FollowerUpdate) -> TempoState:
        if follower_update.reference_beat is None:
            reference_beat = self._project_reference_beat(follower_update.score_beat)
            if reference_beat is not None:
                follower_update = replace(follower_update, reference_beat=reference_beat)

        state = self._base.update(follower_update)
        reference_beat = state.reference_beat
        pace = state.reference_beat_period_seconds
        detected_time = state.perf_time
        if reference_beat is None or pace is None:
            # No reference timeline for this bundle: nothing to lead against.
            return state

        if self._anchor_time is not None and self._anchor_ref is not None:
            progress = reference_beat - self._anchor_ref
            elapsed = detected_time - self._anchor_time
            if progress >= self._min_progress_ref and elapsed > 1e-3:
                # How much later than the stable pace did this note arrive? A
                # broadening makes this positive; steady/faster playing makes it
                # <= 0, so the lead decays back toward the reactive clock.
                lateness = detected_time - (self._anchor_time + pace * progress)
                target = self._lead_fraction * max(0.0, lateness)
                self._lead_seconds += self._lead_gain * (target - self._lead_seconds)
                self._lead_seconds = min(self._max_lead_seconds, max(0.0, self._lead_seconds))
                self._anchor_time = detected_time
                self._anchor_ref = reference_beat
        else:
            self._anchor_time = detected_time
            self._anchor_ref = reference_beat

        if self._lead_seconds <= 0.0:
            return state
        return replace(state, perf_time=detected_time - self._lead_seconds)


@dataclass(frozen=True)
class PaceProfile:
    """Phrase-level rehearsal pace prior, addressed by canonical score beat.

    ``smoothed_period_at`` returns the bar-smoothed expected seconds/quarter
    (already scaled to the requested base tempo) and ``support_at`` the number of
    takes behind it. The prior only claims a pace where the rehearsals actually
    voted (``min_support``); elsewhere it returns ``None`` and the runtime keeps
    the reactive estimate.
    """

    smoothed_period_at: Callable[[float], float | None]
    support_at: Callable[[float], int]
    min_support: int = 2

    def trusted_period(self, score_beat: float) -> float | None:
        if self.support_at(score_beat) < self.min_support:
            return None
        period = self.smoothed_period_at(score_beat)
        if period is None or not math.isfinite(period) or period <= 0:
            return None
        return period


class RehearsalAnchoredTempoModel:
    """Play the rehearsed pace; let live evidence only scale it, slowly.

    The rehearsal profile is the performance plan the pianist and orchestra
    agreed on. Where it is supported, the pace is the phrase-level rehearsal
    tempo times **one slowly-adapting scale** — today's overall pace relative to
    rehearsal. So follower jitter cannot manufacture tempo fluctuation (the
    scale integrates it away over many notes), and missing or sparse evidence
    holds the plan instead of chasing noise. Where the profile has no support the
    pace degrades to the wrapped reactive/LTE model.

    Only the pace *magnitude* is re-anchored. Position stabilization, the
    reference/canonical relationship, and any predictive phase lead stay with the
    wrapped model; the reference period is rescaled by the same factor as the
    canonical period so the two coordinates remain consistent.
    """

    def __init__(
        self,
        *,
        base: TempoModel,
        profile: PaceProfile,
        scale_gain: float = 0.05,
        min_scale: float = 0.4,
        max_scale: float = 2.5,
    ) -> None:
        if not 0.0 < scale_gain <= 1.0:
            raise ValueError("scale_gain must be in (0, 1]")
        if not 0.0 < min_scale < max_scale:
            raise ValueError("scale bounds must be positive and ordered")
        self._base = base
        self._profile = profile
        self._scale_gain = scale_gain
        self._min_scale = min_scale
        self._max_scale = max_scale
        self._scale: float | None = None

    @property
    def last_observation(self) -> TempoObservation:
        return self._base.last_observation

    def reset_timing_reference(self) -> None:
        self._base.reset_timing_reference()

    def seed_timing(self, seed: TimingSeed) -> TempoState:
        state = self._base.seed_timing(seed)
        period = self._profile.trusted_period(seed.score_beat)
        if period is not None and seed.beat_period_seconds > 0:
            self._scale = self._clamp_scale(seed.beat_period_seconds / period)
        return self._anchor(state)

    def update(self, follower_update: FollowerUpdate) -> TempoState:
        return self._anchor(self._base.update(follower_update))

    def _clamp_scale(self, scale: float) -> float:
        return min(self._max_scale, max(self._min_scale, scale))

    def _anchor(self, state: TempoState) -> TempoState:
        period = self._profile.trusted_period(state.score_beat)
        if period is None or state.beat_period_seconds <= 0:
            # Unsupported passage: keep the reactive pace unchanged.
            return state
        # The scale is today's overall pace relative to rehearsal. Adapt it only
        # from *accepted* tempo candidates — real beat-level evidence — never
        # from the model's stale initial period or from chord/jitter positions
        # that were rejected. Each candidate is one noisy sample; the small gain
        # integrates many of them into a stable scale.
        observation = self._base.last_observation
        if (
            observation.accepted
            and observation.candidate_tempo_bpm is not None
            and observation.candidate_tempo_bpm > 0
        ):
            ratio = self._clamp_scale((60.0 / observation.candidate_tempo_bpm) / period)
            if self._scale is None:
                self._scale = ratio
            else:
                self._scale += self._scale_gain * (ratio - self._scale)
        if self._scale is None:
            # No beat-level evidence yet: stay reactive rather than guess a scale.
            return state
        canonical_period = period * self._scale
        reference_period = state.reference_beat_period_seconds
        if reference_period is not None:
            reference_period = reference_period * (canonical_period / state.beat_period_seconds)
        return replace(
            state,
            beat_period_seconds=canonical_period,
            tempo_bpm=60.0 / canonical_period,
            reference_beat_period_seconds=reference_period,
        )


__all__ = [
    "InterpretationArrivalCurve",
    "LteTempoModel",
    "PaceProfile",
    "RehearsalAnchoredTempoModel",
    "dispersion_trust_gain",
]
