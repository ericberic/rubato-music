"""De-risking spike for design/REHEARSAL_TAKE_COVERAGE_DESIGN.md roadmap item 1.

Proof-of-concept only, not production code. Validates the proposed two-stage
alignment approach (pitch n-gram seeding for localization, then the existing
local sequence aligner for the fine warp) against synthetic partial takes cut
from the real movement-2 solo reference MIDI.

No real recorded take (`data/processed/movement2_take/`) is available in this
checkout -- it is DVC-managed and not pulled here -- so this spike relies
entirely on synthetic partial takes: contiguous slices of the reference with
~10% of notes dropped, at a range of lengths and starting positions.

"Beat" here means MIDI tick position divided by ticks-per-beat on the solo
track's own tick grid -- a reasonable stand-in for notated beat position for
a de-risking spike. It is not the beat-grid-from-MusicXML the coverage index
will eventually need (roadmap item 5); that gap is orthogonal to what this
spike is checking (can we *localize and align* a partial take at all).

Run directly for a printed report:
    PYTHONPATH=src .venv/bin/python -m pytest tests/accompaniment/test_localization_spike.py -s
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass
from pathlib import Path

import mido
import pytest

from aimusic.accompaniment.offline_alignment import MidiNoteEvent, align_note_events

REPO_ROOT = Path(__file__).resolve().parents[2]
SOLO_REFERENCE_PATH = (
    REPO_ROOT / "data/scores/chopin_op11_movement_2/derived/solo_reference.mid"
)
SOLO_TRACK_NAME = "PIANO SOLO"

CHORD_GROUP_BEAT_EPS = 0.02  # notes within this many beats are one chord
NGRAM_LENGTHS = (4, 5, 6)  # design doc §2.4
DIAGONAL_BIN = 3  # positions; absorbs drift from dropped notes
AMBIGUITY_MARGIN = 0.15  # design doc §2.4: top-2 within 15% -> ambiguous

TAKE_LENGTHS = (20, 30, 50, 80)  # note counts
TRIALS_PER_LENGTH = 25
DROP_RATE = 0.10
ALIGNMENT_WINDOW_SLACK = 15  # notes; stage-2 window half-width around the seed
# "Correct" localization means landing inside the window stage 2 will search,
# not pinpointing the exact note -- that refinement is the fine aligner's job
# (§2.4 stage 2). Dropped notes make the diagonal-vote estimate drift by up
# to roughly (note_count * DROP_RATE) positions; empirically that stays under
# 15 even at 80-note takes, so this tolerance is set equal to the stage-2
# window slack rather than to the drift bound itself.
POSITION_TOLERANCE = ALIGNMENT_WINDOW_SLACK
SEED = 20260709


# ---------------------------------------------------------------------------
# Reference loading and canonicalization
# ---------------------------------------------------------------------------


from tests.oguri_guard import requires_oguri_derived

pytestmark = requires_oguri_derived


@dataclass(frozen=True)
class ChordNote:
    """One note of the flattened, chord-grouped canonical reference sequence."""

    position: int
    beat: float
    time_seconds: float
    pitch: int
    chord_id: int


def load_canonical_reference(path: Path = SOLO_REFERENCE_PATH) -> tuple[ChordNote, ...]:
    """Parse the solo track and flatten it to one canonical pitch sequence.

    Chords (near-simultaneous note-ons) are grouped and re-sorted by pitch,
    per design doc §2.4, "ordered by pitch within a chord to defeat
    roll-order noise" -- raw MIDI chord order is roll/arpeggiation order, not
    a stable pitch order, and n-grams need a canonical order to match on.
    """

    midi = mido.MidiFile(path)
    track = next(
        t
        for t in midi.tracks
        if any(m.type == "track_name" and m.name == SOLO_TRACK_NAME for m in t)
    )
    tempo = 500_000
    elapsed_ticks = 0
    elapsed_seconds = 0.0
    raw: list[tuple[float, float, int]] = []  # (beat, seconds, pitch)
    for message in track:
        elapsed_seconds += mido.tick2second(message.time, midi.ticks_per_beat, tempo)
        elapsed_ticks += message.time
        if message.type == "set_tempo":
            tempo = message.tempo
        if message.type == "note_on" and message.velocity > 0:
            raw.append((elapsed_ticks / midi.ticks_per_beat, elapsed_seconds, message.note))

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


# ---------------------------------------------------------------------------
# Stage 1: n-gram seeding / localization
# ---------------------------------------------------------------------------

NgramIndex = dict[tuple[int, ...], list[int]]


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
class LocalizationResult:
    candidates: tuple[tuple[int, int], ...]  # (predicted_start_position, vote_count), desc
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
    candidates = _merge_nearby_candidates(raw_candidates)[:5]
    top_votes = candidates[0][1]
    ambiguous = len(candidates) > 1 and candidates[1][1] >= top_votes * (1 - AMBIGUITY_MARGIN)
    return LocalizationResult(candidates, candidates[0][0], ambiguous)


def _merge_nearby_candidates(
    ranked_candidates: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Collapse candidate positions that are the same score location split
    across adjacent diagonal buckets by quantization noise.

    Two candidates within ALIGNMENT_WINDOW_SLACK of each other would be
    captured by the same stage-2 alignment window anyway, so they are not a
    meaningful second location for the ambiguity check -- only a candidate
    outside that radius (like the genuinely repeated coda, ~114 positions
    from its twin) represents a real second placement.
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


# ---------------------------------------------------------------------------
# Synthetic partial takes
# ---------------------------------------------------------------------------


def make_synthetic_take(
    reference: tuple[ChordNote, ...],
    start_position: int,
    note_count: int,
    drop_rate: float,
    rng: random.Random,
) -> list[ChordNote]:
    """A contiguous slice of the reference with ~drop_rate notes removed.

    The first note is never dropped -- a take is defined by when playing
    starts, so position 0 of the take is by construction the true start.
    """

    end = min(start_position + note_count, len(reference))
    true_slice = reference[start_position:end]
    kept = [true_slice[0]] + [
        note for note in true_slice[1:] if rng.random() >= drop_rate
    ]
    return kept


# ---------------------------------------------------------------------------
# Stage 2: fine alignment (existing aligner) within the localized window
# ---------------------------------------------------------------------------


def align_within_window(
    reference: tuple[ChordNote, ...],
    take_notes: list[ChordNote],
    predicted_start: int,
    *,
    slack: int = ALIGNMENT_WINDOW_SLACK,
) -> tuple[float, int]:
    """Run the existing local-alignment aligner in a window around the seed.

    Returns (match_rate, predicted_end_position) where match_rate is
    pitch-matched pairs over take note count.
    """

    window_start = max(0, predicted_start - slack)
    window_end = max(0, min(len(reference), predicted_start + len(take_notes) + slack))
    window = reference[window_start:window_end]

    reference_events = tuple(
        MidiNoteEvent(
            index=note.position,
            time_seconds=note.time_seconds,
            pitch=note.pitch,
            velocity=64,
            channel=0,
            track_index=0,
        )
        for note in window
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

    result = align_note_events(reference_events, take_events)
    match_rate = result.summary.pitch_match_count / len(take_notes) if take_notes else 0.0
    matched_ref_positions = [pair.reference_index for pair in result.matched_pairs]
    predicted_end = max(matched_ref_positions) if matched_ref_positions else predicted_start
    return match_rate, predicted_end


# ---------------------------------------------------------------------------
# Trial harness
# ---------------------------------------------------------------------------


@dataclass
class TrialResult:
    length: int
    start_position: int
    predicted_start: int | None
    ambiguous: bool
    localized_correctly: bool
    match_rate: float | None
    start_error_beats: float | None


def run_localization_trials(
    reference: tuple[ChordNote, ...],
    index: NgramIndex,
    rng: random.Random,
) -> list[TrialResult]:
    results: list[TrialResult] = []
    max_start = len(reference) - max(TAKE_LENGTHS) - 1
    for length in TAKE_LENGTHS:
        for _ in range(TRIALS_PER_LENGTH):
            start_position = rng.randint(0, max_start)
            take_notes = make_synthetic_take(reference, start_position, length, DROP_RATE, rng)
            take_pitches = [note.pitch for note in take_notes]

            loc = localize(take_pitches, index)
            correct = (
                loc.chosen_start is not None
                and abs(loc.chosen_start - start_position) <= POSITION_TOLERANCE
            )

            match_rate = None
            start_error_beats = None
            if loc.chosen_start is not None:
                match_rate, _ = align_within_window(reference, take_notes, loc.chosen_start)
                predicted_beat = reference[max(0, min(loc.chosen_start, len(reference) - 1))].beat
                start_error_beats = abs(predicted_beat - reference[start_position].beat)

            results.append(
                TrialResult(
                    length=length,
                    start_position=start_position,
                    predicted_start=loc.chosen_start,
                    ambiguous=loc.ambiguous,
                    localized_correctly=correct,
                    match_rate=match_rate,
                    start_error_beats=start_error_beats,
                )
            )
    return results


def _print_report(results: list[TrialResult]) -> None:
    print("\n--- Localization spike report ---")
    print(f"{'len':>4}  {'n':>3}  {'unique+correct':>14}  {'ambiguous':>9}  "
          f"{'median match_rate':>18}  {'median |start err| (beats)':>27}")
    for length in TAKE_LENGTHS:
        subset = [r for r in results if r.length == length]
        unique_correct = sum(r.localized_correctly and not r.ambiguous for r in subset)
        ambiguous = sum(r.ambiguous for r in subset)
        match_rates = [r.match_rate for r in subset if r.match_rate is not None]
        errors = [r.start_error_beats for r in subset if r.start_error_beats is not None]
        print(
            f"{length:>4}  {len(subset):>3}  {unique_correct:>14}  {ambiguous:>9}  "
            f"{statistics.median(match_rates) if match_rates else float('nan'):>18.3f}  "
            f"{statistics.median(errors) if errors else float('nan'):>27.3f}"
        )
    overall_at_least_20 = [r for r in results if r.length >= 20]
    rate = sum(r.localized_correctly and not r.ambiguous for r in overall_at_least_20) / len(
        overall_at_least_20
    )
    print(f"\noverall unique+correct rate (n>=20 notes): {rate:.1%}")
    print("--- end report ---\n")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def reference() -> tuple[ChordNote, ...]:
    return load_canonical_reference()


@pytest.fixture(scope="module")
def ngram_index(reference: tuple[ChordNote, ...]) -> NgramIndex:
    return build_ngram_index(reference)


def test_reference_loads_with_expected_shape(reference: tuple[ChordNote, ...]) -> None:
    # Sanity check on the fixture itself: 2831 solo note-ons over ~588 s.
    assert len(reference) > 2000
    assert reference[-1].beat > reference[0].beat


def test_localization_spike_meets_roadmap_threshold(
    reference: tuple[ChordNote, ...], ngram_index: NgramIndex
) -> None:
    """Roadmap item 1 done-when: >=90% of >=20-note synthetic takes localize
    uniquely and correctly."""

    rng = random.Random(SEED)
    results = run_localization_trials(reference, ngram_index, rng)
    _print_report(results)

    at_least_20 = [r for r in results if r.length >= 20]
    unique_correct_rate = sum(
        r.localized_correctly and not r.ambiguous for r in at_least_20
    ) / len(at_least_20)

    assert unique_correct_rate >= 0.90, (
        f"localization spike below roadmap threshold: {unique_correct_rate:.1%} of "
        f">=20-note synthetic takes localized uniquely and correctly (want >=90%)"
    )

    match_rates = [r.match_rate for r in at_least_20 if r.match_rate is not None]
    assert statistics.median(match_rates) >= 0.75, (
        "fine alignment within the localized window should clear the design "
        "doc's usable-span gate (match_rate >= 0.75, §2.4 stage 3)"
    )


def test_ambiguity_detector_flags_genuinely_repeated_material(
    reference: tuple[ChordNote, ...], ngram_index: NgramIndex
) -> None:
    """The coda restates an earlier phrase near-verbatim (canonical positions
    ~2605 and ~2719, a 23-note match) -- exactly the "concertos restate
    themes" risk §2.4 calls out. A take drawn from that shared material
    should come back ambiguous, with both true locations among the top
    candidates, rather than silently pinned to one.
    """

    repeat_a, repeat_b, repeat_length = 2605, 2719, 23
    assert reference[repeat_a].pitch == reference[repeat_b].pitch  # fixture still holds

    take_notes = list(reference[repeat_a : repeat_a + repeat_length])
    take_pitches = [note.pitch for note in take_notes]

    loc = localize(take_pitches, ngram_index)

    assert loc.ambiguous, "a take built entirely from repeated material should be ambiguous"
    candidate_starts = {start for start, _ in loc.candidates}
    assert any(abs(start - repeat_a) <= POSITION_TOLERANCE for start in candidate_starts)
    assert any(abs(start - repeat_b) <= POSITION_TOLERANCE for start in candidate_starts)


def test_ambiguity_detector_does_not_flag_unique_material(
    reference: tuple[ChordNote, ...], ngram_index: NgramIndex
) -> None:
    """Control case: a take from a passage with no far-away pitch-identical
    match should localize unambiguously."""

    rng = random.Random(SEED + 1)
    # Position 500 is far from the known repeat at 2605/2719 and long enough
    # (30 notes) that a spurious full-length duplicate elsewhere is very
    # unlikely in a single-movement solo part.
    start_position = 500
    take_notes = make_synthetic_take(reference, start_position, 30, DROP_RATE, rng)
    take_pitches = [note.pitch for note in take_notes]

    loc = localize(take_pitches, ngram_index)

    assert not loc.ambiguous
    assert loc.chosen_start is not None
    assert abs(loc.chosen_start - start_position) <= POSITION_TOLERANCE


if __name__ == "__main__":
    ref = load_canonical_reference()
    idx = build_ngram_index(ref)
    trial_results = run_localization_trials(ref, idx, random.Random(SEED))
    _print_report(trial_results)
