"""Cell-sample extraction and canonical performance-profile fusion.

Two pieces of docs/design/REHEARSAL_TAKE_COVERAGE_DESIGN.md §2.3/§2.5:

- `compute_cell_samples` resamples one aligned take's raw MIDI (real note
  velocities, real inter-anchor timing, real sustain-pedal CC64) onto the
  0.5-beat profile grid, using the take's `timing_map` anchors to convert
  take-seconds to score-beats. This is "this take's vote for each covered
  cell" -- the input the fold consumes.
- `compute_canonical_cell_samples` resamples a canonical take timing map onto
  integer 960-PPQ score ticks and separates absolute tempo from local rubato.
- `fit_interpretation_for` fuses those canonical observations into performance-profile v2.
"""

from __future__ import annotations

import bisect
import statistics
from collections.abc import Sequence
from pathlib import Path

import mido

from aimusic.accompaniment.offline_alignment import (
    PiecewiseLinearTimingMap,
    TimingAnchor,
    _build_tempo_map,
    _seconds_at_tick,
)
from aimusic.core.time import utc_now
from aimusic.takes import store
from aimusic.takes.lifecycle import (
    AlignedResultV2,
    AnalysisState,
    CellSampleV2,
    ProfileMembership,
    TakeDocV2,
    TimingMapPointV2,
    UserDisposition,
)
from aimusic.takes.models import (
    CellSample,
    Interpretation,
    PerformanceProfileCell,
    TimingMapPoint,
)

GRID_BEATS = 0.5
CANONICAL_PPQ = 960
GRID_STEP_TICKS = 480
EDGE_TRIM_HEAD_BEATS = 1.0
EDGE_TRIM_TAIL_BEATS = 0.5
EDGE_TRIM_QUALITY_SCALE = 0.5  # design doc §2.3: scaled down, not excluded
RECENCY_HALF_LIFE_TAKES = 8  # design doc §2.5: per cell, not global


def _load_take_events(midi_path: Path) -> tuple[list[tuple[float, int]], list[tuple[float, float]]]:
    """Extract (take_seconds, velocity) note-ons and (take_seconds, depth 0..1) pedal events.

    Uses the same global tempo map as offline_alignment.py's `extract_note_events`:
    Type 1 MIDI files put tempo changes on track 0, affecting every track, so
    tracking `tempo` locally per track (as an earlier version of this function
    did) silently mis-times every other track's events.
    """

    midi = mido.MidiFile(midi_path, clip=True)
    tempo_map = _build_tempo_map(midi)
    notes: list[tuple[float, int]] = []
    pedal: list[tuple[float, float]] = []
    for track in midi.tracks:
        elapsed_ticks = 0
        tempo_index = 0
        for message in track:
            elapsed_ticks += message.time
            seconds, tempo_index = _seconds_at_tick(
                elapsed_ticks, midi.ticks_per_beat, tempo_map, tempo_index
            )
            if message.type == "note_on" and message.velocity > 0:
                notes.append((seconds, message.velocity))
            elif message.type == "control_change" and message.control == 64:
                pedal.append((seconds, message.value / 127.0))
    notes.sort(key=lambda item: item[0])
    pedal.sort(key=lambda item: item[0])
    return notes, pedal


def _pedal_depth_at(pedal_events: list[tuple[float, float]], take_seconds: float) -> float:
    """Step-function sample: the last pedal value at or before `take_seconds`."""

    idx = bisect.bisect_right(pedal_events, take_seconds, key=lambda event: event[0])
    return pedal_events[idx - 1][1] if idx > 0 else 0.0


def compute_cell_samples(
    take_midi_path: Path,
    *,
    score_start_beat: float,
    score_end_beat: float,
    match_rate: float,
    timing_map: list[TimingMapPoint] | tuple[TimingMapPoint, ...],
    grid_beats: float = GRID_BEATS,
) -> list[CellSample]:
    """Resample one take's real MIDI onto the profile grid (design doc §2.3).

    Returns [] when there aren't enough matched anchors to interpolate a
    take-seconds<->score-beat mapping -- no real data beats fake data.
    """

    if len(timing_map) < 1 or score_end_beat <= score_start_beat:
        return []

    anchors_by_beat = tuple(
        sorted(
            (
                TimingAnchor(
                    reference_time_seconds=point.score_beat,
                    performance_time_seconds=point.take_seconds,
                    reference_index=i,
                    performance_index=i,
                )
                for i, point in enumerate(timing_map)
            ),
            key=lambda a: a.reference_time_seconds,
        )
    )
    anchors_by_time = tuple(
        sorted(
            (
                TimingAnchor(
                    reference_time_seconds=point.take_seconds,
                    performance_time_seconds=point.score_beat,
                    reference_index=i,
                    performance_index=i,
                )
                for i, point in enumerate(timing_map)
            ),
            key=lambda a: a.reference_time_seconds,
        )
    )
    try:
        beat_to_time = PiecewiseLinearTimingMap(anchors_by_beat)
        time_to_beat = PiecewiseLinearTimingMap(anchors_by_time)
    except ValueError:
        return []

    notes, pedal_events = _load_take_events(take_midi_path)
    if not notes:
        return []

    # Bucket real note velocities onto the *absolute* grid (multiples of
    # grid_beats from 0), via each note's interpolated score-beat position --
    # not relative to this take's own score_start_beat. Overlapping takes
    # almost never start on the same note, so an absolute grid is what lets
    # their cell_samples land on the same cells and actually fuse. Keyed by
    # the grid index (int), not the float beat: the sample loop below
    # recomputes `cell = k * grid_beats` from the same index, so both paths
    # produce bit-identical keys -- accumulating a float `cell` via repeated
    # `+= grid_beats` instead would drift out of sync with these keys for any
    # grid_beats that isn't exactly representable in binary floating point.
    velocity_buckets: dict[int, list[int]] = {}
    for take_seconds, velocity in notes:
        beat = time_to_beat.map_time(take_seconds)
        if beat < score_start_beat or beat >= score_end_beat:
            continue
        grid_index = round(beat / grid_beats)
        velocity_buckets.setdefault(grid_index, []).append(velocity)

    head_trim_end = score_start_beat + EDGE_TRIM_HEAD_BEATS
    tail_trim_start = score_end_beat - EDGE_TRIM_TAIL_BEATS

    samples: list[CellSample] = []
    start_index = round(score_start_beat / grid_beats)
    end_index = round(score_end_beat / grid_beats)
    for grid_index in range(start_index, end_index + 1):
        velocities = velocity_buckets.get(grid_index)
        if not velocities:
            continue
        cell = grid_index * grid_beats

        # Local tempo: seconds-per-beat slope of the take/score timing map
        # around this cell, i.e. the real "period_s" -- not a note-density proxy.
        # Unclamped and divided by the constant grid_beats (not the clamped
        # interval width) so a cell near score_start_beat/score_end_beat can't
        # produce a near-zero denominator; PiecewiseLinearTimingMap already
        # extrapolates linearly past its anchors, which is exactly the
        # constant-slope assumption a local tempo estimate wants here.
        half_step = grid_beats / 2
        left_beat = cell - half_step
        right_beat = cell + half_step
        period_s = (
            beat_to_time.map_time(right_beat) - beat_to_time.map_time(left_beat)
        ) / grid_beats

        # Clamp the query point to the matched span: extrapolating a
        # step-function pedal value beyond the anchors isn't meaningful, and
        # a grid cell can round to just outside [score_start_beat,
        # score_end_beat) at the ends.
        clamped_beat = min(max(cell, score_start_beat), score_end_beat)
        cell_take_seconds = beat_to_time.map_time(clamped_beat)
        pedal_depth = _pedal_depth_at(pedal_events, cell_take_seconds)

        quality = match_rate
        if cell < head_trim_end or cell >= tail_trim_start:
            quality *= EDGE_TRIM_QUALITY_SCALE

        samples.append(
            CellSample(
                beat=cell,
                period_s=max(0.001, period_s),
                velocity=statistics.fmean(velocities),
                pedal=pedal_depth,
                quality=quality,
            )
        )

    return samples


def compute_canonical_cell_samples(
    take_midi_path: Path,
    *,
    start_score_tick: int,
    end_score_tick: int,
    match_rate: float,
    timing_map: tuple[TimingMapPointV2, ...] | list[TimingMapPointV2],
    canonical_ppq: int = CANONICAL_PPQ,
    grid_step_ticks: int = GRID_STEP_TICKS,
) -> tuple[tuple[CellSampleV2, ...], float | None]:
    """Resample one aligned take into explicit canonical performance units.

    Timing points establish a monotonic ``score_tick -> take_seconds`` warp.
    The local slope is stored as seconds per *quarter note*, independent of
    the half-quarter sampling grid.  Each sample also stores a dimensionless
    ratio to this take's robust baseline, so global pace and local rubato are
    never conflated again.
    """

    if end_score_tick <= start_score_tick or grid_step_ticks <= 0:
        return (), None
    points = _monotonic_canonical_points(timing_map)
    if len(points) < 2:
        return (), None

    score_anchors = tuple(
        TimingAnchor(
            reference_time_seconds=score_tick / canonical_ppq,
            performance_time_seconds=take_seconds,
            reference_index=index,
            performance_index=index,
        )
        for index, (score_tick, take_seconds) in enumerate(points)
    )
    time_anchors = tuple(
        TimingAnchor(
            reference_time_seconds=take_seconds,
            performance_time_seconds=score_tick / canonical_ppq,
            reference_index=index,
            performance_index=index,
        )
        for index, (score_tick, take_seconds) in enumerate(points)
    )
    try:
        score_to_time = PiecewiseLinearTimingMap(score_anchors)
        time_to_score = PiecewiseLinearTimingMap(time_anchors)
    except ValueError:
        return (), None

    notes, pedal_events = _load_take_events(take_midi_path)
    if not notes:
        return (), None

    velocity_buckets: dict[int, list[int]] = {}
    for take_seconds, velocity in notes:
        score_tick = round(time_to_score.map_time(take_seconds) * canonical_ppq)
        if score_tick < start_score_tick or score_tick >= end_score_tick:
            continue
        grid_index = round(score_tick / grid_step_ticks)
        velocity_buckets.setdefault(grid_index, []).append(velocity)

    head_trim_end = start_score_tick + round(EDGE_TRIM_HEAD_BEATS * canonical_ppq)
    tail_trim_start = end_score_tick - round(EDGE_TRIM_TAIL_BEATS * canonical_ppq)
    raw: list[tuple[int, float, float, float, float]] = []
    start_index = round(start_score_tick / grid_step_ticks)
    end_index = round(end_score_tick / grid_step_ticks)
    half_step_quarters = (grid_step_ticks / 2) / canonical_ppq
    for grid_index in range(start_index, end_index + 1):
        velocities = velocity_buckets.get(grid_index)
        if not velocities:
            continue
        score_tick = grid_index * grid_step_ticks
        score_quarters = score_tick / canonical_ppq
        seconds_per_quarter = (
            score_to_time.map_time(score_quarters + half_step_quarters)
            - score_to_time.map_time(score_quarters - half_step_quarters)
        ) / (2 * half_step_quarters)
        clamped_tick = min(max(score_tick, start_score_tick), end_score_tick)
        take_seconds = score_to_time.map_time(clamped_tick / canonical_ppq)
        quality = match_rate
        if score_tick < head_trim_end or score_tick >= tail_trim_start:
            quality *= EDGE_TRIM_QUALITY_SCALE
        raw.append(
            (
                score_tick,
                max(0.001, seconds_per_quarter),
                statistics.fmean(velocities),
                _pedal_depth_at(pedal_events, take_seconds),
                quality,
            )
        )

    if not raw:
        return (), None
    base_seconds_per_quarter = _weighted_median(
        [row[1] for row in raw], [row[4] for row in raw]
    )
    samples = tuple(
        CellSampleV2(
            score_tick=score_tick,
            seconds_per_quarter=seconds_per_quarter,
            rubato_ratio=seconds_per_quarter / base_seconds_per_quarter,
            velocity=velocity,
            pedal=pedal,
            quality=quality,
        )
        for score_tick, seconds_per_quarter, velocity, pedal, quality in raw
    )
    return samples, base_seconds_per_quarter


def _monotonic_canonical_points(
    timing_map: tuple[TimingMapPointV2, ...] | list[TimingMapPointV2],
) -> tuple[tuple[int, float], ...]:
    """Collapse chord duplicates and alignment regressions to a strict warp."""

    ordered = sorted(
        ((point.score_tick, point.take_seconds) for point in timing_map),
        key=lambda item: (item[1], item[0]),
    )
    monotonic: list[tuple[int, float]] = []
    for score_tick, take_seconds in ordered:
        if monotonic and take_seconds <= monotonic[-1][1] + 1e-9:
            if score_tick > monotonic[-1][0]:
                monotonic[-1] = (score_tick, monotonic[-1][1])
            continue
        if monotonic and score_tick <= monotonic[-1][0]:
            continue
        monotonic.append((score_tick, take_seconds))
    return tuple(monotonic)


def _weighted_median(values: list[float], weights: list[float]) -> float:
    pairs = sorted(zip(values, weights), key=lambda item: item[0])
    total = sum(weights)
    if total <= 0:
        return statistics.fmean(values)
    cumulative = 0.0
    half = total / 2.0
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= half:
            return value
    return pairs[-1][0]


def _weighted_mad(values: list[float], weights: list[float], median: float) -> float:
    deviations = [abs(v - median) for v in values]
    return _weighted_median(deviations, weights)


def selects(piece_id: str, movement: int) -> list[TakeDocV2]:
    """Return profile-eligible takes in the fold's deterministic recency order."""

    return sorted(
        (
            take
            for take in store.list_takes_v2(piece_id, movement)
            if take.analysis_state == AnalysisState.ALIGNED
            and take.disposition == UserDisposition.KEPT
            and take.profile_membership == ProfileMembership.INCLUDED
        ),
        key=lambda take: take.recorded_at,
        reverse=True,
    )


def active_take_input_revision(piece_id: str, movement: int) -> str:
    """Describe the active fold inputs without reading alignment payloads.

    This is safe to use on request paths that only need to pin or compare a
    materialization revision.  The expensive aligned cell-sample reads and
    weighted fold remain background work.
    """

    takes = selects(piece_id, movement)
    return ";".join(
        f"{take.take_id}:{take.lifecycle_revision}" for take in takes
    ) or "empty"


def active_take_ids(piece_id: str, movement: int) -> tuple[str, ...]:
    """Return the take ids that must be represented by the cached profile."""

    return tuple(take.take_id for take in selects(piece_id, movement))


def fold_cells(
    aligned_results: Sequence[AlignedResultV2],
) -> tuple[tuple[PerformanceProfileCell, ...], float | None]:
    """Fold aligned takes (most-recent-first) into profile cells + base tempo.

    Pure -- no store I/O. This is the single fitting implementation shared by
    :func:`fit_interpretation_for` (the persisted Interpretation) and the
    offline evaluator, so fit-quality metrics score exactly the model the
    runtime reads. Recency rank is per cell (§2.5): a cell only ranks among the
    takes that actually voted on it.
    """

    # score_tick -> (velocity, seconds/quarter, rubato ratio, pedal, quality, rank)
    cell_votes: dict[int, list[tuple[float, float, float, float, float, int]]] = {}
    base_votes: list[tuple[float, float, int]] = []

    for result in aligned_results:
        if result.coordinate_system != "canonical_score":
            raise ValueError(
                f"take {result.take_id} requires the one-time canonical performance migration"
            )
        if result.base_seconds_per_quarter is not None:
            base_votes.append(
                (result.base_seconds_per_quarter, result.match_rate, len(base_votes))
            )
        for sample in result.cell_samples:
            votes = cell_votes.setdefault(sample.score_tick, [])
            rank = len(votes)  # recency rank within this cell only
            votes.append(
                (
                    sample.velocity,
                    sample.seconds_per_quarter,
                    sample.rubato_ratio,
                    sample.pedal,
                    sample.quality,
                    rank,
                )
            )

    cells: list[PerformanceProfileCell] = []

    for score_tick in sorted(cell_votes):
        votes = cell_votes[score_tick]
        n = len(votes)
        weights = [
            quality * (0.5 ** (rank / RECENCY_HALF_LIFE_TAKES))
            for _velocity, _spq, _rubato, _pedal, quality, rank in votes
        ]

        velocities = [v[0] for v in votes]
        seconds_per_quarters = [v[1] for v in votes]
        rubato_ratios = [v[2] for v in votes]
        pedals = [v[3] for v in votes]

        vel_median = _weighted_median(velocities, weights)
        spq_median = _weighted_median(seconds_per_quarters, weights)
        rubato_median = _weighted_median(rubato_ratios, weights)
        pedal_median = _weighted_median(pedals, weights)

        cells.append(
            PerformanceProfileCell(
                score_tick=score_tick,
                seconds_per_quarter=round(spq_median, 6),
                seconds_per_quarter_mad=round(
                    _weighted_mad(seconds_per_quarters, weights, spq_median), 6
                ),
                rubato_ratio=round(rubato_median, 6),
                rubato_ratio_mad=round(
                    _weighted_mad(rubato_ratios, weights, rubato_median), 6
                ),
                velocity=round(vel_median, 2),
                velocity_mad=round(_weighted_mad(velocities, weights, vel_median), 2),
                pedal=round(pedal_median, 4),
                pedal_mad=round(_weighted_mad(pedals, weights, pedal_median), 4),
                support=n,
                mean_quality=round(statistics.fmean(v[4] for v in votes), 4),
            )
        )

    base_seconds_per_quarter = None
    if base_votes:
        base_seconds_per_quarter = round(
            _weighted_median(
                [vote[0] for vote in base_votes],
                [
                    quality * (0.5 ** (rank / RECENCY_HALF_LIFE_TAKES))
                    for _base, quality, rank in base_votes
                ],
            ),
            6,
        )

    return tuple(cells), base_seconds_per_quarter


def fit_interpretation_for(piece_id: str, movement: int) -> Interpretation:
    """Fit the Interpretation from the selects (kept + included takes).

    Reads the selected takes' aligned results in recency order and folds them
    with :func:`fold_cells`, then wraps the result with provenance (which takes,
    which input revision) so the artifact stays reproducible.
    """

    takes_by_recency = selects(piece_id, movement)
    aligned: list[AlignedResultV2] = []
    for take in takes_by_recency:
        result = store.get_aligned_result_v2(piece_id, movement, take.take_id)
        if result is not None:
            aligned.append(result)
    cells, base_seconds_per_quarter = fold_cells(aligned)
    return Interpretation(
        piece_id=piece_id,
        movement=movement,
        take_count=len(takes_by_recency),
        updated=utc_now(),
        base_seconds_per_quarter=base_seconds_per_quarter,
        cells=cells,
        moments=(),
        input_revision=";".join(
            f"{take.take_id}:{take.lifecycle_revision}" for take in takes_by_recency
        ) or "empty",
        included_take_ids=tuple(take.take_id for take in takes_by_recency),
    )


__all__ = [
    "CellSample",
    "CANONICAL_PPQ",
    "GRID_BEATS",
    "GRID_STEP_TICKS",
    "EDGE_TRIM_HEAD_BEATS",
    "EDGE_TRIM_TAIL_BEATS",
    "RECENCY_HALF_LIFE_TAKES",
    "active_take_input_revision",
    "active_take_ids",
    "selects",
    "compute_cell_samples",
    "compute_canonical_cell_samples",
    "fold_cells",
    "fit_interpretation_for",
    "fit_interpretation_for",
]
