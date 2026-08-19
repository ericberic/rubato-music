"""CRUD tests for the persistent take store (roadmap item 2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from mido import Message, MetaMessage, MidiFile, MidiTrack, bpm2tempo

from aimusic.core import paths
from aimusic.takes import store
from aimusic.takes.models import AlignedResult


@pytest.fixture(autouse=True)
def isolated_data_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))


def _write_fixture_midi(path: Path, *, note_count: int = 4) -> None:
    midi = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    midi.tracks.append(track)
    tempo = bpm2tempo(120)
    track.append(MetaMessage("set_tempo", tempo=tempo, time=0))
    for i in range(note_count):
        pitch = 60 + i
        track.append(Message("note_on", note=pitch, velocity=64, time=0 if i == 0 else 240))
        track.append(Message("note_off", note=pitch, velocity=0, time=240))
    path.parent.mkdir(parents=True, exist_ok=True)
    midi.save(path)


def test_save_take_writes_midi_and_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source.mid"
    _write_fixture_midi(source, note_count=5)

    take = store.save_take("chopin_op11", 2, source, input_name="CLP-795GP USB")

    assert take.take_id.startswith("t")
    assert take.piece_id == "chopin_op11"
    assert take.movement == 2
    assert take.note_on_count == 5
    assert take.duration_seconds > 0
    assert take.status == store.CAPTURED
    assert take.input_name == "CLP-795GP USB"

    take_dir = paths.take_dir("chopin_op11", 2, take.take_id, create=False)
    assert (take_dir / "take.mid").exists()
    assert (take_dir / "take.json").exists()


def test_save_take_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        store.save_take("chopin_op11", 2, tmp_path / "missing.mid")


def test_get_take_round_trips(tmp_path: Path) -> None:
    source = tmp_path / "source.mid"
    _write_fixture_midi(source)
    saved = store.save_take("chopin_op11", 2, source)

    fetched = store.get_take("chopin_op11", 2, saved.take_id)

    assert fetched == saved


def test_get_take_missing_raises_not_found() -> None:
    with pytest.raises(store.TakeNotFoundError):
        store.get_take("chopin_op11", 2, "t-does-not-exist")


def test_get_take_v2_missing_raises_not_found() -> None:
    """The v2 accessor is non-optional; callers never receive ``None``."""

    with pytest.raises(store.TakeNotFoundError):
        store.get_take_v2("chopin_op11", 2, "t-does-not-exist")


def test_list_takes_returns_all_in_recorded_order(tmp_path: Path) -> None:
    source = tmp_path / "source.mid"
    _write_fixture_midi(source)

    first = store.save_take(
        "chopin_op11",
        2,
        source,
        take_id="t20260709T000000Z-0001",
        recorded_at="2026-07-09T00:00:00Z",
    )
    second = store.save_take(
        "chopin_op11",
        2,
        source,
        take_id="t20260709T000100Z-0002",
        recorded_at="2026-07-09T00:01:00Z",
    )

    takes = store.list_takes("chopin_op11", 2)

    assert [take.take_id for take in takes] == [first.take_id, second.take_id]


def test_list_takes_empty_when_no_manifest() -> None:
    assert store.list_takes("chopin_op11", 2) == []


def test_list_takes_is_scoped_per_movement(tmp_path: Path) -> None:
    source = tmp_path / "source.mid"
    _write_fixture_midi(source)
    store.save_take("chopin_op11", 2, source)

    assert store.list_takes("chopin_op11", 3) == []
    assert len(store.list_takes("chopin_op11", 2)) == 1


def test_update_take_status_persists(tmp_path: Path) -> None:
    source = tmp_path / "source.mid"
    _write_fixture_midi(source)
    take = store.save_take("chopin_op11", 2, source)

    updated = store.update_take_status("chopin_op11", 2, take.take_id, store.ALIGNED)

    assert updated.status == store.ALIGNED
    assert store.get_take("chopin_op11", 2, take.take_id).status == store.ALIGNED


def test_discard_take_marks_status_but_keeps_files(tmp_path: Path) -> None:
    source = tmp_path / "source.mid"
    _write_fixture_midi(source)
    take = store.save_take("chopin_op11", 2, source)

    discarded = store.discard_take("chopin_op11", 2, take.take_id)

    assert discarded.status == store.DISCARDED
    take_dir = paths.take_dir("chopin_op11", 2, take.take_id, create=False)
    assert (take_dir / "take.mid").exists()
    assert (take_dir / "take.json").exists()


def test_aligned_result_round_trips(tmp_path: Path) -> None:
    source = tmp_path / "source.mid"
    _write_fixture_midi(source)
    take = store.save_take("chopin_op11", 2, source)

    assert store.get_aligned_result("chopin_op11", 2, take.take_id) is None

    aligned = AlignedResult(
        take_id=take.take_id,
        aligner="seeded-local-v1",
        score_start_beat=10.0,
        score_end_beat=14.0,
        match_rate=0.9,
        ambiguous=False,
        matched_notes=8,
        extra_notes=0,
        missing_notes=1,
        timing_map=(),
        candidates=(),
        cell_samples=(),
        edge_trim_beats=(1.0, 0.5),
    )
    store.write_aligned_result("chopin_op11", 2, take.take_id, aligned)

    assert store.get_aligned_result("chopin_op11", 2, take.take_id) == aligned
