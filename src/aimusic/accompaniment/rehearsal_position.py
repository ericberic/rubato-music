"""Performer-facing score positions on the canonical Movement 2 beat grid.

The offline fusion artifact maps expressive Oguri reference ticks to canonical
score ticks.  This module performs that one projection at runtime and retains
machine review state/confidence separately from coordinate ownership.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from statistics import median
from typing import Iterable

from aimusic.accompaniment.bundle_v2 import (
    BundleLoaderV2,
    CanonicalTimeline,
    MappingReviewState,
)
from aimusic.accompaniment.offline_alignment import extract_note_events
from aimusic.accompaniment.score_fusion import (
    apply_human_corrections,
    load_human_corrections,
    load_performance_beat_map,
)
from aimusic.core import paths

MAPPING_ID = "oguri-to-joseffy-checked-anchors-v6"
SOURCE_TICKS_PER_SECOND = 1920.0  # Oguri fixed 120 BPM projected at 960 PPQ.
# Checked next-bar boundaries used for pickup resolution without changing the
# projection's extrapolation beyond its reviewed anchor range.
MOVEMENT_2_MEASURE_END_SOURCE_TICKS = {53: 130_285 * 4}


@dataclass(frozen=True)
class ProjectionAnchor:
    source_tick: int
    display_measure: int
    confidence: float


# Sparse musical landmarks explicitly substantiated by rehearsal feedback.
# They constrain offline symbolic sequence alignment; they are not a dense
# substitute for it. Source ticks are native Oguri ticks multiplied by four,
# exactly matching AlignedResultV2's compatibility ticks.
MOVEMENT_2_VERIFIED_DOWNBEATS = (
    # Native ticks 0..2016 contain setup and silence. The first sounding
    # orchestral event is Violin I E4 at tick 2017, corresponding to the
    # engraved E-natural downbeat in printed m.1. Treating tick 0 as that
    # downbeat made the score cursor traverse m.1 before any sound existed.
    ProjectionAnchor(source_tick=2_017 * 4, display_measure=1, confidence=0.90),
    # The first piano B in measure 12 is retained separately as a beat-four
    # landmark. The first full LH/RH attack is the verified m.13 downbeat.
    ProjectionAnchor(source_tick=35_374 * 4, display_measure=13, confidence=0.82),
    # The F-sharp attack was verified against audible review and notation.
    ProjectionAnchor(source_tick=37_485 * 4, display_measure=14, confidence=0.84),
    # Rehearsal feedback identified the large rubato inside m.17 and the
    # following reset at m.18, making both boundaries high-value constraints.
    ProjectionAnchor(source_tick=42_703 * 4, display_measure=17, confidence=0.82),
    ProjectionAnchor(source_tick=46_242 * 4, display_measure=18, confidence=0.82),
    # The take-ending chord and subsequent piano resumption were identified by
    # the performer as the downbeats of m.22 and m.23.
    ProjectionAnchor(source_tick=55_772 * 4, display_measure=22, confidence=0.86),
    ProjectionAnchor(source_tick=58_155 * 4, display_measure=23, confidence=0.80),
    # The phrase-ending bass onset is the verified m.52 downbeat. The rolled
    # treble attack follows shortly and remains inside beat one.
    ProjectionAnchor(source_tick=123_906 * 4, display_measure=52, confidence=0.88),
    # Eric's full-performance review established that the four reactive chords
    # in m.93 resolve through the following downbeat. The orchestra's low
    # D-flat/C-sharp attack at native source tick 223_861 is that m.94 downbeat;
    # the solo-derived path otherwise places the boundary about an eighth late.
    ProjectionAnchor(source_tick=223_861 * 4, display_measure=94, confidence=0.90),
)

# Score-authored simultaneity cues confirmed in rehearsal. Unlike editable
# anchors added from the Data UI, these are part of the Movement-II performance
# contract and must survive a fresh local profile or missing anchors.json.
MOVEMENT_2_REACTIVE_ANCHOR_TICKS = (
    # Four bass-octave/chord attacks in m.44 plus the resolving m.45 attack.
    # These are canonical score beats, not nearby source-performance attacks.
    # Using the latter armed treble pitch 71 for beat one and placed the fifth
    # cue after the m.45 downbeat, so the reactive contract was incomplete.
    165_120,
    166_080,
    167_040,
    168_000,
    168_960,
    # Four attacks in m.93 plus the resolving m.94 downbeat.
    353_280,
    354_240,
    355_200,
    356_160,
    357_120,
)


@dataclass(frozen=True)
class ProjectedPosition:
    score_tick: int
    score_beat: float
    measure_index: int
    measure_label: str
    beat_in_measure: float
    source_seconds: float
    confidence: float


@dataclass(frozen=True)
class RehearsalEntry:
    """The first piano onset inside a performer-selected printed measure."""

    selected_measure: int
    selected_score_tick: int
    entry_source_seconds: float
    entry_position: ProjectedPosition
    entry_pitch: int | None


@dataclass(frozen=True)
class RehearsalCueSpan:
    """Canonical entry and two-measure orchestral preparation span."""

    entry: RehearsalEntry
    cue_start_measure_index: int
    cue_start_score_tick: int
    cue_start_source_seconds: float

    @property
    def lead_in_seconds(self) -> float:
        return max(0.0, self.entry.entry_source_seconds - self.cue_start_source_seconds)


@dataclass(frozen=True)
class ScoreProjection:
    timeline: CanonicalTimeline
    mapping_id: str
    mapping_review_state: MappingReviewState
    canonical_position: bool
    anchors: tuple[tuple[int, int, float], ...]

    def position_at_source_tick(self, source_tick: int) -> ProjectedPosition:
        projected_tick, confidence = self._project_tick(max(0, source_tick))
        projected_tick = max(0, min(projected_tick, self.timeline.end_tick - 1))
        position = self.timeline.position_at(projected_tick)
        return ProjectedPosition(
            score_tick=position.score_tick,
            score_beat=position.score_tick / self.timeline.document.canonical_ppq,
            measure_index=position.measure_index,
            measure_label=position.measure_label,
            beat_in_measure=position.beat_in_measure,
            source_seconds=max(0, source_tick) / SOURCE_TICKS_PER_SECOND,
            confidence=confidence,
        )

    def position_at_source_seconds(self, source_seconds: float) -> ProjectedPosition:
        source_tick = round(max(0.0, source_seconds) * SOURCE_TICKS_PER_SECOND)
        return self.position_at_source_tick(source_tick)

    def source_tick_at_score_tick(self, score_tick: int) -> int:
        """Invert the piecewise display projection for position-targeted cues.

        The mapping is provisional, but a cue selected on the displayed score
        must use the exact same mapping as the cursor and take overlay.  Keeping
        the inverse here prevents a second, subtly different measure-to-Oguri
        conversion from appearing in the recording route.
        """

        target_tick = max(0, min(score_tick, self.timeline.end_tick - 1))
        if not self.anchors:
            return target_tick
        if len(self.anchors) == 1:
            source, target, _confidence = self.anchors[0]
            return max(0, source + (target_tick - target))

        left, right = self.anchors[0], self.anchors[1]
        for candidate_left, candidate_right in zip(self.anchors, self.anchors[1:]):
            left, right = candidate_left, candidate_right
            if target_tick <= candidate_right[1]:
                break
        target_span = right[1] - left[1]
        if target_span == 0:
            return max(0, left[0])
        ratio = (target_tick - left[1]) / target_span
        return max(0, round(left[0] + ratio * (right[0] - left[0])))

    def source_seconds_at_score_tick(self, score_tick: int) -> float:
        return self.source_tick_at_score_tick(score_tick) / SOURCE_TICKS_PER_SECOND

    def inferred_reference_quarter_bpm(self) -> float:
        """Infer the source performance's nominal notated-quarter tempo.

        A Standard MIDI File's declared tempo only turns ticks into seconds; it
        does not prove which source ticks correspond to a notated quarter note.
        The dense symbolic score projection supplies that missing
        correspondence. A robust median retains local rubato while rejecting
        fermatas and isolated mapping outliers.
        """

        ppq = self.timeline.document.canonical_ppq
        seconds_per_quarter: list[float] = []
        for left, right in zip(self.anchors, self.anchors[1:]):
            source_seconds = (right[0] - left[0]) / SOURCE_TICKS_PER_SECOND
            score_quarters = (right[1] - left[1]) / ppq
            if source_seconds <= 0.0 or not 0.25 <= score_quarters <= 8.0:
                continue
            local_seconds_per_quarter = source_seconds / score_quarters
            if 0.2 <= local_seconds_per_quarter <= 4.0:
                seconds_per_quarter.append(local_seconds_per_quarter)
        if not seconds_per_quarter:
            raise ValueError("score projection has no usable tempo correspondence")
        return 60.0 / median(seconds_per_quarter)

    def tempo_scale_for_canonical_bpm(self, tempo_bpm: float) -> float:
        """Scale source seconds so the notated quarter has the requested BPM."""

        if tempo_bpm <= 0:
            raise ValueError("tempo_bpm must be positive")
        return tempo_bpm / self.inferred_reference_quarter_bpm()

    def _project_tick(self, source_tick: int) -> tuple[int, float]:
        if not self.anchors:
            # A projection assembled without anchors is not authoritative, but
            # identity is a safer degraded coordinate than an IndexError.
            return source_tick, 0.0
        if len(self.anchors) == 1:
            source, target, confidence = self.anchors[0]
            return target + (source_tick - source), confidence
        exact = [anchor for anchor in self.anchors if source_tick == anchor[0]]
        if exact:
            return exact[0][1], min(anchor[2] for anchor in exact)

        left, right = self.anchors[0], self.anchors[1]
        for candidate_left, candidate_right in zip(self.anchors, self.anchors[1:]):
            left, right = candidate_left, candidate_right
            if source_tick <= candidate_right[0]:
                break
        source_span = right[0] - left[0]
        if source_span == 0:
            return left[1], min(left[2], right[2])
        ratio = (source_tick - left[0]) / source_span
        target = round(left[1] + ratio * (right[1] - left[1]))
        return target, min(left[2], right[2])


def estimate_performed_end_source_tick(
    *,
    aligned_end_tick: int,
    timing_points: Iterable[tuple[int, float]],
    take_duration_seconds: float,
) -> int:
    """Extend a matched-note span through the take's final performed time.

    Alignment necessarily ends at the last matched onset.  That is correct for
    diagnostics but too short for a performer-facing take wash: a pianist can
    sustain or count through a rest before pressing Stop.  Estimate the small
    trailing extent from recent monotonic timing-map slopes, bounded to eight
    seconds so an accidentally long armed recording cannot paint pages of
    unobserved music.
    """

    ordered = sorted(
        ((int(score_tick), float(take_seconds)) for score_tick, take_seconds in timing_points),
        key=lambda item: (item[1], item[0]),
    )
    if not ordered:
        return aligned_end_tick

    monotonic: list[tuple[int, float]] = []
    furthest_tick = -1
    for score_tick, take_seconds in ordered:
        if score_tick < furthest_tick:
            continue
        if monotonic and abs(take_seconds - monotonic[-1][1]) < 1e-6:
            if score_tick > monotonic[-1][0]:
                monotonic[-1] = (score_tick, take_seconds)
                furthest_tick = score_tick
            continue
        monotonic.append((score_tick, take_seconds))
        furthest_tick = score_tick

    last_tick, last_seconds = monotonic[-1]
    trailing_seconds = min(8.0, max(0.0, take_duration_seconds - last_seconds))
    if trailing_seconds <= 0.0:
        return max(aligned_end_tick, last_tick)

    slopes: list[float] = []
    recent = monotonic[-13:]
    for (left_tick, left_seconds), (right_tick, right_seconds) in zip(recent, recent[1:]):
        elapsed = right_seconds - left_seconds
        tick_delta = right_tick - left_tick
        if elapsed >= 0.05 and tick_delta > 0:
            slopes.append(tick_delta / elapsed)
    slope = median(slopes) if slopes else SOURCE_TICKS_PER_SECOND
    slope = max(SOURCE_TICKS_PER_SECOND * 0.25, min(SOURCE_TICKS_PER_SECOND * 4.0, slope))
    return max(aligned_end_tick, round(last_tick + trailing_seconds * slope))


def score_projection(piece_id: str, movement: int) -> ScoreProjection:
    """Return the explicit score projection for a supported rehearsal bundle.

    Cached on the inputs' modification times: building a projection parses the
    canonical timeline and a several-hundred-anchor beat map, and the live status
    path calls this on every publish (measured ~56x per performed note, dominating
    the real-time loop). Keying on mtimes keeps an edited beat map or a new human
    correction picked up on the next call, without re-reading unchanged files.
    """

    return _score_projection_cached(
        piece_id, movement, _projection_input_stamp(piece_id, movement)
    )


def _projection_input_stamp(piece_id: str, movement: int) -> tuple[float, ...]:
    """Modification times of every file the projection is built from."""

    candidates = (
        paths.score_bundle_dir(piece_id, movement) / "timeline.machine.json",
        paths.score_performance_beat_map_path(piece_id, movement),
        paths.score_alignment_corrections_path(piece_id, movement),
    )
    stamps = []
    for path in candidates:
        try:
            stamps.append(path.stat().st_mtime)
        except OSError:
            stamps.append(0.0)
    return tuple(stamps)


@lru_cache(maxsize=8)
def _score_projection_cached(
    piece_id: str, movement: int, _stamp: tuple[float, ...]
) -> ScoreProjection:
    timeline = _load_timeline(paths.score_bundle_dir(piece_id, movement))
    if (piece_id, movement) == ("chopin_op11", 2):
        beat_map_path = paths.score_performance_beat_map_path(piece_id, movement)
        if beat_map_path.exists():
            document = load_performance_beat_map(beat_map_path)
            if document.timeline_id != timeline.document.timeline_id:
                raise ValueError("performance beat map targets another canonical timeline")
            corrections = load_human_corrections(
                paths.score_alignment_corrections_path(piece_id, movement)
            )
            correction_suffix = ""
            if corrections and corrections.corrections:
                digest = sha256(
                    corrections.model_dump_json().encode("utf-8")
                ).hexdigest()[:10]
                correction_suffix = f"+human-{digest}"
            return ScoreProjection(
                timeline=timeline,
                mapping_id=document.mapping_id + correction_suffix,
                mapping_review_state=MappingReviewState(document.review_state),
                # The coordinate is always canonical measure/beat.  Review
                # state and per-anchor confidence separately describe how
                # strongly the source-MIDI correspondence is substantiated.
                canonical_position=True,
                anchors=apply_human_corrections(document, corrections),
            )
        anchors = tuple(
            (
                anchor.source_tick,
                timeline.tick_at(anchor.display_measure - 1),
                anchor.confidence,
            )
            for anchor in MOVEMENT_2_VERIFIED_DOWNBEATS
        )
        return ScoreProjection(
            timeline=timeline,
            mapping_id=MAPPING_ID,
            mapping_review_state=MappingReviewState.MACHINE,
            canonical_position=False,
            anchors=anchors,
        )

    # Movement 1's current alignment coordinate is already its score beat
    # coordinate.  Keep the identity fallback explicit for existing tests and
    # APIs while Movement 2 uses the provisional source mapping above.
    return ScoreProjection(
        timeline=timeline,
        mapping_id="canonical-score-identity",
        mapping_review_state=MappingReviewState.REVIEWED,
        canonical_position=True,
        anchors=((0, 0, 1.0),),
    )


def rehearsal_entry_for_measure(
    projection: ScoreProjection,
    selected_score_tick: int,
    solo_reference_path: Path,
) -> RehearsalEntry:
    """Resolve a score click to the pianist's actual onset in that measure.

    A selected printed measure is a passage identity, not necessarily an
    attack at its left barline.  For example, printed m.53 contains an
    orchestral melody followed by the soloist's late pickup.  The cue must end
    at that pickup so its preceding eight seconds remain audible.
    """

    selected = projection.timeline.position_at(selected_score_tick)
    measure_start_tick = projection.timeline.tick_at(selected.measure_index)
    measure_end_tick = (
        projection.timeline.tick_at(selected.measure_index + 1)
        if selected.measure_index + 1 < len(projection.timeline.document.measures)
        else projection.timeline.end_tick
    )
    source_start = projection.source_seconds_at_score_tick(measure_start_tick)
    source_end_tick = MOVEMENT_2_MEASURE_END_SOURCE_TICKS.get(
        selected.measure_index + 1,
        projection.source_tick_at_score_tick(measure_end_tick),
    )
    source_end = source_end_tick / SOURCE_TICKS_PER_SECOND
    first_note = next(
        (
            note
            for note in extract_note_events(solo_reference_path)
            if source_start - 0.001 <= note.time_seconds < source_end - 0.001
        ),
        None,
    )
    entry_seconds = first_note.time_seconds if first_note is not None else source_start
    entry_position = projection.position_at_source_seconds(entry_seconds)
    return RehearsalEntry(
        selected_measure=selected.measure_index + 1,
        selected_score_tick=measure_start_tick,
        entry_source_seconds=entry_seconds,
        entry_position=entry_position,
        entry_pitch=first_note.pitch if first_note is not None else None,
    )


def rehearsal_cue_span(
    projection: ScoreProjection,
    *,
    selected_score_tick: int | None,
    solo_reference_path: Path,
    first_solo_entry_seconds: float,
) -> RehearsalCueSpan:
    """Resolve Cue & Record to start the orchestra at the selected measure.

    The performer picks the measure the orchestra should start from and enters
    whenever they are ready at any point after it -- the lead-in plays the
    rehearsed shape open-endedly, so warming up for longer is just a matter of
    waiting. Starting the cue *before* the selection used to be necessary when
    the orchestra stopped at a predicted entry beat; it only made the behaviour
    surprising (selecting m.52 started the orchestra at m.50, and m.52 is itself
    an orchestral interlude).

    The entry within the bar is still resolved from the solo reference, because
    it remains a useful localization hint for the aligner -- but it no longer
    decides when the orchestra starts or stops.
    """

    if selected_score_tick is None:
        entry_position = projection.position_at_source_seconds(first_solo_entry_seconds)
        entry = RehearsalEntry(
            selected_measure=entry_position.measure_index + 1,
            selected_score_tick=projection.timeline.tick_at(entry_position.measure_index),
            entry_source_seconds=first_solo_entry_seconds,
            entry_position=entry_position,
            entry_pitch=None,
        )
    else:
        entry = rehearsal_entry_for_measure(
            projection,
            selected_score_tick,
            solo_reference_path,
        )

    cue_start_measure_index = projection.timeline.position_at(
        entry.selected_score_tick
    ).measure_index
    cue_start_score_tick = projection.timeline.tick_at(cue_start_measure_index)
    return RehearsalCueSpan(
        entry=entry,
        cue_start_measure_index=cue_start_measure_index,
        cue_start_score_tick=cue_start_score_tick,
        cue_start_source_seconds=projection.source_seconds_at_score_tick(cue_start_score_tick),
    )


def _load_timeline(bundle_root: Path) -> CanonicalTimeline:
    """Load timeline structure without requiring DVC-managed media readiness."""

    # BundleLoaderV2 intentionally permits incomplete readiness, so this also
    # validates manifest/timeline identity and the display map contract.
    return BundleLoaderV2.load(bundle_root).timeline


__all__ = [
    "MAPPING_ID",
    "MOVEMENT_2_REACTIVE_ANCHOR_TICKS",
    "MOVEMENT_2_VERIFIED_DOWNBEATS",
    "MOVEMENT_2_MEASURE_END_SOURCE_TICKS",
    "ProjectedPosition",
    "ProjectionAnchor",
    "RehearsalEntry",
    "RehearsalCueSpan",
    "SOURCE_TICKS_PER_SECOND",
    "ScoreProjection",
    "estimate_performed_end_source_tick",
    "rehearsal_entry_for_measure",
    "rehearsal_cue_span",
    "score_projection",
]
