"""Causal position stabilization and robust beat-level tempo estimation."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal, Protocol, Self, runtime_checkable

from aimusic.accompaniment.following import FollowerUpdate

PositionAction = Literal["initial", "advance", "clamp_backward_jitter", "repeat_reset"]
TempoDecision = Literal[
    "timing_seed",
    "initial_anchor",
    "accepted",
    "insufficient_progress",
    "nonpositive_time",
    "implausible_tempo",
    "repeat_reset",
]


@dataclass(frozen=True)
class TempoState:
    """Current timing state used to map score beats into performance time."""

    perf_time: float
    score_beat: float
    beat_period_seconds: float
    tempo_bpm: float
    confidence: float
    reference_beat: float | None = None
    reference_beat_period_seconds: float | None = None
    # A short, bounded continuation from the last confident symbolic anchor.
    # The raw follower confidence remains visible while the scheduler is
    # explicitly authorized to keep planning during this grace interval.
    coasting: bool = False


@dataclass(frozen=True)
class TimingSeed:
    """Non-observational clock origin supplied by authored runtime policy.

    A count-off establishes phase and pace, but it is not follower evidence.
    Keeping that cue on a separate API prevents startup from manufacturing a
    confidence-1 :class:`FollowerUpdate` merely to initialize model internals.
    """

    perf_time: float
    score_beat: float
    beat_period_seconds: float
    reference_beat: float | None = None
    reference_beat_period_seconds: float | None = None


@dataclass(frozen=True)
class _TempoAnchor:
    """Internal timing baseline; deliberately carries no confidence/evidence."""

    perf_time: float
    score_beat: float
    reference_beat: float | None = None

    @classmethod
    def from_follower(
        cls,
        update: FollowerUpdate,
        *,
        score_beat: float | None = None,
    ) -> Self:
        return cls(
            perf_time=update.perf_time,
            score_beat=update.score_beat if score_beat is None else score_beat,
            reference_beat=update.reference_beat,
        )


@dataclass(frozen=True)
class TempoObservation:
    """Why one follower estimate did or did not change the tempo clock."""

    raw_score_beat: float
    stabilized_score_beat: float
    position_action: PositionAction
    decision: TempoDecision
    accepted: bool
    beat_delta: float | None = None
    time_delta: float | None = None
    candidate_tempo_bpm: float | None = None


@runtime_checkable
class TempoModel(Protocol):
    """Runtime seam for the FOLLOW clock (reactive EMA or predictive LTE).

    ``LiveEngine`` drives whichever model the run selected; both consume a
    follower estimate and return a :class:`TempoState`, expose the reason the
    last update did or did not move the clock, and can drop their timing
    reference after an autonomous LEAD without forgetting learned pace.
    """

    def update(self, follower_update: FollowerUpdate) -> TempoState: ...

    def seed_timing(self, seed: TimingSeed) -> TempoState: ...

    def reset_timing_reference(self) -> None: ...

    @property
    def last_observation(self) -> TempoObservation: ...


class OnlineTempoModel:
    """Separate symbolic position evidence from beat-level tempo evidence.

    A score follower may emit several positions for the notes of one chord.
    Those positions are useful location evidence, but their millisecond spacing
    is not a musical beat duration. Tempo is therefore updated only after a
    sufficiently large score/time baseline, rejected outside a plausible
    range, and exponentially smoothed at the configured response rate.
    """

    def __init__(
        self,
        initial_tempo_bpm: float = 120.0,
        smoothing_alpha: float = 0.25,
        min_progress_beats: float = 0.5,
        min_elapsed_seconds: float = 0.12,
        min_tempo_bpm: float = 20.0,
        max_tempo_bpm: float = 400.0,
        reset_backward_beats: float = 2.0,
    ) -> None:
        if initial_tempo_bpm <= 0:
            raise ValueError("initial_tempo_bpm must be positive")
        if not 0 < smoothing_alpha <= 1:
            raise ValueError("smoothing_alpha must be in (0, 1]")
        if min_progress_beats <= 0:
            raise ValueError("min_progress_beats must be positive")
        if min_elapsed_seconds <= 0:
            raise ValueError("min_elapsed_seconds must be positive")
        if not 0 < min_tempo_bpm < max_tempo_bpm:
            raise ValueError("tempo bounds must be positive and ordered")
        if reset_backward_beats <= 0:
            raise ValueError("reset_backward_beats must be positive")
        self._beat_period_seconds = 60.0 / initial_tempo_bpm
        self._reference_beat_period_seconds = 60.0 / initial_tempo_bpm
        self._smoothing_alpha = smoothing_alpha
        self._min_progress_beats = min_progress_beats
        self._min_elapsed_seconds = min_elapsed_seconds
        self._min_tempo_bpm = min_tempo_bpm
        self._max_tempo_bpm = max_tempo_bpm
        self._reset_backward_beats = reset_backward_beats
        self._tempo_anchor: _TempoAnchor | None = None
        self._stabilized_score_beat: float | None = None
        self._last_observation: TempoObservation | None = None

    @property
    def last_observation(self) -> TempoObservation:
        if self._last_observation is None:
            raise RuntimeError("tempo model has not observed a follower update")
        return self._last_observation

    def reset_timing_reference(self) -> None:
        """Keep learned pace but require a fresh anchor after an autonomous lead."""

        self._tempo_anchor = None

    def seed_timing(self, seed: TimingSeed) -> TempoState:
        """Seed phase/pace from a cue without claiming a follower observation."""

        if (
            not math.isfinite(seed.perf_time)
            or not math.isfinite(seed.score_beat)
            or seed.perf_time < 0
            or seed.score_beat < 0
        ):
            raise ValueError("timing seed coordinates must be non-negative")
        if not math.isfinite(seed.beat_period_seconds) or seed.beat_period_seconds <= 0:
            raise ValueError("timing seed beat period must be positive")
        if seed.reference_beat_period_seconds is not None and (
            not math.isfinite(seed.reference_beat_period_seconds)
            or seed.reference_beat_period_seconds <= 0
        ):
            raise ValueError("timing seed reference period must be positive")
        if seed.reference_beat is not None and not math.isfinite(seed.reference_beat):
            raise ValueError("timing seed reference beat must be finite")
        self._beat_period_seconds = seed.beat_period_seconds
        if seed.reference_beat_period_seconds is not None:
            self._reference_beat_period_seconds = seed.reference_beat_period_seconds
        self._stabilized_score_beat = seed.score_beat
        self._tempo_anchor = _TempoAnchor(
            perf_time=seed.perf_time,
            score_beat=seed.score_beat,
            reference_beat=seed.reference_beat,
        )
        self._last_observation = TempoObservation(
            raw_score_beat=seed.score_beat,
            stabilized_score_beat=seed.score_beat,
            position_action="initial",
            decision="timing_seed",
            accepted=False,
        )
        return TempoState(
            perf_time=seed.perf_time,
            score_beat=seed.score_beat,
            beat_period_seconds=seed.beat_period_seconds,
            tempo_bpm=60.0 / seed.beat_period_seconds,
            confidence=0.0,
            reference_beat=seed.reference_beat,
            reference_beat_period_seconds=seed.reference_beat_period_seconds,
            coasting=True,
        )

    def update(self, follower_update: FollowerUpdate) -> TempoState:
        raw_beat = follower_update.score_beat
        previous_position = self._stabilized_score_beat
        if previous_position is None:
            stable_beat = raw_beat
            position_action: PositionAction = "initial"
        elif raw_beat < previous_position - self._reset_backward_beats:
            stable_beat = raw_beat
            position_action = "repeat_reset"
        elif raw_beat < previous_position:
            stable_beat = previous_position
            position_action = "clamp_backward_jitter"
        else:
            stable_beat = raw_beat
            position_action = "advance"
        self._stabilized_score_beat = stable_beat

        anchor = self._tempo_anchor
        decision: TempoDecision
        accepted = False
        beat_delta: float | None = None
        time_delta: float | None = None
        candidate_bpm: float | None = None
        if anchor is None:
            self._tempo_anchor = _TempoAnchor.from_follower(follower_update)
            decision = "initial_anchor"
        else:
            beat_delta = stable_beat - anchor.score_beat
            time_delta = follower_update.perf_time - anchor.perf_time
            if position_action == "repeat_reset":
                self._tempo_anchor = _TempoAnchor.from_follower(follower_update)
                decision = "repeat_reset"
            elif time_delta <= 0:
                decision = "nonpositive_time"
            elif beat_delta < self._min_progress_beats or time_delta < self._min_elapsed_seconds:
                decision = "insufficient_progress"
            else:
                candidate_period = time_delta / beat_delta
                candidate_bpm = 60.0 / candidate_period
                if not self._min_tempo_bpm <= candidate_bpm <= self._max_tempo_bpm:
                    decision = "implausible_tempo"
                else:
                    alpha = self._smoothing_alpha
                    self._beat_period_seconds = (
                        alpha * candidate_period + (1.0 - alpha) * self._beat_period_seconds
                    )
                    if (
                        follower_update.reference_beat is not None
                        and anchor.reference_beat is not None
                    ):
                        reference_delta = follower_update.reference_beat - anchor.reference_beat
                        if reference_delta > 0:
                            reference_period = time_delta / reference_delta
                            reference_bpm = 60.0 / reference_period
                            if self._min_tempo_bpm <= reference_bpm <= self._max_tempo_bpm:
                                self._reference_beat_period_seconds = (
                                    alpha * reference_period
                                    + (1.0 - alpha) * self._reference_beat_period_seconds
                                )
                    self._tempo_anchor = _TempoAnchor.from_follower(
                        follower_update,
                        score_beat=stable_beat,
                    )
                    decision = "accepted"
                    accepted = True

        self._last_observation = TempoObservation(
            raw_score_beat=raw_beat,
            stabilized_score_beat=stable_beat,
            position_action=position_action,
            decision=decision,
            accepted=accepted,
            beat_delta=beat_delta,
            time_delta=time_delta,
            candidate_tempo_bpm=candidate_bpm,
        )
        return TempoState(
            perf_time=follower_update.perf_time,
            score_beat=stable_beat,
            beat_period_seconds=self._beat_period_seconds,
            tempo_bpm=60.0 / self._beat_period_seconds,
            confidence=follower_update.confidence,
            reference_beat=follower_update.reference_beat,
            reference_beat_period_seconds=(
                self._reference_beat_period_seconds
                if follower_update.reference_beat is not None
                else None
            ),
        )


def _theil_sen_fit(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Robust line fit ``y = slope * x + intercept`` from >= 2 points.

    Theil-Sen is the median of all pairwise slopes, with the intercept taken as
    the median residual. It is used instead of least squares because the score
    follower advances in discrete jumps (it can leap a beat and then pause), so a
    single outlier onset would tilt an ordinary regression; a median is immune to
    a minority of such jumps. Returns ``None`` when the x-values do not spread.
    """

    if len(points) < 2:
        return None
    slopes: list[float] = []
    for i in range(len(points)):
        xi, yi = points[i]
        for j in range(i + 1, len(points)):
            xj, yj = points[j]
            dx = xj - xi
            if abs(dx) < 1e-9:
                continue
            slopes.append((yj - yi) / dx)
    if not slopes:
        return None
    slope = _median(slopes)
    intercept = _median([y - slope * x for x, y in points])
    return slope, intercept


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


@dataclass(frozen=True)
class EntryPaceEstimate:
    """Pianist pace/phase at a cue-in, fit to piano onsets alone.

    Coordinates stay explicit: ``canonical_*`` is the 960-PPQ notation beat,
    ``reference_*`` is the reference-performance beat. Both are robust line fits
    over the collected onsets and can be evaluated at any performance time so the
    caller seeds phase from the pianist's *current* position, not a stale onset.
    The pre-entry orchestra clock never contributes: it is a warp artifact, not
    performer evidence.
    """

    canonical_beats_per_second: float
    canonical_intercept_beats: float
    reference_beats_per_second: float | None
    reference_intercept_beats: float | None
    onset_count: int
    canonical_span_beats: float
    elapsed_seconds: float

    @property
    def canonical_beat_period_seconds(self) -> float:
        return 1.0 / self.canonical_beats_per_second

    @property
    def tempo_bpm(self) -> float:
        return self.canonical_beats_per_second * 60.0

    @property
    def reference_beat_period_seconds(self) -> float | None:
        if self.reference_beats_per_second is None or self.reference_beats_per_second <= 0:
            return None
        return 1.0 / self.reference_beats_per_second

    def canonical_beat_at(self, perf_time: float) -> float:
        return self.canonical_intercept_beats + self.canonical_beats_per_second * perf_time

    def reference_beat_at(self, perf_time: float) -> float | None:
        if self.reference_beats_per_second is None or self.reference_intercept_beats is None:
            return None
        return self.reference_intercept_beats + self.reference_beats_per_second * perf_time


@dataclass
class EntryPaceAcquisition:
    """Collect piano onsets at an orchestra cue-in and fit a robust pace.

    Position acquisition is not tempo acquisition. Two matched notes localize the
    phrase but span ~0 beats, so any pace read from them is noise -- in the
    measure-48 cue-in trace the pianist had advanced 0.016 beats at the second
    match, which regresses to ~2 BPM. This collector therefore waits until the
    pianist has actually played across a musical span, then fits the onsets
    *only* (never blending the pre-entry orchestra seed) and reports a pace/phase
    the runtime can clamp onto in one step.

    Defaults are trace-derived starting points, not searched values. Two live
    cue-in traces show the fit only settles on the ~51 BPM truth once **elapsed
    time** reaches ~1.3 s (~10-11 onsets); shorter windows read 65-90 BPM
    because the follower does a fast catch-up burst right after it recenters, and
    its raw canonical beat plateaus for a stretch (so span in beats is an
    unreliable gate -- elapsed time is the real one).
    """

    min_onsets: int = 6
    min_canonical_span_beats: float = 0.5
    min_elapsed_seconds: float = 1.3
    max_elapsed_seconds: float = 3.0
    min_tempo_bpm: float = 20.0
    max_tempo_bpm: float = 400.0
    _onsets: list[tuple[float, float, float | None]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.min_onsets < 2:
            raise ValueError("min_onsets must be at least 2 to fit a pace")
        if self.min_canonical_span_beats <= 0:
            raise ValueError("min_canonical_span_beats must be positive")
        if self.min_elapsed_seconds <= 0:
            raise ValueError("min_elapsed_seconds must be positive")
        if self.max_elapsed_seconds < self.min_elapsed_seconds:
            raise ValueError("max_elapsed_seconds must be >= min_elapsed_seconds")
        if not 0 < self.min_tempo_bpm < self.max_tempo_bpm:
            raise ValueError("tempo bounds must be positive and ordered")

    def observe(
        self,
        *,
        perf_time: float,
        canonical_beat: float,
        reference_beat: float | None,
    ) -> None:
        """Record one confident piano-matched onset (strictly advancing in time)."""

        if self._onsets and perf_time <= self._onsets[-1][0]:
            # Chord spread / duplicate timestamps carry no new pace information.
            return
        self._onsets.append((perf_time, canonical_beat, reference_beat))

    @property
    def onset_count(self) -> int:
        return len(self._onsets)

    @property
    def latest_canonical_beat(self) -> float | None:
        return self._onsets[-1][1] if self._onsets else None

    def _ready(self, *, force: bool) -> bool:
        if len(self._onsets) < 2:
            return False
        elapsed = self._onsets[-1][0] - self._onsets[0][0]
        span = self._onsets[-1][1] - self._onsets[0][1]
        if force:
            return elapsed > 0 and span > 0
        return (
            len(self._onsets) >= self.min_onsets
            and span >= self.min_canonical_span_beats
            and elapsed >= self.min_elapsed_seconds
        )

    def elapsed_since_first(self, now: float) -> float | None:
        if not self._onsets:
            return None
        return now - self._onsets[0][0]

    def should_force(self, now: float) -> bool:
        """True once the acquisition window has run long enough to settle for less.

        A pianist who keeps playing crosses the span threshold well before this;
        the timeout only rescues a very sparse or hesitant entrance so the
        orchestra is never stranded holding forever.
        """

        elapsed = self.elapsed_since_first(now)
        return elapsed is not None and elapsed >= self.max_elapsed_seconds and len(self._onsets) >= 2

    def should_abandon(self, now: float, ceiling_seconds: float) -> bool:
        """True once waiting for a usable pace has stopped being defensible.

        ``should_force`` only relaxes the readiness *gates*; the fit it then
        produces can still be rejected outright by the slope/tempo bounds, and
        that rejection is indistinguishable from "not ready yet". Without this
        the caller waits forever: a real cue-in produced 33 onsets over 12.6 s
        whose forced fit was 13.8 BPM -- below ``min_tempo_bpm`` -- so the
        orchestra stayed frozen for the whole take. A pace that cannot be fit
        means the follower is not tracking, which is a reason to recover, not a
        reason to keep waiting.
        """

        elapsed = self.elapsed_since_first(now)
        return elapsed is not None and elapsed >= ceiling_seconds

    def estimate(self, *, force: bool = False) -> EntryPaceEstimate | None:
        """Fit pace/phase from the onsets, or ``None`` if not yet trustworthy."""

        if not self._ready(force=force):
            return None
        canonical_fit = _theil_sen_fit([(t, c) for t, c, _ in self._onsets])
        if canonical_fit is None:
            return None
        canonical_slope, canonical_intercept = canonical_fit
        if canonical_slope <= 0:
            return None
        tempo_bpm = canonical_slope * 60.0
        if not self.min_tempo_bpm <= tempo_bpm <= self.max_tempo_bpm:
            return None
        reference_slope: float | None = None
        reference_intercept: float | None = None
        reference_points = [(t, r) for t, _, r in self._onsets if r is not None]
        if len(reference_points) == len(self._onsets):
            reference_fit = _theil_sen_fit(reference_points)
            if reference_fit is not None and reference_fit[0] > 0:
                reference_slope, reference_intercept = reference_fit
        return EntryPaceEstimate(
            canonical_beats_per_second=canonical_slope,
            canonical_intercept_beats=canonical_intercept,
            reference_beats_per_second=reference_slope,
            reference_intercept_beats=reference_intercept,
            onset_count=len(self._onsets),
            canonical_span_beats=self._onsets[-1][1] - self._onsets[0][1],
            elapsed_seconds=self._onsets[-1][0] - self._onsets[0][0],
        )
