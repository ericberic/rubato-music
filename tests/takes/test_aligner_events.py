"""`take:alignment_done` emission (roadmap item 5, design doc §2.7/§3.1).

Tests the event payload/timing contract at the aligner layer, independent of
the WebSocket wire format (see tests/test_events_ws.py for that) and of the
background ThreadPoolExecutor's timing (see
tests/takes/test_aligner.py::test_alignment_worker_enqueue_processes_take_asynchronously
for that). `align_and_store`/`resolve_take`/`AlignmentWorker._run` are all
synchronous entry points, so calling them directly here keeps these
deterministic.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from mido import Message, MidiFile, MidiTrack

from aimusic.server.schemas import TakeAlignmentDone
from aimusic.takes import aligner, store

REPO_ROOT = Path(__file__).resolve().parents[2]
SOLO_REFERENCE_PATH = (
    REPO_ROOT / "data/scores/chopin_op11_movement_2/derived/solo_reference.mid"
)
SEED = 20260710


from tests.oguri_guard import requires_oguri_derived

pytestmark = requires_oguri_derived


@pytest.fixture(autouse=True)
def isolated_data_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))


class _FakeEvents:
    def __init__(self) -> None:
        self.published: list[TakeAlignmentDone] = []

    def publish(self, event: TakeAlignmentDone) -> None:
        self.published.append(event)


@pytest.fixture()
def fake_events(monkeypatch: pytest.MonkeyPatch) -> _FakeEvents:
    fake = _FakeEvents()
    monkeypatch.setattr(aligner, "events", fake)
    return fake


@pytest.fixture(scope="module")
def reference() -> tuple[aligner.ChordNote, ...]:
    return aligner.load_canonical_note_sequence(
        SOLO_REFERENCE_PATH, track_name_contains=("PIANO SOLO",)
    )


def _synthetic_take_notes(
    reference: tuple[aligner.ChordNote, ...], start: int, length: int, rng: random.Random
) -> list[aligner.ChordNote]:
    return list(reference[start : start + length])


def _write_take_midi(path: Path, notes: list[aligner.ChordNote]) -> None:
    midi = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    midi.tracks.append(track)
    start_seconds = notes[0].time_seconds
    last_ticks = 0
    for note in notes:
        elapsed_ticks = int(round((note.time_seconds - start_seconds) * 480 * 2))
        delta = max(0, elapsed_ticks - last_ticks)
        track.append(Message("note_on", note=note.pitch, velocity=64, time=delta))
        track.append(Message("note_off", note=note.pitch, velocity=0, time=60))
        last_ticks = elapsed_ticks
    path.parent.mkdir(parents=True, exist_ok=True)
    midi.save(path)


def test_align_and_store_publishes_alignment_done_on_success(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path, fake_events: _FakeEvents
) -> None:
    rng = random.Random(SEED)
    notes = _synthetic_take_notes(reference, 800, 30, rng)
    take_midi_path = tmp_path / "take.mid"
    _write_take_midi(take_midi_path, notes)

    take = store.save_take("chopin_op11", 2, take_midi_path)
    updated = aligner.align_and_store("chopin_op11", 2, take.take_id)

    assert len(fake_events.published) == 1
    event = fake_events.published[0]
    assert event.type == "take:alignment_done"
    assert event.take_id == take.take_id
    assert event.piece_id == "chopin_op11"
    assert event.movement == 2
    assert event.status == updated.status
    assert event.status != store.DISCARDED
    assert isinstance(event.score_start_beat, float)
    assert isinstance(event.score_end_beat, float)


def test_align_and_store_publishes_unalignable_when_localization_fails(
    tmp_path: Path, fake_events: _FakeEvents, monkeypatch: pytest.MonkeyPatch
) -> None:
    take_midi_path = tmp_path / "take.mid"
    midi = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    track.append(Message("note_on", note=1, velocity=64, time=0))
    track.append(Message("note_off", note=1, velocity=0, time=240))
    midi.tracks.append(track)
    midi.save(take_midi_path)

    take = store.save_take("chopin_op11", 2, take_midi_path)

    def _boom(*args: object, **kwargs: object) -> None:
        raise ValueError("no localization candidates")

    monkeypatch.setattr(aligner, "align_take", _boom)
    aligner.align_and_store("chopin_op11", 2, take.take_id)

    assert len(fake_events.published) == 1
    event = fake_events.published[0]
    assert event.take_id == take.take_id
    assert event.status == store.UNALIGNABLE
    assert event.score_start_beat is None
    assert event.score_end_beat is None


def test_align_and_store_does_not_publish_when_discard_race_wins(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path, fake_events: _FakeEvents
) -> None:
    rng = random.Random(SEED + 1)
    notes = _synthetic_take_notes(reference, 500, 30, rng)
    take_midi_path = tmp_path / "take.mid"
    _write_take_midi(take_midi_path, notes)

    take = store.save_take("chopin_op11", 2, take_midi_path)
    store.discard_take("chopin_op11", 2, take.take_id)

    result = aligner.align_and_store("chopin_op11", 2, take.take_id)

    assert result.status == store.DISCARDED
    assert fake_events.published == []


def test_resolve_take_publishes_alignment_done(
    reference: tuple[aligner.ChordNote, ...], tmp_path: Path, fake_events: _FakeEvents
) -> None:
    repeat_a, repeat_length = 2605, 23
    notes = list(reference[repeat_a : repeat_a + repeat_length])
    take_midi_path = tmp_path / "take.mid"
    _write_take_midi(take_midi_path, notes)

    take = store.save_take("chopin_op11", 2, take_midi_path)
    aligner.align_and_store("chopin_op11", 2, take.take_id)
    fake_events.published.clear()  # only interested in the resolve's own event

    before = store.get_aligned_result("chopin_op11", 2, take.take_id)
    assert before is not None
    other_index = next(
        i
        for i, c in enumerate(before.candidates)
        if c.start_beat != before.score_start_beat
    )

    resolved = aligner.resolve_take("chopin_op11", 2, take.take_id, other_index)

    assert len(fake_events.published) == 1
    event = fake_events.published[0]
    assert event.take_id == take.take_id
    assert event.status == resolved.status


def test_run_exception_handler_publishes_alignment_done(
    tmp_path: Path, fake_events: _FakeEvents, monkeypatch: pytest.MonkeyPatch
) -> None:
    take_midi_path = tmp_path / "take.mid"
    midi = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    track.append(Message("note_on", note=60, velocity=64, time=0))
    track.append(Message("note_off", note=60, velocity=0, time=240))
    midi.tracks.append(track)
    midi.save(take_midi_path)

    take = store.save_take("chopin_op11", 2, take_midi_path)

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated alignment crash")

    monkeypatch.setattr(aligner, "align_and_store", _boom)

    aligner.alignment_worker._run("chopin_op11", 2, take.take_id)

    assert store.get_take("chopin_op11", 2, take.take_id).status == store.UNALIGNABLE
    assert len(fake_events.published) == 1
    assert fake_events.published[0].status == store.UNALIGNABLE


def test_run_exception_handler_does_not_publish_when_discarded(
    tmp_path: Path, fake_events: _FakeEvents, monkeypatch: pytest.MonkeyPatch
) -> None:
    take_midi_path = tmp_path / "take.mid"
    midi = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    track.append(Message("note_on", note=60, velocity=64, time=0))
    track.append(Message("note_off", note=60, velocity=0, time=240))
    midi.tracks.append(track)
    midi.save(take_midi_path)

    take = store.save_take("chopin_op11", 2, take_midi_path)
    store.discard_take("chopin_op11", 2, take.take_id)

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated alignment crash")

    monkeypatch.setattr(aligner, "align_and_store", _boom)

    aligner.alignment_worker._run("chopin_op11", 2, take.take_id)

    assert store.get_take("chopin_op11", 2, take.take_id).status == store.DISCARDED
    assert fake_events.published == []
