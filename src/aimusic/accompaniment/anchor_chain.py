"""Anchor chains: a fast online tracker for passages the follower cannot serve.

An anchor means one thing -- **"here is where I am"** -- and its action is
derived, not annotated. Correcting position always follows; sounding the
orchestra follows only where the score has notes at that beat. A cadenza anchor
and a downbeat-sync anchor are the same object with different triggers.

Chains exist because note-sequence following fails on free passages. In the
Chopin mvt II cadenza the reference recording realises the music at 0.32
pitch-sequence similarity to the notation, so there is nothing stable to match
note-by-note. Live, the follower froze at one beat for 16.4 seconds while the
performer played twenty gestures past it, and the orchestral interlude that
should have followed never fired.

**All heavy lifting is offline.** Triggers are derived from recorded
demonstrations (`build_chroma_anchors`), and what ships to the runtime is a
handful of floats per anchor. Online cost per note is a 12-element dot product
against at most three candidates -- less than the follower already does.

Three signals cover three different failure modes, and no one of them suffices:

    chroma        right harmony, wrong notes or order  (~70% alone, tempo-invariant)
    order         only anchors k..k+2 are live         (kills adjacent confusion)
    elapsed time  k+1 is due one running period after k (survives tempo change)

Measured on real demonstrations: chroma alone identified 96% of beats in-domain
but only 7/10 when the same passage was played 40% slower, with two of the
three misses being adjacent beats -- which ordering removes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum

CHROMA_BINS = 12

# How much the elapsed-time prior may shade a match. Small on purpose: it
# breaks ties between harmonically similar beats without being able to reject
# one the harmony clearly identifies.
TIMING_WEIGHT = 0.25

# The observation window, as a fraction of the running beat period. Long enough
# to hold one broken chord, short enough that the previous one has aged out.
WINDOW_PERIODS = 0.8

# Minimum spacing between fires, as a fraction of the period. Stops a chain
# firing several times inside one gesture as its window fills.
REFRACTORY_PERIODS = 0.55


# ------------------------------------------------------------------ triggers


class Trigger:
    """What the performer plays at an anchor.

    Subclasses answer one question: given the notes observed since the last
    anchor fired, how well does this look like *my* anchor? Scores are
    comparable across trigger kinds so a chain may mix them.
    """

    def score(self, window: "NoteWindow") -> float:
        raise NotImplementedError


@dataclass(frozen=True)
class PitchTrigger(Trigger):
    """A single pitch. Brittle -- a fluffed note loses it entirely.

    Retained because it is exactly right where a pitch is unique and reliably
    played, such as a marked bass note in a metrical passage.
    """

    pitch: int

    def score(self, window: "NoteWindow") -> float:
        return 1.0 if self.pitch in window.pitches else 0.0


@dataclass(frozen=True)
class SimultaneityTrigger(Trigger):
    """Several pitches struck together. Used for a decisive landing.

    The mvt II cadenza ends on G#3 + F#6 -- 34 semitones apart, occurring twice
    in a 520-second take where either pitch alone occurs eight times.
    """

    pitches: tuple[int, ...]
    window_seconds: float = 0.15

    def score(self, window: "NoteWindow") -> float:
        times = [t for t, p in window.notes if p in self.pitches]
        found = {p for _t, p in window.notes if p in self.pitches}
        if found != set(self.pitches) or not times:
            return 0.0
        return 1.0 if (max(times) - min(times)) <= self.window_seconds else 0.0


@dataclass(frozen=True)
class ChromaTrigger(Trigger):
    """A normalized 12-bin pitch-class profile.

    Order-, octave- and ornament-invariant: a broken chord played bottom-up,
    top-down or with a note missing yields nearly the same vector. That is what
    note-sequence matching lacks, and why it survives a performer and a
    reference realising the same cadenza differently.

    Weak alone in harmonically self-similar writing -- a run of diminished
    sevenths all look alike -- so it is scored alongside order and timing.
    """

    profile: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.profile) != CHROMA_BINS:
            raise ValueError(f"a chroma profile needs {CHROMA_BINS} bins")

    def score(self, window: "NoteWindow") -> float:
        return _cosine(window.chroma(), self.profile)


def _cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def normalize_chroma(counts: list[float]) -> tuple[float, ...]:
    norm = math.sqrt(sum(c * c for c in counts))
    return tuple(c / norm for c in counts) if norm else tuple(counts)


# -------------------------------------------------------------------- window


@dataclass
class NoteWindow:
    """Notes observed since the last anchor fired."""

    notes: list[tuple[float, int]] = field(default_factory=list)

    def add(self, when: float, pitch: int, keep_seconds: float | None = None) -> None:
        self.notes.append((when, pitch))
        if keep_seconds is not None:
            cutoff = when - keep_seconds
            self.notes = [(t, p) for t, p in self.notes if t >= cutoff]

    def clear(self) -> None:
        self.notes.clear()

    @property
    def pitches(self) -> set[int]:
        return {p for _t, p in self.notes}

    def chroma(self) -> tuple[float, ...]:
        counts = [0.0] * CHROMA_BINS
        for _t, pitch in self.notes:
            counts[pitch % CHROMA_BINS] += 1.0
        return normalize_chroma(counts)


# --------------------------------------------------------------------- chain


@dataclass(frozen=True)
class ChainAnchor:
    """One "here is where I am" marker."""

    canonical_beat: float
    trigger: Trigger
    label: str = ""


class ChainState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"


@dataclass(frozen=True)
class AnchorChain:
    """A run of anchors covering a passage, with a declared exit.

    ``arm_within_beats`` is the *only* place a follower position is consulted.
    Arming early -- before the passage that breaks the follower -- is what
    avoids the deadlock in position-gated anchors, where a frozen position is
    far from every future anchor so nothing can ever fire.
    """

    chain_id: str
    anchors: tuple[ChainAnchor, ...]
    hand_back_beat: float
    exit_trigger: Trigger | None = None
    arm_within_beats: float = 2.0
    lookahead: int = 2
    min_margin: float = 0.0
    # A window holding one note of a seven-note broken chord already scores
    # ~0.38 against that chord's template. Without a floor the chain fires on
    # the first note of every gesture, races through its anchors and measures
    # a period of milliseconds. Wait until enough of the harmony has arrived.
    min_score: float = 0.7
    seed_period_seconds: float = 1.0
    give_up_after_periods: float = 2.5

    def __post_init__(self) -> None:
        if not self.anchors:
            raise ValueError("a chain needs at least one anchor")
        beats = [a.canonical_beat for a in self.anchors]
        if beats != sorted(beats):
            raise ValueError("chain anchors must be ordered by canonical beat")

    @property
    def start_beat(self) -> float:
        return self.anchors[0].canonical_beat

    @property
    def end_beat(self) -> float:
        return self.anchors[-1].canonical_beat


@dataclass(frozen=True)
class ChainFire:
    """A position assertion produced by the chain."""

    canonical_beat: float
    anchor_index: int
    kind: str  # "anchor" | "exit" | "gave_up"
    score: float = 0.0
    margin: float = 0.0
    period_seconds: float = 0.0
    label: str = ""


class AnchorChainTracker:
    """Online tracker. One dot product per candidate per note.

    Never consults a follower position once running: the chain *is* the
    position authority for its passage. That is deliberate -- an anchor exists
    to assert position, so validating it against the estimate it overrides
    inverts the authority and deadlocks when that estimate is stuck.
    """

    def __init__(self, chain: AnchorChain) -> None:
        self.chain = chain
        self.state = ChainState.IDLE
        self._next = 0
        self._window = NoteWindow()
        self._last_fire_at: float | None = None
        self._period = chain.seed_period_seconds

    @property
    def next_index(self) -> int:
        return self._next

    @property
    def period_seconds(self) -> float:
        return self._period

    def maybe_arm(self, follower_beat: float | None) -> bool:
        """Arm when the follower gets close. The only use of position."""

        if self.state is not ChainState.IDLE or follower_beat is None:
            return False
        if abs(self.chain.start_beat - follower_beat) <= self.chain.arm_within_beats:
            self.state = ChainState.RUNNING
            return True
        return False

    def observe(self, pitch: int, now: float) -> ChainFire | None:
        if self.state is not ChainState.RUNNING:
            return None
        # A sliding window, not one cleared on each fire: a fire mid-gesture
        # would otherwise leave that gesture's remaining notes in the next
        # window and poison the following match. Old notes age out instead.
        self._window.add(now, pitch, keep_seconds=self._period * WINDOW_PERIODS)

        if self.chain.exit_trigger is not None and self.chain.exit_trigger.score(self._window) > 0:
            self.state = ChainState.DONE
            return ChainFire(
                canonical_beat=self.chain.hand_back_beat,
                anchor_index=len(self.chain.anchors),
                kind="exit",
                score=1.0,
                period_seconds=self._period,
            )

        if self._last_fire_at is not None:
            overdue = (now - self._last_fire_at) / max(self._period, 1e-6)
            if overdue > self.chain.give_up_after_periods:
                self.state = ChainState.DONE
                return ChainFire(
                    canonical_beat=self.chain.anchors[max(0, self._next - 1)].canonical_beat,
                    anchor_index=max(0, self._next - 1),
                    kind="gave_up",
                    period_seconds=self._period,
                )

        candidates = range(self._next, min(self._next + 1 + self.chain.lookahead,
                                           len(self.chain.anchors)))
        scored = sorted(
            ((self._score(index, now), index) for index in candidates), reverse=True
        )
        if not scored:
            return None
        best, index = scored[0]
        margin = best - (scored[1][0] if len(scored) > 1 else 0.0)
        if best < self.chain.min_score or margin < self.chain.min_margin:
            return None
        # Refractory: never fire twice inside one gesture.
        if (
            self._last_fire_at is not None
            and (now - self._last_fire_at) < self._period * REFRACTORY_PERIODS
        ):
            return None

        anchor = self.chain.anchors[index]
        if self._last_fire_at is not None and index > 0:
            beats = anchor.canonical_beat - self.chain.anchors[self._next - 1].canonical_beat
            if beats > 0:
                measured = (now - self._last_fire_at) / beats
                # Track the performer's pace without letting one gesture swing it.
                self._period = 0.5 * self._period + 0.5 * measured
        self._last_fire_at = now
        self._next = index + 1
        if self._next >= len(self.chain.anchors) and self.chain.exit_trigger is None:
            self.state = ChainState.DONE
        return ChainFire(
            canonical_beat=anchor.canonical_beat,
            anchor_index=index,
            kind="anchor",
            score=best,
            margin=margin,
            period_seconds=self._period,
            label=anchor.label,
        )

    def _score(self, index: int, now: float) -> float:
        base = self.chain.anchors[index].trigger.score(self._window)
        if base <= 0.0:
            return 0.0
        # Timing shades the choice between candidates; it must not veto a clear
        # match. A full multiplier let a perfect chroma score (1.0) fall to 0.73
        # on an unremarkable timing prior and sit below the firing threshold, so
        # the chain stalled on material it had recognised.
        return base * (1.0 - TIMING_WEIGHT + TIMING_WEIGHT * self._timing_prior(index, now))

    def _timing_prior(self, index: int, now: float) -> float:
        """Favour the anchor that is due now, at the performer's running pace.

        Adaptive rather than fixed: a passage rehearsed at 1.0 s/beat was played
        40% slower in performance, so a constant expectation is wrong by
        construction. Never zero -- timing shades the decision, chroma and order
        make it.
        """

        if self._last_fire_at is None:
            return 1.0
        previous = self.chain.anchors[self._next - 1].canonical_beat if self._next else None
        if previous is None:
            return 1.0
        expected_beats = self.chain.anchors[index].canonical_beat - previous
        if expected_beats <= 0:
            return 1.0
        expected = expected_beats * self._period
        actual = now - self._last_fire_at
        ratio = actual / expected if expected > 0 else 1.0
        return 1.0 / (1.0 + abs(math.log(max(ratio, 1e-3))))


# ------------------------------------------------------------------- offline


def build_chroma_anchors(
    demonstrations: list[list[tuple[float, int]]],
    *,
    start_beat: float,
    beats: int,
    beat_length: float = 1.0,
) -> tuple[ChainAnchor, ...]:
    """Derive per-beat chroma triggers from recorded demonstrations.

    Each demonstration is [(beat_position, pitch)] for one pass of the passage,
    normalized so the passage starts at 0. Averaging across passes is what
    separates an invariant from an ornament -- a single pass cannot, because a
    pitch that looks decisive once may occur four times.
    """

    if not demonstrations:
        raise ValueError("need at least one demonstration")
    anchors: list[ChainAnchor] = []
    for beat in range(beats):
        totals = [0.0] * CHROMA_BINS
        for demo in demonstrations:
            counts = [0.0] * CHROMA_BINS
            for position, pitch in demo:
                if beat * beat_length <= position < (beat + 1) * beat_length:
                    counts[pitch % CHROMA_BINS] += 1.0
            unit = normalize_chroma(counts)
            for i, value in enumerate(unit):
                totals[i] += value
        anchors.append(
            ChainAnchor(
                canonical_beat=start_beat + beat * beat_length,
                trigger=ChromaTrigger(normalize_chroma(totals)),
                label=f"beat {beat}",
            )
        )
    return tuple(anchors)
