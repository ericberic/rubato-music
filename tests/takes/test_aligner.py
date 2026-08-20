"""Tests for the production alignment pipeline (roadmap item 3).

Reuses the same real movement-2 solo reference and synthetic-partial-take
approach validated in tests/accompaniment/test_localization_spike.py
(roadmap item 1), but drives it through the production `aligner`/`store`
modules instead of the spike's standalone functions.
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path

import pytest
from mido import Message, MetaMessage, MidiFile, MidiTrack, bpm2tempo, second2tick
from pydantic import ValidationError

from aimusic.core import paths
from aimusic.takes import aligner, store
from aimusic.takes.models import TimingMapPoint

REPO_ROOT = Path(__file__).resolve().parents[2]
SOLO_REFERENCE_PATH = (
    REPO_ROOT / "data/scores/chopin_op11_movement_2/derived/solo_reference.mid"
)
DROP_RATE = 0.10
SEED = 20260709


from tests.oguri_guard import requires_oguri_derived

pytestmark = requires_oguri_derived


@pytest.fixture(autouse=True)
def isolated_data_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))


@pytest.fixture(scope="module")
def reference() -> tuple[aligner.ChordNote, ...]:
    return aligner.load_canonical_note_sequence(
        SOLO_REFERENCE_PATH, track_name_contains=("PIANO SOLO",)
    )


def _synthetic_take_notes(
    reference: tuple[aligner.ChordNote, ...],
    start_position: int,
    note_count: int,
    rng: random.Random,
) -> list[aligner.ChordNote]:
    end = min(start_position + note_count, len(reference))
    true_slice = reference[start_position:end]
    return [true_slice[0]] + [note for note in true_slice[1:] if rng.random() >= DROP_RATE]


def _write_take_midi(
    path: Path,
    notes: list[aligner.ChordNote],
    *,
    ticks_per_beat: int = 480,
    tempo_bpm: int = 120,
) -> None:
    midi = MidiFile(ticks_per_beat=ticks_per_beat)
    track = MidiTrack()
    midi.tracks.append(track)
    tempo = bpm2tempo(tempo_bpm)
    track.append(MetaMessage("set_tempo", tempo=tempo, time=0))

    start_seconds = notes[0].time_seconds
    last_time = 0.0
    for note in notes:
        elapsed = note.time_seconds - start_seconds
        delta = max(0.0, elapsed - last_time)
        delta_ticks = int(round(second2tick(delta, ticks_per_beat, tempo)))
        track.append(Message("note_on", note=note.pitch, velocity=64, time=delta_ticks))
        track.append(Message("note_off", note=note.pitch, velocity=0, time=ticks_per_beat // 8))
        last_time = elapsed

    path.parent.mkdir(parents=True, exist_ok=True)
    midi.save(path)


def test_synthetic_take_localizes_and_aligns_correctly(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path
) -> None:
    rng = random.Random(SEED)
    start_position = 500
    notes = _synthetic_take_notes(reference, start_position, 30, rng)

    take_midi_path = tmp_path / "take.mid"
    _write_take_midi(take_midi_path, notes)

    aligned = aligner.align_take("test-take", take_midi_path, SOLO_REFERENCE_PATH)

    assert not aligned.ambiguous
    assert aligned.match_rate >= 0.75
    assert aligned.matched_notes >= int(0.75 * len(notes))

    expected_start_beat = reference[start_position].beat
    assert abs(aligned.score_start_beat - expected_start_beat) <= 5.0
    assert aligned.score_end_beat > aligned.score_start_beat
    assert len(aligned.timing_map) == aligned.matched_notes
    assert all(isinstance(point, TimingMapPoint) for point in aligned.timing_map)


def test_selected_entry_anchors_after_captured_orchestra_lead_in(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path
) -> None:
    """Cue notes before the pianist's entry cannot shift the selected passage."""

    start_position = 500
    lead_in = [
        aligner.ChordNote(i, i * 0.1, i * 0.1, pitch, i)
        for i, pitch in enumerate((36, 43, 48, 52, 55, 60, 64, 67))
    ]
    reference_start_seconds = reference[start_position].time_seconds
    passage = [
        aligner.ChordNote(
            len(lead_in) + i,
            8.0 + (note.time_seconds - reference_start_seconds),
            8.0 + (note.time_seconds - reference_start_seconds),
            note.pitch,
            len(lead_in) + i,
        )
        for i, note in enumerate(reference[start_position : start_position + 45])
    ]
    take_path = tmp_path / "cued-take.mid"
    _write_take_midi(take_path, lead_in + passage)

    aligned = aligner.align_take(
        "cued-take",
        take_path,
        SOLO_REFERENCE_PATH,
        expected_entry_beat=reference[start_position].beat,
        expected_entry_seconds=8.0,
    )

    assert aligned.ambiguous is False
    assert aligned.match_rate >= 0.8
    assert aligned.score_start_beat == pytest.approx(reference[start_position].beat)
    assert all(
        candidate.start_position is None or candidate.start_position >= 0
        for candidate in aligned.candidates
    )


def test_repeated_material_is_flagged_ambiguous(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path
) -> None:
    # Same genuinely-repeated coda passage used in the localization spike.
    repeat_a, repeat_length = 2605, 23
    assert reference[repeat_a].pitch == reference[2719].pitch  # fixture still holds

    notes = list(reference[repeat_a : repeat_a + repeat_length])
    take_midi_path = tmp_path / "take.mid"
    _write_take_midi(take_midi_path, notes)

    aligned = aligner.align_take("ambiguous-take", take_midi_path, SOLO_REFERENCE_PATH)

    assert aligned.ambiguous


def test_take_with_no_notes_raises(tmp_path: Path) -> None:
    empty_midi_path = tmp_path / "empty.mid"
    midi = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    track.append(MetaMessage("set_tempo", tempo=bpm2tempo(120), time=0))
    midi.tracks.append(track)
    midi.save(empty_midi_path)

    with pytest.raises(ValueError, match="take has no note events"):
        aligner.align_take(
            "empty-take",
            empty_midi_path,
            SOLO_REFERENCE_PATH,
            expected_entry_beat=242.3125,
            expected_entry_seconds=8.0,
        )


def test_reference_with_no_notes_raises_before_localization(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path
) -> None:
    take_midi_path = tmp_path / "take.mid"
    _write_take_midi(take_midi_path, list(reference[500:530]))

    empty_reference_path = tmp_path / "empty_reference.mid"
    midi = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    track.append(MetaMessage("track_name", name="PIANO SOLO", time=0))
    track.append(MetaMessage("set_tempo", tempo=bpm2tempo(120), time=0))
    midi.tracks.append(track)
    midi.save(empty_reference_path)

    with pytest.raises(ValueError, match="reference has no note events"):
        aligner.align_take(
            "empty-reference",
            take_midi_path,
            empty_reference_path,
            expected_entry_beat=242.3125,
            expected_entry_seconds=8.0,
        )


def test_align_and_store_updates_take_status_and_writes_aligned_json(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path
) -> None:
    rng = random.Random(SEED + 1)
    start_position = 800
    notes = _synthetic_take_notes(reference, start_position, 30, rng)

    take_midi_path = tmp_path / "take.mid"
    _write_take_midi(take_midi_path, notes)

    take = store.save_take("chopin_op11", 2, take_midi_path)
    updated = aligner.align_and_store("chopin_op11", 2, take.take_id)

    assert updated.status in {store.ALIGNED, store.AMBIGUOUS, store.UNALIGNABLE}
    result = store.get_aligned_result("chopin_op11", 2, take.take_id)
    assert result is not None
    assert result.take_id == take.take_id


def test_align_and_store_does_not_clobber_a_discard_race(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path
) -> None:
    """Same discard race as resolve's _perform_resolve: if the take is
    discarded while align_and_store's background alignment is still
    running, the finishing job must not overwrite that with a stale
    aligned/ambiguous/unalignable status (design doc §3.5).
    """

    rng = random.Random(SEED + 5)
    notes = _synthetic_take_notes(reference, 500, 30, rng)
    take_midi_path = tmp_path / "take.mid"
    _write_take_midi(take_midi_path, notes)

    take = store.save_take("chopin_op11", 2, take_midi_path)
    store.discard_take("chopin_op11", 2, take.take_id)

    result = aligner.align_and_store("chopin_op11", 2, take.take_id)

    assert result.status == store.DISCARDED
    assert store.get_take("chopin_op11", 2, take.take_id).status == store.DISCARDED


def test_run_exception_handler_does_not_clobber_a_discard_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AlignmentWorker._run's exception handler must not clobber a
    DISCARDED status either, mirroring _run_resolve's equivalent guard.
    """

    take_midi_path = tmp_path / "take.mid"
    midi = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    track.append(Message("note_on", note=60, velocity=64, time=0))
    track.append(Message("note_off", note=60, velocity=0, time=240))
    midi.tracks.append(track)
    midi.save(take_midi_path)

    take = store.save_take("chopin_op11", 2, take_midi_path)
    store.discard_take("chopin_op11", 2, take.take_id)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated alignment crash")

    monkeypatch.setattr(aligner, "align_take", _boom)

    aligner.alignment_worker._run("chopin_op11", 2, take.take_id)

    assert store.get_take("chopin_op11", 2, take.take_id).status == store.DISCARDED


def test_resolve_take_picks_the_other_candidate(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path
) -> None:
    """The genuinely-repeated coda passage (design doc §2.4's ambiguity
    example) lands `ambiguous` with two candidates -- the original position
    (2605) and the real second occurrence (2719). Resolving to the second
    candidate must re-align there (design doc §3.2/§4.3: "It was here / It
    was there"), not just relabel score_start_beat: match_rate, timing_map,
    and cell_samples all need to reflect the chosen window.
    """

    repeat_a, repeat_length = 2605, 23
    notes = list(reference[repeat_a : repeat_a + repeat_length])
    take_midi_path = tmp_path / "take.mid"
    _write_take_midi(take_midi_path, notes)

    take = store.save_take("chopin_op11", 2, take_midi_path)
    first_pass = aligner.align_and_store("chopin_op11", 2, take.take_id)
    assert first_pass.status == store.AMBIGUOUS

    before = store.get_aligned_result("chopin_op11", 2, take.take_id)
    assert before is not None
    assert len(before.candidates) == 2
    other_index = next(
        i for i, c in enumerate(before.candidates) if c.start_beat != before.score_start_beat
    )

    resolved = aligner.resolve_take("chopin_op11", 2, take.take_id, other_index)

    assert resolved.status in {store.ALIGNED, store.UNALIGNABLE}
    after = store.get_aligned_result("chopin_op11", 2, take.take_id)
    assert after is not None
    assert after.ambiguous is False
    assert after.score_start_beat == before.candidates[other_index].start_beat
    # Re-aligned at the real second occurrence: should match just as well as
    # the original placement, not degrade to some stale/partial window.
    assert after.match_rate == pytest.approx(before.match_rate)
    assert after.cell_samples


def test_resolve_take_rejects_non_ambiguous_take(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path
) -> None:
    rng = random.Random(SEED + 3)
    notes = _synthetic_take_notes(reference, 600, 30, rng)
    take_midi_path = tmp_path / "take.mid"
    _write_take_midi(take_midi_path, notes)

    take = store.save_take("chopin_op11", 2, take_midi_path)
    updated = aligner.align_and_store("chopin_op11", 2, take.take_id)
    assert updated.status != store.AMBIGUOUS  # a unique 30-note passage should localize cleanly

    with pytest.raises(ValueError, match="not ambiguous"):
        aligner.resolve_take("chopin_op11", 2, take.take_id, 0)


def test_resolve_take_rejects_out_of_range_candidate_index(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path
) -> None:
    repeat_a, repeat_length = 2605, 23
    notes = list(reference[repeat_a : repeat_a + repeat_length])
    take_midi_path = tmp_path / "take.mid"
    _write_take_midi(take_midi_path, notes)

    take = store.save_take("chopin_op11", 2, take_midi_path)
    assert aligner.align_and_store("chopin_op11", 2, take.take_id).status == store.AMBIGUOUS

    with pytest.raises(ValueError, match="out of range"):
        aligner.resolve_take("chopin_op11", 2, take.take_id, 99)


def test_resolve_take_rejects_candidate_missing_start_position(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path
) -> None:
    """Backward compatibility: a take aligned before `start_position` was
    added to `candidates` only has `{start_beat, score}` on disk. Resolving
    it must fail with a clear ValueError (-> 422), not a raw KeyError
    (-> unhandled 500).
    """

    repeat_a, repeat_length = 2605, 23
    notes = list(reference[repeat_a : repeat_a + repeat_length])
    take_midi_path = tmp_path / "take.mid"
    _write_take_midi(take_midi_path, notes)

    take = store.save_take("chopin_op11", 2, take_midi_path)
    assert aligner.align_and_store("chopin_op11", 2, take.take_id).status == store.AMBIGUOUS

    # Simulate a pre-migration aligned.json: a take aligned before
    # `start_position` existed only persisted {start_beat, score} --
    # `LocalizationCandidate.start_position` defaults to `None` for that
    # case (design doc §2.1's one intentional widening), so this writes a
    # perfectly valid `AlignedResult` rather than hand-corrupted JSON.
    stored = store.get_aligned_result("chopin_op11", 2, take.take_id)
    assert stored is not None
    old_style_candidates = tuple(
        c.model_copy(update={"start_position": None}) for c in stored.candidates
    )
    updated_stored = stored.model_copy(update={"candidates": old_style_candidates})
    store.write_aligned_result("chopin_op11", 2, take.take_id, updated_stored)

    with pytest.raises(ValueError, match="start_position"):
        aligner.resolve_take("chopin_op11", 2, take.take_id, 0)


def test_resolve_take_rejects_malformed_candidates_on_disk(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path
) -> None:
    """A corrupted or hand-edited aligned.json could store `candidates` as
    something other than a list (e.g. a dict from a bad merge). The typed
    read path (`AlignedResult.model_validate_json`, design doc §2.1) must
    fail loudly with `pydantic.ValidationError` -- not the old stringly
    `ValueError` from a hand-rolled isinstance probe, and not a raw
    TypeError from len()/indexing on unvalidated data.
    """

    repeat_a, repeat_length = 2605, 23
    notes = list(reference[repeat_a : repeat_a + repeat_length])
    take_midi_path = tmp_path / "take.mid"
    _write_take_midi(take_midi_path, notes)

    take = store.save_take("chopin_op11", 2, take_midi_path)
    assert aligner.align_and_store("chopin_op11", 2, take.take_id).status == store.AMBIGUOUS

    # A schema violation this structural (candidates as a dict, not a list)
    # can only happen via hand-corrupted JSON on disk -- the typed
    # constructor/model_copy path can't produce it. Write the corrupted
    # text directly, bypassing `store.write_aligned_result`.
    aligned_path = paths.take_dir("chopin_op11", 2, take.take_id, create=False) / "aligned.json"
    raw = json.loads(aligned_path.read_text(encoding="utf-8"))
    raw["candidates"] = {"not": "a list"}
    aligned_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValidationError):
        aligner.resolve_take("chopin_op11", 2, take.take_id, 0)


def test_resolve_take_raises_when_reference_has_no_notes(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reference MIDI whose matched track has zero note-on events would
    otherwise crash `_align_at_position`'s empty-match fallback with an
    IndexError (`reference[clamped]` on an empty tuple). `load_canonical_
    note_sequence` only raises for a *missing* track, not an empty one, so
    this needs its own guard.
    """

    repeat_a, repeat_length = 2605, 23
    notes = list(reference[repeat_a : repeat_a + repeat_length])
    take_midi_path = tmp_path / "take.mid"
    _write_take_midi(take_midi_path, notes)

    take = store.save_take("chopin_op11", 2, take_midi_path)
    assert aligner.align_and_store("chopin_op11", 2, take.take_id).status == store.AMBIGUOUS

    empty_reference_path = tmp_path / "empty_reference.mid"
    empty_midi = MidiFile(ticks_per_beat=480)
    empty_track = MidiTrack()
    empty_track.append(MetaMessage("track_name", name="PIANO SOLO", time=0))
    empty_track.append(MetaMessage("set_tempo", tempo=bpm2tempo(120), time=0))
    empty_midi.tracks.append(empty_track)
    empty_midi.save(empty_reference_path)

    monkeypatch.setattr(
        aligner, "resolve_reference_midi_path", lambda piece_id, movement: empty_reference_path
    )

    with pytest.raises(ValueError, match="no notes"):
        aligner.resolve_take("chopin_op11", 2, take.take_id, 0)


def test_perform_resolve_does_not_clobber_a_discard_race(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path
) -> None:
    """If the take is discarded (e.g. by the user) while the background
    re-alignment is still running, the finishing job must not overwrite
    that with a stale aligned/unalignable status -- discard is a real,
    user-initiated action (design doc §3.5), not something a background
    worker should undo.
    """

    repeat_a, repeat_length = 2605, 23
    notes = list(reference[repeat_a : repeat_a + repeat_length])
    take_midi_path = tmp_path / "take.mid"
    _write_take_midi(take_midi_path, notes)

    take = store.save_take("chopin_op11", 2, take_midi_path)
    assert aligner.align_and_store("chopin_op11", 2, take.take_id).status == store.AMBIGUOUS

    # Simulate the worker's own sequencing: validate (as enqueue_resolve
    # does synchronously) before the take is discarded out from under it.
    candidates, chosen_start = aligner._validate_resolve_candidate(
        "chopin_op11", 2, take.take_id, 0
    )
    store.discard_take("chopin_op11", 2, take.take_id)

    result = aligner._perform_resolve("chopin_op11", 2, take.take_id, chosen_start, candidates)

    assert result.status == store.DISCARDED
    assert store.get_take("chopin_op11", 2, take.take_id).status == store.DISCARDED


def test_alignment_worker_enqueue_processes_take_asynchronously(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path
) -> None:
    rng = random.Random(SEED + 2)
    start_position = 900
    notes = _synthetic_take_notes(reference, start_position, 30, rng)

    take_midi_path = tmp_path / "take.mid"
    _write_take_midi(take_midi_path, notes)

    take = store.save_take("chopin_op11", 2, take_midi_path)
    aligner.alignment_worker.enqueue("chopin_op11", 2, take.take_id)

    deadline = time.monotonic() + 5.0
    final = store.get_take("chopin_op11", 2, take.take_id)
    while final.status == store.ALIGNING and time.monotonic() < deadline:
        time.sleep(0.05)
        final = store.get_take("chopin_op11", 2, take.take_id)

    assert final.status in {store.ALIGNED, store.AMBIGUOUS, store.UNALIGNABLE}


def test_alignment_worker_enqueue_resolve_runs_in_background(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path
) -> None:
    """`enqueue_resolve` must not block the caller: it returns immediately
    with status `aligning` (design doc §2.7 -- resolving is exactly as
    CPU-bound as the original alignment) and the background thread finishes
    the real re-alignment.
    """

    repeat_a, repeat_length = 2605, 23
    notes = list(reference[repeat_a : repeat_a + repeat_length])
    take_midi_path = tmp_path / "take.mid"
    _write_take_midi(take_midi_path, notes)

    take = store.save_take("chopin_op11", 2, take_midi_path)
    assert aligner.align_and_store("chopin_op11", 2, take.take_id).status == store.AMBIGUOUS

    immediate = aligner.alignment_worker.enqueue_resolve("chopin_op11", 2, take.take_id, 1)
    assert immediate.status == store.ALIGNING

    deadline = time.monotonic() + 5.0
    final = store.get_take("chopin_op11", 2, take.take_id)
    while final.status == store.ALIGNING and time.monotonic() < deadline:
        time.sleep(0.05)
        final = store.get_take("chopin_op11", 2, take.take_id)

    assert final.status in {store.ALIGNED, store.UNALIGNABLE}
    result = store.get_aligned_result("chopin_op11", 2, take.take_id)
    assert result is not None
    assert result.ambiguous is False


def test_alignment_worker_enqueue_resolve_validates_before_backgrounding(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path
) -> None:
    """An invalid resolve request must fail synchronously (so the API route
    can return 422 immediately), not after silently flipping the take to
    `aligning` first.
    """

    rng = random.Random(SEED + 4)
    notes = _synthetic_take_notes(reference, 700, 30, rng)
    take_midi_path = tmp_path / "take.mid"
    _write_take_midi(take_midi_path, notes)

    take = store.save_take("chopin_op11", 2, take_midi_path)
    status_before = aligner.align_and_store("chopin_op11", 2, take.take_id).status
    assert status_before != store.AMBIGUOUS

    with pytest.raises(ValueError, match="not ambiguous"):
        aligner.alignment_worker.enqueue_resolve("chopin_op11", 2, take.take_id, 0)

    # Status must be unchanged -- the failed validation shouldn't have
    # touched it.
    assert store.get_take("chopin_op11", 2, take.take_id).status == status_before


def test_run_resolve_exception_handler_does_not_clobber_a_discard_race(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same discard race `_perform_resolve`'s success path guards
    against also applies to `AlignmentWorker._run_resolve`'s exception
    handler: a *failed* background re-alignment must not clobber a
    DISCARDED status either.
    """

    repeat_a, repeat_length = 2605, 23
    notes = list(reference[repeat_a : repeat_a + repeat_length])
    take_midi_path = tmp_path / "take.mid"
    _write_take_midi(take_midi_path, notes)

    take = store.save_take("chopin_op11", 2, take_midi_path)
    assert aligner.align_and_store("chopin_op11", 2, take.take_id).status == store.AMBIGUOUS

    candidates, chosen_start = aligner._validate_resolve_candidate(
        "chopin_op11", 2, take.take_id, 0
    )

    # Force _perform_resolve to raise (empty reference sequence), same
    # trick as test_resolve_take_raises_when_reference_has_no_notes.
    empty_reference_path = tmp_path / "empty_reference.mid"
    empty_midi = MidiFile(ticks_per_beat=480)
    empty_track = MidiTrack()
    empty_track.append(MetaMessage("track_name", name="PIANO SOLO", time=0))
    empty_track.append(MetaMessage("set_tempo", tempo=bpm2tempo(120), time=0))
    empty_midi.tracks.append(empty_track)
    empty_midi.save(empty_reference_path)
    monkeypatch.setattr(
        aligner, "resolve_reference_midi_path", lambda piece_id, movement: empty_reference_path
    )

    store.discard_take("chopin_op11", 2, take.take_id)

    # Call the worker's exception-handling entry point directly (no need
    # for real threading -- it's synchronous once invoked).
    aligner.alignment_worker._run_resolve("chopin_op11", 2, take.take_id, chosen_start, candidates)

    assert store.get_take("chopin_op11", 2, take.take_id).status == store.DISCARDED
