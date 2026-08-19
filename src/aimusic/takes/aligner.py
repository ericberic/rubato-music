"""Production take-localization and alignment pipeline (roadmap item 3).

Ports the two-stage approach de-risked in
`tests/accompaniment/test_localization_spike.py` (roadmap item 1) into
reusable production code: pitch n-gram seeding to localize a take somewhere
in the score, then the existing `align_note_events` local aligner
(`offline_alignment.py`) to refine the match inside that window. See
docs/design/REHEARSAL_TAKE_COVERAGE_DESIGN.md §2.4 for the algorithm rationale and
§2.3 for the full `aligned.json` contract. `cell_samples` (this take's real
velocity/timing/pedal resampled onto the profile grid, §2.3) is delegated to
`aimusic.takes.profile.compute_cell_samples`, which the fold pipeline
(roadmap item 4) also owns; this module just calls it once alignment has a
`timing_map` to resample against.

The local aligner still operates in the reference MIDI coordinate because that
is the strongest pitch/timing comparison. Persistence converts its matched
ticks once onto the canonical notated beat grid and keeps reference ticks only
as diagnostics.
"""

from __future__ import annotations

import logging
import statistics
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import mido

from aimusic.accompaniment.offline_alignment import (
    AlignmentResult,
    MidiNoteEvent,
    align_note_events,
)
from aimusic.accompaniment.oguri import oguri_movement
from aimusic.accompaniment.oguri_extract import extract_oguri_movement
from aimusic.accompaniment.rehearsal_position import (
    rehearsal_entry_for_measure,
    score_projection,
)
from aimusic.core import paths
from aimusic.core.event_journal import journal_exception
from aimusic.core.events import events
from aimusic.server.schemas import TakeAlignmentDone
from aimusic.takes import profile, store
from aimusic.takes.lifecycle import (
    AnalysisState,
    JobKind,
    JobState,
    LifecycleFailure,
)
from aimusic.takes.models import AlignedResult, LocalizationCandidate, TimingMapPoint

CHORD_GROUP_BEAT_EPS = 0.02  # notes within this many beats are one chord
NGRAM_LENGTHS = (4, 5, 6)  # design doc §2.4
DIAGONAL_BIN = 3  # positions; absorbs drift from dropped notes
AMBIGUITY_MARGIN = 0.15  # design doc §2.4: top-2 within 15% -> ambiguous
ALIGNMENT_WINDOW_SLACK = 15  # notes; stage-2 window half-width around the seed
MATCH_RATE_GATE = 0.75  # design doc §2.4 stage 3: below this, unalignable

ALIGNER_NAME = "seeded-local-v2"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChordNote:
    """One note of a flattened, chord-grouped canonical pitch sequence."""

    position: int
    beat: float
    time_seconds: float
    pitch: int
    chord_id: int


NgramIndex = dict[tuple[int, ...], list[int]]


def load_canonical_note_sequence(
    midi_path: Path | str,
    *,
    track_name_contains: tuple[str, ...] = (),
) -> tuple[ChordNote, ...]:
    """Parse a MIDI file into a flattened, chord-grouped canonical pitch sequence.

    Chords (near-simultaneous note-ons) are grouped and re-sorted by pitch so
    n-gram matching has a stable order regardless of arpeggiation/roll noise
    (design doc §2.4). When `track_name_contains` is given, only the first
    track whose name matches is used (e.g. the reference's "PIANO SOLO"
    track); otherwise all tracks are merged, which is correct for a take
    recording's single captured track.
    """

    midi = mido.MidiFile(midi_path, clip=True)
    markers = tuple(marker.upper() for marker in track_name_contains)
    if markers:
        tracks = None
        for track in midi.tracks:
            if any(
                message.type == "track_name"
                and any(marker in message.name.upper() for marker in markers)
                for message in track
            ):
                tracks = [track]
                break
        if tracks is None:
            raise ValueError(f"No track found containing any of the markers: {track_name_contains}")
    else:
        tracks = list(midi.tracks)

    tempo_messages = []
    for track in midi.tracks:
        elapsed = 0
        for message in track:
            elapsed += message.time
            if message.type == "set_tempo":
                tempo_messages.append((elapsed, message))

    target_messages = []
    for track in tracks:
        elapsed = 0
        for message in track:
            elapsed += message.time
            if message.type == "note_on" and message.velocity > 0:
                target_messages.append((elapsed, message))

    merged = sorted(
        [(tick, 0, msg) for tick, msg in tempo_messages]
        + [(tick, 1, msg) for tick, msg in target_messages],
        key=lambda x: (x[0], x[1]),
    )

    raw: list[tuple[float, float, int]] = []  # (beat, seconds, pitch)
    tempo = 500_000
    current_tick = 0
    current_seconds = 0.0
    for abs_tick, _, message in merged:
        delta_ticks = abs_tick - current_tick
        current_seconds += mido.tick2second(delta_ticks, midi.ticks_per_beat, tempo)
        current_tick = abs_tick
        if message.type == "set_tempo":
            tempo = message.tempo
        elif message.type == "note_on":
            raw.append((abs_tick / midi.ticks_per_beat, current_seconds, message.note))

    chords: list[list[tuple[float, float, int]]] = []
    for beat, seconds, pitch in raw:
        if chords and beat - chords[-1][0][0] <= CHORD_GROUP_BEAT_EPS:
            chords[-1].append((beat, seconds, pitch))
        else:
            chords.append([(beat, seconds, pitch)])

    flattened: list[ChordNote] = []
    for chord_id, chord in enumerate(chords):
        for beat, seconds, pitch in sorted(chord, key=lambda item: item[2]):
            flattened.append(ChordNote(len(flattened), beat, seconds, pitch, chord_id))
    return tuple(flattened)


def build_ngram_index(reference: tuple[ChordNote, ...]) -> NgramIndex:
    """Index pitch n-grams (n=4..6) to the reference positions they occur at."""

    index: NgramIndex = {}
    pitches = [note.pitch for note in reference]
    for n in NGRAM_LENGTHS:
        for start in range(len(pitches) - n + 1):
            gram = tuple(pitches[start : start + n])
            index.setdefault(gram, []).append(start)
    return index


@dataclass(frozen=True)
class VoteCandidate:
    """One stage-1 localization candidate: a reference position and its
    n-gram vote count. Distinct from `aimusic.takes.models.
    LocalizationCandidate` (the persisted `{start_beat, score,
    start_position}` shape) -- this is the pre-serialization voting result.
    """

    start_position: int
    votes: int


@dataclass(frozen=True)
class LocalizationResult:
    candidates: tuple[VoteCandidate, ...]  # desc by votes
    chosen_start: int | None
    ambiguous: bool


def localize(take_pitches: list[int], index: NgramIndex) -> LocalizationResult:
    """BLAST-style seed voting: histogram over (ref_position - take_position)."""

    bucket_diagonals: dict[int, list[int]] = {}
    for n in NGRAM_LENGTHS:
        for take_start in range(len(take_pitches) - n + 1):
            gram = tuple(take_pitches[take_start : take_start + n])
            for ref_start in index.get(gram, ()):
                diagonal = ref_start - take_start
                bucket = round(diagonal / DIAGONAL_BIN)
                bucket_diagonals.setdefault(bucket, []).append(diagonal)

    if not bucket_diagonals:
        return LocalizationResult((), None, False)

    ranked = sorted(bucket_diagonals.items(), key=lambda kv: -len(kv[1]))
    raw_candidates = [
        (round(statistics.median(diagonals)), len(diagonals)) for _, diagonals in ranked
    ]
    # A negative diagonal means the hypothesis starts before the reference
    # score. It can be useful as transient voting noise, but it is not a valid
    # persisted score location (and used to crash the stricter v2 artifact).
    merged = [item for item in _merge_nearby_candidates(raw_candidates) if item[0] >= 0][:5]
    if not merged:
        return LocalizationResult((), None, False)
    candidates = tuple(VoteCandidate(position, votes) for position, votes in merged)
    top_votes = candidates[0].votes
    ambiguous = len(candidates) > 1 and candidates[1].votes >= top_votes * (1 - AMBIGUITY_MARGIN)
    return LocalizationResult(candidates, candidates[0].start_position, ambiguous)


def _merge_nearby_candidates(
    ranked_candidates: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Collapse candidate positions that are one score location split across
    adjacent diagonal buckets by quantization noise.

    Two candidates within ALIGNMENT_WINDOW_SLACK of each other would be
    captured by the same stage-2 alignment window anyway, so they are not a
    meaningful second location for the ambiguity check -- only a candidate
    outside that radius represents a real second placement (e.g. a genuinely
    repeated passage).
    """

    merged: list[tuple[int, int]] = []  # (representative_position, total_votes)
    for position, votes in ranked_candidates:
        for i, (rep_position, rep_votes) in enumerate(merged):
            if abs(position - rep_position) <= ALIGNMENT_WINDOW_SLACK:
                merged[i] = (rep_position, rep_votes + votes)
                break
        else:
            merged.append((position, votes))
    return sorted(merged, key=lambda item: -item[1])


def align_within_window(
    reference: tuple[ChordNote, ...],
    take_notes: tuple[ChordNote, ...],
    predicted_start: int,
    *,
    slack: int = ALIGNMENT_WINDOW_SLACK,
) -> tuple[AlignmentResult, tuple[ChordNote, ...]]:
    """Run the existing local-alignment aligner in a window around the seed."""

    window_start = min(len(reference), max(0, predicted_start - slack))
    window_end = min(len(reference), max(window_start, predicted_start + len(take_notes) + slack))
    window = reference[window_start:window_end]

    reference_events = tuple(
        MidiNoteEvent(
            index=i,
            time_seconds=note.time_seconds,
            pitch=note.pitch,
            velocity=64,
            channel=0,
            track_index=0,
        )
        for i, note in enumerate(window)
    )
    take_events = tuple(
        MidiNoteEvent(
            index=i,
            time_seconds=note.time_seconds,
            pitch=note.pitch,
            velocity=64,
            channel=0,
            track_index=0,
        )
        for i, note in enumerate(take_notes)
    )
    return align_note_events(reference_events, take_events), window


def _align_at_position(
    take_id: str,
    take_midi_path: Path | str,
    reference: tuple[ChordNote, ...],
    take_notes: tuple[ChordNote, ...],
    chosen_start: int,
    *,
    ambiguous: bool,
    candidates: tuple[LocalizationCandidate, ...],
) -> AlignedResult:
    """Stage 2/3: fine-align + qualify at a given reference start position and
    build the `aligned.json` artifact.

    Shared by `align_take`'s localize-driven path and `resolve_take`'s
    candidate-driven path (design doc §4.3 "It was here / It was there") --
    both ultimately just pick a reference start position and need the same
    windowed alignment, quality gate, and cell-sample resampling from there.
    """

    alignment, window = align_within_window(reference, take_notes, chosen_start)
    matched = sorted(alignment.matched_pairs, key=lambda pair: pair.reference_index)
    match_rate = alignment.summary.pitch_match_count / len(take_notes) if take_notes else 0.0

    if matched:
        score_start_beat = window[matched[0].reference_index].beat
        score_end_beat = window[matched[-1].reference_index].beat
    else:
        clamped = max(0, min(chosen_start, len(reference) - 1))
        score_start_beat = reference[clamped].beat
        score_end_beat = score_start_beat

    timing_map = tuple(
        TimingMapPoint(
            score_beat=window[pair.reference_index].beat,
            take_seconds=pair.performance_time_seconds,
        )
        for pair in matched
    )

    cell_samples = tuple(
        profile.compute_cell_samples(
            Path(take_midi_path),
            score_start_beat=score_start_beat,
            score_end_beat=score_end_beat,
            match_rate=match_rate,
            timing_map=timing_map,
        )
    )

    return AlignedResult(
        take_id=take_id,
        aligner=ALIGNER_NAME,
        score_start_beat=score_start_beat,
        score_end_beat=score_end_beat,
        match_rate=match_rate,
        ambiguous=ambiguous,
        matched_notes=alignment.summary.pitch_match_count,
        extra_notes=alignment.summary.extra_performance_note_count,
        missing_notes=alignment.summary.missing_reference_note_count,
        timing_map=timing_map,
        candidates=candidates,
        cell_samples=cell_samples,
        edge_trim_beats=(profile.EDGE_TRIM_HEAD_BEATS, profile.EDGE_TRIM_TAIL_BEATS),
    )


def align_take(
    take_id: str,
    take_midi_path: Path | str,
    reference_midi_path: Path | str,
    *,
    reference_track_name_contains: tuple[str, ...] = ("PIANO SOLO",),
    expected_entry_beat: float | None = None,
    expected_entry_seconds: float = 0.0,
) -> AlignedResult:
    """Localize and align a take's MIDI against the score's solo reference.

    ``expected_entry_beat`` is the resolved reference-MIDI entry position.
    The recording may contain an orchestral lead-in, so the corresponding
    take-time anchor is supplied separately rather than pretending the first
    captured note belongs at the selected downbeat.
    """

    reference = load_canonical_note_sequence(
        reference_midi_path, track_name_contains=reference_track_name_contains
    )
    take_notes = load_canonical_note_sequence(take_midi_path)
    if not reference:
        raise ValueError("reference has no note events to align")
    if not take_notes:
        raise ValueError("take has no note events to align")

    index = build_ngram_index(reference)
    take_pitches = [note.pitch for note in take_notes]
    localization = localize(take_pitches, index)

    if localization.chosen_start is None and expected_entry_beat is None:
        return AlignedResult(
            take_id=take_id,
            aligner=ALIGNER_NAME,
            score_start_beat=0.0,
            score_end_beat=0.0,
            match_rate=0.0,
            ambiguous=False,
            matched_notes=0,
            extra_notes=len(take_notes),
            missing_notes=0,
            timing_map=(),
            candidates=(),
            cell_samples=(),
            edge_trim_beats=(profile.EDGE_TRIM_HEAD_BEATS, profile.EDGE_TRIM_TAIL_BEATS),
        )

    total_votes = sum(candidate.votes for candidate in localization.candidates) or 1
    candidates = tuple(
        LocalizationCandidate(
            start_beat=reference[max(0, min(candidate.start_position, len(reference) - 1))].beat,
            start_position=candidate.start_position,
            score=candidate.votes / total_votes,
        )
        for candidate in localization.candidates
    )

    chosen_start = localization.chosen_start
    ambiguous = localization.ambiguous
    if expected_entry_beat is not None:
        reference_beats = [note.beat for note in reference]
        insertion = bisect_left(reference_beats, expected_entry_beat)
        nearby = [index for index in (insertion - 1, insertion) if 0 <= index < len(reference)]
        entry_reference_position = min(
            nearby, key=lambda index: abs(reference[index].beat - expected_entry_beat)
        )
        entry_take_position = next(
            (
                index
                for index, note in enumerate(take_notes)
                if note.time_seconds >= max(0.0, expected_entry_seconds - 0.05)
            ),
            len(take_notes) - 1,
        )
        # A cued recording can contain MIDI noise or an echoed cue before the
        # pianist's selected entrance.  It is context, not part of the take.
        # Slice at the known take-time boundary before fine alignment so those
        # events cannot pull the window earlier or depress the match rate.
        take_notes = take_notes[entry_take_position:]
        if not take_notes:
            raise ValueError("take has no note events after the selected entrance")
        chosen_start = entry_reference_position
        ambiguous = False

        cue_candidate = LocalizationCandidate(
            start_beat=reference[chosen_start].beat,
            start_position=chosen_start,
            score=1.0,
        )
        candidates = (cue_candidate,) + tuple(
            candidate for candidate in candidates if candidate.start_position != chosen_start
        )

    if chosen_start is None:  # guarded by the no-localization return above
        raise ValueError("no alignment start available")

    return _align_at_position(
        take_id,
        take_midi_path,
        reference,
        take_notes,
        chosen_start,
        ambiguous=ambiguous,
        candidates=candidates,
    )


def resolve_reference_midi_path(piece_id: str, movement: int) -> Path:
    """Locate (and lazily extract) the solo reference MIDI for a piece/movement."""

    if piece_id != "chopin_op11" or movement != 2:
        raise ValueError(f"No solo reference registered for {piece_id} movement {movement}")
    movement_source = oguri_movement(movement)
    if not movement_source.solo_reference_path.exists():
        if not movement_source.local_path.exists():
            raise FileNotFoundError(f"Oguri source MIDI is missing: {movement_source.local_path}")
        extract_oguri_movement(movement_source)
    return movement_source.solo_reference_path


def _publish_alignment_done(take: store.Take) -> None:
    """Emit `take:alignment_done` for the WS event feed (design doc §2.7).

    Lets the capture-cockpit toast resolve in place from "placing it in the
    score..." to the final `kept`/`ambiguous`/`unalignable` wording (§3.1)
    without polling. Skipped for `discarded`: that status change is a
    user-initiated action the background worker deliberately never
    overrides (see the discard-race comments above), not an alignment
    outcome the toast should resolve to.
    """

    if take.status == store.DISCARDED:
        return
    aligned = store.get_aligned_result(take.piece_id, take.movement, take.take_id)
    aligned_v2 = store.get_aligned_result_v2(take.piece_id, take.movement, take.take_id)
    lifecycle = store.get_take_v2(take.piece_id, take.movement, take.take_id)
    target_tick = (
        lifecycle.cue.target_score_tick
        if lifecycle.cue is not None and lifecycle.cue.kind == "from_position"
        else lifecycle.placement_hint.target_score_tick
        if lifecycle.placement_hint is not None
        else None
    )
    events.publish(
        TakeAlignmentDone(
            type="take:alignment_done",
            take_id=take.take_id,
            piece_id=take.piece_id,
            movement=take.movement,
            status=take.status,
            analysis_state=lifecycle.analysis_state,
            disposition=lifecycle.disposition,
            score_start_beat=(
                aligned_v2.start_score_tick / store.CANONICAL_PPQ
                if aligned_v2 and aligned_v2.coordinate_system == "canonical_score"
                else aligned.score_start_beat if aligned else None
            ),
            score_end_beat=(
                aligned_v2.end_score_tick / store.CANONICAL_PPQ
                if aligned_v2 and aligned_v2.coordinate_system == "canonical_score"
                else aligned.score_end_beat if aligned else None
            ),
            placement_basis="selected_passage" if target_tick is not None else "pitch_localization",
            target_score_tick=target_tick,
            failure_code=lifecycle.failure.code if lifecycle.failure else None,
            failure_message=lifecycle.failure.message if lifecycle.failure else None,
            aligner=aligned.aligner if aligned else None,
            match_rate=aligned.match_rate if aligned else None,
            matched_notes=aligned.matched_notes if aligned else None,
            extra_notes=aligned.extra_notes if aligned else None,
            missing_notes=aligned.missing_notes if aligned else None,
        )
    )


def align_and_store(piece_id: str, movement: int, take_id: str) -> store.Take:
    """Run the alignment pipeline for a stored take and persist the result.

    Synchronous: the design budgets <5s per take (§2.7), so this is meant to
    be called from a single background worker thread (`AlignmentWorker`
    below), not inline on the recording/request thread.
    """

    _ensure_analysis_running(piece_id, movement, take_id)
    take = store.get_take(piece_id, movement, take_id)
    take_midi_path = paths.take_dir(piece_id, movement, take_id, create=False) / take.midi_path
    reference_path = resolve_reference_midi_path(piece_id, movement)

    lifecycle = store.get_take_v2(piece_id, movement, take_id)
    target_score_tick = None
    expected_entry_seconds = 0.0
    if lifecycle.cue is not None and lifecycle.cue.kind == "from_position":
        target_score_tick = lifecycle.cue.target_score_tick
        expected_entry_seconds = lifecycle.cue.cue_seconds
    elif lifecycle.placement_hint is not None:
        target_score_tick = lifecycle.placement_hint.target_score_tick
    expected_entry_beat = None
    if target_score_tick is not None:
        projection = score_projection(piece_id, movement)
        entry = rehearsal_entry_for_measure(
            projection,
            target_score_tick,
            reference_path,
        )
        expected_entry_beat = (
            projection.source_tick_at_score_tick(entry.entry_position.score_tick)
            / store.CANONICAL_PPQ
        )
        LOGGER.info(
            "Resolved take entry take_id=%s selected_score_tick=%d "
            "entry_measure=%s entry_beat=%.3f entry_source=%.6fs pitch=%s",
            take_id,
            target_score_tick,
            entry.entry_position.measure_label,
            entry.entry_position.beat_in_measure,
            entry.entry_source_seconds,
            entry.entry_pitch,
        )

    try:
        aligned = align_take(
            take_id,
            take_midi_path,
            reference_path,
            expected_entry_beat=expected_entry_beat,
            expected_entry_seconds=expected_entry_seconds,
        )
    except ValueError:
        store.transition_take_analysis(piece_id, movement, take_id, AnalysisState.UNALIGNABLE)
        updated = store.get_take(piece_id, movement, take_id)
        _publish_alignment_done(updated)
        _materialize(piece_id, movement, take_id)
        return updated

    store.write_aligned_result(piece_id, movement, take_id, aligned)

    if aligned.ambiguous:
        status = store.AMBIGUOUS
    elif aligned.match_rate >= MATCH_RATE_GATE:
        status = store.ALIGNED
    else:
        status = store.UNALIGNABLE

    # The take may have been discarded by the user while this background
    # alignment was running -- don't clobber that (design doc §3.5: discard
    # is a real, user-initiated action; a background worker's job shouldn't
    # undo it). Same guard as _perform_resolve's resolve-path equivalent.
    target = {
        store.ALIGNED: AnalysisState.ALIGNED,
        store.AMBIGUOUS: AnalysisState.AMBIGUOUS,
        store.UNALIGNABLE: AnalysisState.UNALIGNABLE,
    }[status]
    store.transition_take_analysis(
        piece_id,
        movement,
        take_id,
        target,
        alignment_artifact="aligned.v2.json"
        if target in {AnalysisState.ALIGNED, AnalysisState.AMBIGUOUS}
        else None,
    )
    updated = store.get_take(piece_id, movement, take_id)
    _publish_alignment_done(updated)
    _materialize(piece_id, movement, take_id)
    return updated


def _ensure_analysis_running(piece_id: str, movement: int, take_id: str) -> None:
    current = store.get_take_v2(piece_id, movement, take_id)
    if current.analysis_state == AnalysisState.RUNNING:
        return
    if current.analysis_state != AnalysisState.QUEUED:
        store.transition_take_analysis(piece_id, movement, take_id, AnalysisState.QUEUED)
    store.transition_take_analysis(piece_id, movement, take_id, AnalysisState.RUNNING)


def _materialize(piece_id: str, movement: int, take_id: str) -> None:
    from aimusic.takes.materializer import materialization_worker

    materialization_worker.enqueue(piece_id, movement, take_id)


def _validate_resolve_candidate(
    piece_id: str,
    movement: int,
    take_id: str,
    candidate: int | str,
    *,
    require_ambiguous: bool = True,
) -> tuple[tuple[LocalizationCandidate, ...], int]:
    """Fast, synchronous precondition checks for resolving an ambiguous take.

    Raises `ValueError` for every reason a resolve can't proceed. Shared by
    the fully-synchronous `resolve_take` and `AlignmentWorker.enqueue_resolve`
    -- the latter must run this *before* moving the take off `ambiguous`,
    since that status is exactly what's being checked here.

    `store.get_aligned_result` already validates `aligned.json` against the
    `AlignedResult` schema (design doc §2.1) -- a malformed 'candidates'
    (wrong shape entirely, not just missing `start_position`) raises
    `pydantic.ValidationError` there, loudly, before this function is even
    entered. The only structural check left here is the candidate_index
    range and the `start_position` version-skew check below.
    """

    take_v2 = store.get_take_v2(piece_id, movement, take_id)
    if require_ambiguous and take_v2.analysis_state != AnalysisState.AMBIGUOUS:
        raise ValueError(f"take {take_id} is not ambiguous (state={take_v2.analysis_state})")

    existing = store.get_aligned_result(piece_id, movement, take_id)
    if existing is None:
        raise ValueError(f"take {take_id} has no aligned result to resolve")
    candidates = existing.candidates
    if isinstance(candidate, str):
        v2 = store.get_aligned_result_v2(piece_id, movement, take_id)
        if v2 is None:
            raise ValueError(f"take {take_id} has no v2 aligned result")
        index = next(
            (i for i, item in enumerate(v2.candidates) if item.candidate_id == candidate), None
        )
        if index is None:
            raise ValueError(f"candidate_id {candidate!r} is missing or stale")
        candidate_index = index
    else:
        candidate_index = candidate
        if not (0 <= candidate_index < len(candidates)):
            raise ValueError(
                f"candidate_index {candidate_index} out of range "
                f"(take {take_id} has {len(candidates)} candidates)"
            )
    chosen_candidate = candidates[candidate_index]
    if chosen_candidate.start_position is None:
        # Takes aligned before this field existed only persisted
        # {start_beat, score} -- there's no reference index to re-align at.
        raise ValueError(
            f"take {take_id}'s candidate {candidate_index} has no 'start_position' "
            "(aligned with an older aligner version); re-record or re-align this take"
        )
    return candidates, chosen_candidate.start_position


def _perform_resolve(
    piece_id: str,
    movement: int,
    take_id: str,
    chosen_start: int,
    candidates: tuple[LocalizationCandidate, ...],
) -> store.Take:
    """The actual re-alignment work for a resolve, once a candidate's
    reference position has been chosen and validated.

    Re-runs stage 2/3 (fine alignment, quality gate, cell-sample resampling)
    at the candidate's reference position rather than merely relabeling
    `score_start_beat` -- the original `match_rate`/`timing_map`/
    `cell_samples` were computed against a *different* candidate's window
    and don't carry over. The take is never re-flagged `ambiguous`: choosing
    a candidate is the resolution, so the outcome is `aligned` or
    `unalignable` per the same `MATCH_RATE_GATE` every other take is held to.
    """

    _ensure_analysis_running(piece_id, movement, take_id)
    take = store.get_take(piece_id, movement, take_id)
    take_midi_path = paths.take_dir(piece_id, movement, take_id, create=False) / take.midi_path
    reference_path = resolve_reference_midi_path(piece_id, movement)
    reference = load_canonical_note_sequence(reference_path, track_name_contains=("PIANO SOLO",))
    if not reference:
        # load_canonical_note_sequence already raises if no track matches the
        # marker; this covers a matched track with zero note-on events, which
        # _align_at_position's fallback (reference[clamped]) can't handle.
        raise ValueError(f"Reference MIDI for {piece_id} movement {movement} has no notes")
    take_notes = load_canonical_note_sequence(take_midi_path)
    if not take_notes:
        store.transition_take_analysis(piece_id, movement, take_id, AnalysisState.UNALIGNABLE)
        updated = store.get_take(piece_id, movement, take_id)
        _publish_alignment_done(updated)
        _materialize(piece_id, movement, take_id)
        return updated

    aligned = _align_at_position(
        take_id,
        take_midi_path,
        reference,
        take_notes,
        chosen_start,
        ambiguous=False,
        candidates=tuple(candidates),
    )
    store.write_aligned_result(piece_id, movement, take_id, aligned)

    # The take may have been discarded by the user while this background
    # re-alignment was running -- don't clobber that with a stale
    # aligned/unalignable status (design doc §3.5: discard is a real,
    # user-initiated action; the background worker's job shouldn't undo it).
    target = (
        AnalysisState.ALIGNED
        if aligned.match_rate >= MATCH_RATE_GATE
        else AnalysisState.UNALIGNABLE
    )
    store.transition_take_analysis(
        piece_id,
        movement,
        take_id,
        target,
        alignment_artifact="aligned.v2.json" if target == AnalysisState.ALIGNED else None,
    )
    updated = store.get_take(piece_id, movement, take_id)
    _publish_alignment_done(updated)
    _materialize(piece_id, movement, take_id)
    return updated


def resolve_take(piece_id: str, movement: int, take_id: str, candidate_index: int) -> store.Take:
    """Resolve an `ambiguous` take by re-aligning at a chosen localization
    candidate's position (design doc §3.2/§4.3: "It was here / It was
    there", `POST /api/takes/{id}/resolve`).

    Synchronous end-to-end: validates the candidate choice and performs the
    re-alignment inline. `AlignmentWorker.enqueue_resolve` is the
    non-blocking version the API route uses (design doc §2.7: alignment
    must never make Eric wait) -- this one is for direct/test use.
    """

    candidates, chosen_start = _validate_resolve_candidate(
        piece_id, movement, take_id, candidate_index
    )
    return _perform_resolve(piece_id, movement, take_id, chosen_start, candidates)


class AlignmentWorker:
    """Single-thread background queue for take alignment (design doc §2.7).

    Deliberately a separate thread pool from `LiveControl`'s hardware job so
    analysis never contends with the MIDI hardware lock.
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="take-align")

    def enqueue(self, piece_id: str, movement: int, take_id: str) -> None:
        current = store.get_take_v2(piece_id, movement, take_id)
        if current.analysis_state != AnalysisState.QUEUED:
            store.transition_take_analysis(piece_id, movement, take_id, AnalysisState.QUEUED)
        job = store.create_job(piece_id, movement, take_id, JobKind.ALIGN)
        self._executor.submit(self._run, piece_id, movement, take_id, job.job_id)

    def _run(self, piece_id: str, movement: int, take_id: str, job_id: str | None = None) -> None:
        try:
            _ensure_analysis_running(piece_id, movement, take_id)
            if job_id is not None:
                job = store.get_job(piece_id, movement, job_id)
                if job.state == JobState.SUCCEEDED:
                    return
                store.transition_stored_job(piece_id, movement, job_id, JobState.RUNNING)
            align_and_store(piece_id, movement, take_id)
            if job_id is not None:
                store.transition_stored_job(piece_id, movement, job_id, JobState.SUCCEEDED)
        except Exception as exc:
            logging.getLogger(__name__).exception(
                "Failed to align take %s for piece %s movement %d", take_id, piece_id, movement
            )
            journal_exception(
                "take:alignment_failed",
                exc,
                piece_id=piece_id,
                movement=movement,
                take_id=take_id,
                job_id=job_id,
            )
            # Same discard race as _run_resolve's exception handler: don't
            # clobber a DISCARDED status with a failed alignment's outcome.
            failure = LifecycleFailure(code="alignment_crash", message=str(exc), retryable=True)
            current = store.get_take_v2(piece_id, movement, take_id)
            if current.analysis_state == AnalysisState.RUNNING:
                store.transition_take_analysis(
                    piece_id, movement, take_id, AnalysisState.FAILED, failure=failure
                )
                _publish_alignment_done(store.get_take(piece_id, movement, take_id))
                _materialize(piece_id, movement, take_id)
            if (
                job_id is not None
                and store.get_job(piece_id, movement, job_id).state == JobState.RUNNING
            ):
                store.transition_stored_job(
                    piece_id, movement, job_id, JobState.FAILED, failure=failure
                )

    def enqueue_resolve(
        self, piece_id: str, movement: int, take_id: str, candidate: int | str
    ) -> store.Take:
        """Validate synchronously (so the API route can return 404/422
        immediately for bad input), then perform the actual re-alignment in
        the background -- resolving is just as CPU-bound as the original
        alignment (MIDI parsing, local alignment, cell-sample resampling),
        so it gets the same never-block-the-request-thread treatment
        (design doc §2.7).
        """

        candidates, chosen_start = _validate_resolve_candidate(
            piece_id, movement, take_id, candidate
        )
        candidate_id = (
            candidate
            if isinstance(candidate, str)
            else store.get_aligned_result_v2(piece_id, movement, take_id)
            .candidates[candidate]
            .candidate_id
        )
        store.transition_take_analysis(piece_id, movement, take_id, AnalysisState.QUEUED)
        job = store.create_job(
            piece_id, movement, take_id, JobKind.RESOLVE_ALIGNMENT, candidate_id=candidate_id
        )
        self._executor.submit(
            self._run_resolve, piece_id, movement, take_id, chosen_start, candidates, job.job_id
        )
        return store.get_take(piece_id, movement, take_id)

    def _run_resolve(
        self,
        piece_id: str,
        movement: int,
        take_id: str,
        chosen_start: int,
        candidates: tuple[LocalizationCandidate, ...],
        job_id: str | None = None,
    ) -> None:
        try:
            if job_id is not None:
                store.transition_stored_job(piece_id, movement, job_id, JobState.RUNNING)
            _perform_resolve(piece_id, movement, take_id, chosen_start, candidates)
            if job_id is not None:
                store.transition_stored_job(piece_id, movement, job_id, JobState.SUCCEEDED)
        except Exception as exc:
            logging.getLogger(__name__).exception(
                "Failed to resolve take %s for piece %s movement %d", take_id, piece_id, movement
            )
            journal_exception(
                "take:alignment_resolution_failed",
                exc,
                piece_id=piece_id,
                movement=movement,
                take_id=take_id,
                job_id=job_id,
            )
            # Same discard race _perform_resolve's own success path guards
            # against: don't let a failed background re-alignment clobber a
            # DISCARDED status the user set while it was running.
            failure = LifecycleFailure(code="resolve_crash", message=str(exc), retryable=True)
            current = store.get_take_v2(piece_id, movement, take_id)
            if current.analysis_state == AnalysisState.RUNNING:
                store.transition_take_analysis(
                    piece_id, movement, take_id, AnalysisState.FAILED, failure=failure
                )
                _publish_alignment_done(store.get_take(piece_id, movement, take_id))
                _materialize(piece_id, movement, take_id)
            if (
                job_id is not None
                and store.get_job(piece_id, movement, job_id).state == JobState.RUNNING
            ):
                store.transition_stored_job(
                    piece_id, movement, job_id, JobState.FAILED, failure=failure
                )

    def recover(self, piece_id: str, movement: int) -> int:
        """Requeue queued/stale-running jobs after process restart."""
        recovered = 0
        for job in store.list_jobs(piece_id, movement):
            if job.kind not in {JobKind.ALIGN, JobKind.RESOLVE_ALIGNMENT}:
                continue
            if job.state == JobState.RUNNING:
                failure = LifecycleFailure(
                    code="worker_restarted",
                    message="worker restarted while job was running",
                    retryable=True,
                )
                job = store.transition_stored_job(
                    piece_id, movement, job.job_id, JobState.FAILED, failure=failure
                )
                job = store.transition_stored_job(piece_id, movement, job.job_id, JobState.QUEUED)
            if job.state != JobState.QUEUED or job.take_id is None:
                continue
            if job.kind == JobKind.ALIGN:
                self._executor.submit(self._run, piece_id, movement, job.take_id, job.job_id)
            else:
                candidates, chosen = _validate_resolve_candidate(
                    piece_id,
                    movement,
                    job.take_id,
                    job.candidate_id or "",
                    require_ambiguous=False,
                )
                self._executor.submit(
                    self._run_resolve,
                    piece_id,
                    movement,
                    job.take_id,
                    chosen,
                    candidates,
                    job.job_id,
                )
            recovered += 1
        return recovered

    def recover_all(self) -> int:
        """Discover movement stores and recover every unfinished job."""

        root = paths.takes_root()
        recovered = 0
        for piece_dir in root.iterdir():
            if not piece_dir.is_dir():
                continue
            for movement_dir in piece_dir.iterdir():
                if not movement_dir.is_dir():
                    continue
                try:
                    movement = int(movement_dir.name)
                except ValueError:
                    continue
                recovered += self.recover(piece_dir.name, movement)
        return recovered


alignment_worker = AlignmentWorker()


__all__ = [
    "ChordNote",
    "NgramIndex",
    "VoteCandidate",
    "LocalizationResult",
    "AlignmentWorker",
    "alignment_worker",
    "load_canonical_note_sequence",
    "build_ngram_index",
    "localize",
    "align_within_window",
    "align_take",
    "align_and_store",
    "resolve_take",
    "resolve_reference_midi_path",
    "MATCH_RATE_GATE",
    "ALIGNER_NAME",
]
