"""Conversions between the coordinate systems a score bundle mixes.

Three beat origins meet in this codebase and none of them announce themselves:

- **canonical** -- Rubato's runtime coordinate. Measure 1 beat 1 is beat 0.0,
  and in 4/4 measure N starts at ``4 * (N - 1)``. Everything the follower,
  scheduler, coverage and display map speak is canonical.
- **partitura** -- the note-array coordinate used by partitura, Parangonar and
  Matchmaker. Its origin is the first *full* measure, so a score whose first bar
  is treated as a pickup places measure 1 at a **negative** beat.
- **reference** -- beats in a reference performance's own MIDI timeline, at that
  file's PPQ. Unrelated to either of the above.

Mixing the first two silently produces an offset of exactly one measure, which
looks indistinguishable from a follower that is a bar behind. That mistake was
made twice in one session: a comparison reported a follower "3.91 beats off"
across an entire piece, when the follower was accurate to 0.29 beats and the
score's measure 1 simply started at partitura beat -4.

Convert explicitly at the boundary. Never compare a partitura ``onset_beat`` to
a runtime ``score_beat`` without going through here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BeatOrigin:
    """Offset that maps a partitura beat axis onto the canonical one.

    ``canonical = partitura + offset``. The offset is the negation of the
    partitura beat at which measure 1 begins, so a score whose first measure
    sits at partitura beat -4 yields an offset of +4.
    """

    offset: float

    def to_canonical(self, partitura_beat: float) -> float:
        return partitura_beat + self.offset

    def to_partitura(self, canonical_beat: float) -> float:
        return canonical_beat - self.offset


def beat_origin_from_measure_starts(measure_starts: dict[int, float]) -> BeatOrigin:
    """Derive the offset from a mapping of measure number -> partitura beat.

    Uses the *lowest-numbered* measure rather than the minimum beat, because a
    score with a pickup can place measure 1 below measure 2 while later measures
    are ordered normally.
    """

    if not measure_starts:
        raise ValueError("cannot derive a beat origin without measure starts")
    first = min(measure_starts)
    return BeatOrigin(offset=-float(measure_starts[first]))


def canonical_beat_of_measure(measure: int, beats_per_bar: float = 4.0) -> float:
    """Canonical beat where a measure begins, under Rubato's convention."""

    if measure < 1:
        raise ValueError("measure numbers are 1-based")
    return (measure - 1) * beats_per_bar


def measure_and_beat(
    canonical_beat: float, beats_per_bar: float = 4.0
) -> tuple[int, float]:
    """Canonical beat -> (measure, beat-in-measure), both 1-based.

    This is the coordinate a human uses and the only one that should be stored
    for a human annotation: canonical ticks are derived and change whenever the
    mapping is rebuilt, so an annotation stored as a tick silently relocates.
    """

    if canonical_beat < 0:
        raise ValueError("canonical beats start at 0.0 for measure 1 beat 1")
    measure = int(canonical_beat // beats_per_bar) + 1
    return measure, canonical_beat - (measure - 1) * beats_per_bar + 1.0


def canonical_beat_from_measure_beat(
    measure: int, beat_in_measure: float, beats_per_bar: float = 4.0
) -> float:
    """(measure, beat-in-measure) -> canonical beat. Inverse of measure_and_beat."""

    if beat_in_measure < 1.0:
        raise ValueError("beat-in-measure is 1-based; beat 1 is the downbeat")
    return canonical_beat_of_measure(measure, beats_per_bar) + (beat_in_measure - 1.0)


def canonical_beat_to_tick(canonical_beat: float, canonical_ppq: int = 960) -> int:
    return round(canonical_beat * canonical_ppq)


def tick_to_canonical_beat(tick: int, canonical_ppq: int = 960) -> float:
    return tick / canonical_ppq
