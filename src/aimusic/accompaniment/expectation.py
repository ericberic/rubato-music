"""What the score expects the performer to play, per cell.

This is the primitive that lets the follower tell **intentional silence** apart
from **missing evidence** (Decision 0015). Both used to look like
``support == 0``, and they demand opposite responses: do nothing at all, versus
record a pass.

Expectation is a property of the music. It is derived from the score's own solo
part and does not depend on how much has been rehearsed, so it never changes as
takes accumulate.

Coordinates: everything here is **canonical score beats**. The bundle's
``solo_events`` are already canonical, which is deliberate -- the reference MIDI
lives at a different PPQ (240) than the canonical timeline (960), and mixing the
two silently produces plausible-looking nonsense rather than an error. See
``tests/accompaniment/test_expectation.py``, which pins known bars for exactly
that reason.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from enum import StrEnum

from aimusic.accompaniment.score_bundle import ScoreBundle
from aimusic.accompaniment.section_policy import AccompanimentMode

# A cell is half a beat, matching the Interpretation profile grid so readiness
# and expectation can be compared cell-for-cell without resampling.
DEFAULT_CELL_BEATS = 0.5

# At or above this many onsets in a cell, localization has enough to work with.
# Below it (but non-zero, or sustaining) the follower must not demand frequent
# corrections -- a held chord is the performer playing, not the performer absent.
ACTIVE_ONSET_THRESHOLD = 1

# A region at or below this solo-onset density is the orchestra's to lead: there
# is not enough of a solo line here to follow, even if a note or two occurs.
#
# Measured on Movement II, the separation is not marginal:
#     opening introduction   0.00 onsets/beat
#     m.22 interlude         0.50
#     m.52 continuation      0.29
#     solo passages          4.22 - 4.32
# An eight-fold gap, so this threshold does not need to be finely tuned. It is
# the quantitative form of Decision 0012's "piano tacet/sparse -> orchestra-led":
# m.22 has two solo notes in the bar and is an interlude, not a solo passage.
LEAD_MAX_SOLO_ONSETS_PER_BEAT = 1.0


class CellRole(StrEnum):
    """What the score asks of the performer in a cell."""

    TACET = "TACET"
    """The solo part is silent here. Nothing is expected; silence is correct."""

    SPARSE = "SPARSE"
    """The solo part sounds but gives few onsets -- weak localization signal."""

    ACTIVE = "ACTIVE"
    """The solo part has onsets here; corrections should be arriving."""


@dataclass(frozen=True)
class ExpectationCell:
    """One cell of the score's demand on the performer."""

    start_beat: float
    end_beat: float
    expected_onsets: int
    sounding: bool
    """True when a solo note is sustaining across this cell, onset or not."""
    role: CellRole
    authority: AccompanimentMode
    """Coarse authority from the section map: who owns timing here."""

    def contains(self, beat: float) -> bool:
        return self.start_beat <= beat < self.end_beat


def missing_ratio(expected_onsets: int, observed_onsets: int) -> float:
    """How much of the expected evidence failed to arrive, in [0, 1].

    **Zero when nothing was expected.** This is the whole mechanism that makes
    TACET a satisfied state rather than an absence: a cell the score leaves silent
    can never accrue a deficit, so confidence never decays there and no amount of
    recording can "improve" it.

    Guarding on ``expected_onsets == 0`` is load-bearing, not defensive: the
    obvious-looking ``1 - observed / max(expected, 1)`` returns 1.0 for the
    nothing-expected-nothing-observed case, i.e. reports a total miss for a
    passage that was performed exactly as written.
    """

    if expected_onsets <= 0:
        return 0.0
    return max(0.0, 1.0 - observed_onsets / expected_onsets)


class ExpectationModel:
    """Per-cell expectation over a movement, in canonical score beats."""

    def __init__(self, cells: tuple[ExpectationCell, ...]) -> None:
        if not cells:
            raise ValueError("expectation model requires at least one cell")
        self._cells = cells
        self._starts = [cell.start_beat for cell in cells]

    @property
    def cells(self) -> tuple[ExpectationCell, ...]:
        return self._cells

    @classmethod
    def from_bundle(
        cls,
        bundle: ScoreBundle,
        *,
        cell_beats: float = DEFAULT_CELL_BEATS,
    ) -> "ExpectationModel":
        """Derive expectation from a runtime bundle's canonical solo events."""

        if cell_beats <= 0:
            raise ValueError("cell_beats must be positive")

        solo = bundle.solo_events
        if not solo:
            raise ValueError("bundle has no solo events to derive expectation from")

        section_map = bundle.section_map
        end_beat = max(event.beat + event.duration_beats for event in bundle.events)
        cell_count = max(1, int(-(-end_beat // cell_beats)))  # ceil

        onsets = sorted(event.beat for event in solo)
        # Sustains are half-open [beat, beat + duration): a note that merely
        # touches a cell boundary is not sounding in the next cell.
        spans = sorted(
            (event.beat, event.beat + event.duration_beats)
            for event in solo
            if event.duration_beats > 0
        )
        span_starts = [start for start, _ in spans]

        cells: list[ExpectationCell] = []
        for index in range(cell_count):
            start = index * cell_beats
            end = start + cell_beats
            expected = bisect_right(onsets, end - 1e-9) - bisect_right(onsets, start - 1e-9)

            # Any span beginning at or before this cell may still be sounding in
            # it; check those rather than scanning every note.
            limit = bisect_right(span_starts, start + 1e-9)
            sounding = any(
                span_start < end - 1e-9 and span_end > start + 1e-9
                for span_start, span_end in spans[:limit]
            )

            if expected <= 0 and not sounding:
                role = CellRole.TACET
            elif expected >= ACTIVE_ONSET_THRESHOLD:
                role = CellRole.ACTIVE
            else:
                role = CellRole.SPARSE

            try:
                authority = section_map.mode_at(start)
            except ValueError:
                # Past the declared sections (trailing partial cell): whoever led
                # last keeps leading. Never invent FOLLOW authority off the end.
                authority = cells[-1].authority if cells else AccompanimentMode.LEAD
            if authority not in {AccompanimentMode.LEAD, AccompanimentMode.FOLLOW}:
                # HOLD/STOP are transport instructions, not authority statements;
                # for expectation purposes they behave as orchestra-led.
                authority = AccompanimentMode.LEAD

            cells.append(
                ExpectationCell(
                    start_beat=start,
                    end_beat=end,
                    expected_onsets=expected,
                    sounding=sounding,
                    role=role,
                    authority=authority,
                )
            )

        return cls(tuple(cells))

    def cell_at(self, beat: float) -> ExpectationCell:
        index = bisect_right(self._starts, beat) - 1
        if index < 0:
            index = 0
        if index >= len(self._cells):
            index = len(self._cells) - 1
        return self._cells[index]

    def role_at(self, beat: float) -> CellRole:
        return self.cell_at(beat).role

    def authority_at(self, beat: float) -> AccompanimentMode:
        return self.cell_at(beat).authority

    def expected_onsets_in(self, start_beat: float, end_beat: float) -> int:
        return sum(
            cell.expected_onsets
            for cell in self._cells
            if cell.start_beat < end_beat and cell.end_beat > start_beat
        )

    def is_tacet_through(self, start_beat: float, end_beat: float) -> bool:
        """True when the solo part is silent across the whole span."""

        touched = [
            cell
            for cell in self._cells
            if cell.start_beat < end_beat and cell.end_beat > start_beat
        ]
        return bool(touched) and all(cell.role is CellRole.TACET for cell in touched)

    def solo_onsets_per_beat(self, start_beat: float, end_beat: float) -> float:
        """Solo onset density across a span, in onsets per canonical beat."""

        span = end_beat - start_beat
        if span <= 0:
            return 0.0
        return self.expected_onsets_in(start_beat, end_beat) / span

    def is_orchestra_led_region(self, start_beat: float, end_beat: float) -> bool:
        """True when there is too little solo line here to follow.

        Distinct from `is_tacet_through`: an orchestral interlude may contain a
        couple of solo notes without becoming a passage the accompanist can track.
        This is the test the follower needs before handing authority back, since
        demanding literal silence would leave it following two notes a bar.
        """

        return (
            self.solo_onsets_per_beat(start_beat, end_beat) <= LEAD_MAX_SOLO_ONSETS_PER_BEAT
        )

    def first_solo_beat(self) -> float:
        """Canonical beat of the movement's first expected solo onset."""

        for cell in self._cells:
            if cell.expected_onsets > 0:
                return cell.start_beat
        raise ValueError("no solo onsets in expectation model")
