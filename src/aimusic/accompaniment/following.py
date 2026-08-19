"""Score-follower interfaces and deterministic test followers."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from aimusic.accompaniment.score_bundle import ScoreEvent


@dataclass(frozen=True)
class PerformedNote:
    """A note observation from live, replayed, or synthetic MIDI input."""

    perf_time: float
    pitch: int
    velocity: int
    score_beat: float | None = None
    event_id: str | None = None


@dataclass(frozen=True)
class FollowerUpdate:
    """Causal score-position estimate emitted by a score follower."""

    perf_time: float
    score_beat: float
    confidence: float
    raw_state: dict[str, Any] = field(default_factory=dict)
    # Position on the shared reference-performance MIDI timeline.  Movement II
    # uses this to align the split solo and orchestra tracks directly; the
    # canonical score beat remains the measure/beat and cursor coordinate.
    reference_beat: float | None = None


class ScoreFollower(Protocol):
    """Runtime seam for OracleFollower, Matchmaker, ACCompanion, or future trackers."""

    def observe(self, note: PerformedNote) -> FollowerUpdate | None:
        """Consume one performed note and optionally emit a score-position update."""


@runtime_checkable
class EntryPositionableScoreFollower(Protocol):
    """Follower that can begin a local search at a moving cue-in position."""

    def reposition_for_entry(
        self,
        *,
        score_beat: float,
        reference_beat: float | None,
    ) -> None:
        """Center the next note match at one explicit score/reference seam."""


class RelockingFollower:
    """Watchdog that re-localizes a stalled inner follower.

    A note-by-note tracker that mis-locks early -- e.g. an opening it never
    locks -- keeps reporting a position that drifts steadily behind and cannot
    climb back, so it stays tens of beats behind for the rest of the take even
    though the later material is perfectly trackable. This wrapper watches the
    reported position: when it advances less than ``min_advance_beats`` over a
    ``window_seconds`` span *despite* at least ``min_updates`` note updates in
    that span, it calls ``relocalize`` (a global PTHMM re-search) and, after a
    ``cooldown_seconds`` guard, watches again.

    The ``min_updates`` gate is what makes it safe: rests, holds, and
    ritardandos produce few updates, so they never trip it; only sustained
    playing that fails to make score progress does. Offline this recovered a
    stuck take (~70 -> ~4 beats of error) with zero re-locks on a take that was
    already tracking cleanly. The wrapper is coordinate-agnostic -- it watches
    whatever ``score_beat`` the inner follower reports -- so it sits *outside*
    the canonical mapping and re-searches the raw tracker underneath it.
    """

    def __init__(
        self,
        inner: ScoreFollower,
        *,
        relocalize: Callable[[], None],
        window_seconds: float = 8.0,
        min_updates: int = 25,
        min_advance_beats: float = 4.0,
        cooldown_seconds: float = 6.0,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if min_updates < 2:
            raise ValueError("min_updates must be at least 2")
        self._inner = inner
        self._relocalize = relocalize
        self._window = window_seconds
        self._min_updates = min_updates
        self._min_advance = min_advance_beats
        self._cooldown = cooldown_seconds
        self._recent: deque[tuple[float, float]] = deque()
        self._last_relocalize_at = float("-inf")
        self.relocalizations = 0

    def observe(self, note: PerformedNote) -> FollowerUpdate | None:
        update = self._inner.observe(note)
        if update is not None:
            self._note_progress(update)
        return update

    def poll_update(self) -> FollowerUpdate | None:
        poll = getattr(self._inner, "poll_update", None)
        return None if poll is None else poll()

    def _note_progress(self, update: FollowerUpdate) -> None:
        self._recent.append((update.perf_time, update.score_beat))
        cutoff = update.perf_time - self._window
        while self._recent and self._recent[0][0] < cutoff:
            self._recent.popleft()
        if (
            len(self._recent) >= self._min_updates
            and update.perf_time - self._last_relocalize_at > self._cooldown
        ):
            beats = [beat for _perf, beat in self._recent]
            if max(beats) - min(beats) < self._min_advance:
                self._relocalize()
                self._last_relocalize_at = update.perf_time
                self.relocalizations += 1
                self._recent.clear()

    def reposition_for_entry(self, *, score_beat: float, reference_beat: float | None) -> None:
        reposition = getattr(self._inner, "reposition_for_entry", None)
        if reposition is not None:
            reposition(score_beat=score_beat, reference_beat=reference_beat)
        # A fresh entry supersedes any accumulated stall evidence.
        self._recent.clear()
        self._last_relocalize_at = float("-inf")

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if close is not None:
            close()


class OracleFollower:
    """Follower for simulations where the score beat is known by construction.

    This deliberately does not solve score following. It lets scheduler, tempo,
    section-policy, and rendering tests run before integrating a probabilistic
    tracker.
    """

    def observe(self, note: PerformedNote) -> FollowerUpdate | None:
        if note.score_beat is None:
            return None
        return FollowerUpdate(
            perf_time=note.perf_time,
            score_beat=note.score_beat,
            confidence=1.0,
            raw_state={
                "follower": "oracle",
                "event_id": note.event_id,
                "pitch": note.pitch,
                "velocity": note.velocity,
            },
        )


class ReferencePitchFollower:
    """Deterministic pitch-sequence follower for simulation tests.

    This is intentionally simple: it searches forward through known solo events
    and emits the first matching pitch. It proves that downstream code can run
    when score beats are inferred rather than supplied, but it is not robust
    enough for real performance.
    """

    def __init__(self, solo_events: tuple[ScoreEvent, ...], search_window_events: int = 8) -> None:
        if search_window_events <= 0:
            raise ValueError("search_window_events must be positive")
        self._events = tuple(
            event
            for event in sorted(solo_events, key=lambda item: (item.beat, item.event_id))
            if event.pitch is not None
        )
        self._search_window_events = search_window_events
        self._cursor = 0
        self._matched_count = 0

    def observe(self, note: PerformedNote) -> FollowerUpdate | None:
        if note.score_beat is not None:
            self._advance_to_score_beat(note.score_beat)
            self._matched_count += 1
            return FollowerUpdate(
                perf_time=note.perf_time,
                score_beat=note.score_beat,
                confidence=1.0,
                raw_state={
                    "follower": "reference_pitch",
                    "source": "provided_score_beat",
                    "pitch": note.pitch,
                    "velocity": note.velocity,
                },
            )

        match_index = self._find_next_pitch(note.pitch)
        if match_index is None:
            return None

        event = self._events[match_index]
        self._cursor = match_index + 1
        self._matched_count += 1
        confidence = 0.7 if self._matched_count == 1 else 0.9
        return FollowerUpdate(
            perf_time=note.perf_time,
            score_beat=event.beat,
            confidence=confidence,
            raw_state={
                "follower": "reference_pitch",
                "source": "pitch_match",
                "matched_event_id": event.event_id,
                "pitch": note.pitch,
                "velocity": note.velocity,
            },
        )

    def _find_next_pitch(self, pitch: int) -> int | None:
        end = min(len(self._events), self._cursor + self._search_window_events)
        for index in range(self._cursor, end):
            if self._events[index].pitch == pitch:
                return index
        return None

    def _advance_to_score_beat(self, score_beat: float) -> None:
        while self._cursor < len(self._events) and self._events[self._cursor].beat <= score_beat:
            self._cursor += 1
