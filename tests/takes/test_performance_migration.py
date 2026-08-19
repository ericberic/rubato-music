from __future__ import annotations

from pathlib import Path

import pytest
from mido import Message, MetaMessage, MidiFile, MidiTrack, bpm2tempo
from pydantic import ValidationError

from aimusic.accompaniment.bundle_v2 import BundleRef
from aimusic.takes import performance_migration
from aimusic.takes.lifecycle import AlignedResultV2
from aimusic.takes.performance_migration import (
    LegacyAlignedV2,
    migrate_alignment_performance_model,
)


def _write_take(path: Path) -> None:
    midi = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    track.append(MetaMessage("set_tempo", tempo=bpm2tempo(100), time=0))
    for index, pitch in enumerate((60, 62, 64)):
        track.append(
            Message(
                "note_on",
                note=pitch,
                velocity=70 + index,
                time=0 if index == 0 else 480,
            )
        )
        track.append(Message("note_off", note=pitch, velocity=0, time=0))
    midi.tracks.append(track)
    midi.save(path)


def test_migration_rebuilds_regular_canonical_cells_from_raw_midi(tmp_path: Path) -> None:
    take_path = tmp_path / "take.mid"
    _write_take(take_path)
    legacy = LegacyAlignedV2.model_validate(
        {
            "take_id": "take-1",
            "bundle": BundleRef(bundle_id="test", revision="r1", timeline_id="t1"),
            "aligner": "legacy",
            "start_score_tick": 0,
            "end_score_tick": 960,
            "match_rate": 0.9,
            "ambiguous": False,
            "matched_notes": 3,
            "extra_notes": 0,
            "missing_notes": 0,
            "timing_map": [
                {"score_tick": 0, "take_seconds": 0.0},
                {"score_tick": 480, "take_seconds": 0.6},
                {"score_tick": 960, "take_seconds": 1.2},
            ],
            "cell_samples": [
                {"score_tick": 0, "period_s": 0.6, "velocity": 70, "pedal": 0, "quality": 0.9}
            ],
        }
    )

    migrated = migrate_alignment_performance_model(
        legacy,
        take_path=take_path,
        canonicalize_tick=lambda tick: tick * 2,
        mapping_id="canonical-map",
    )

    assert migrated.coordinate_system == "canonical_score"
    assert migrated.start_score_tick == 0
    assert migrated.end_score_tick == 1920
    assert migrated.performance_model == "canonical-performance-v2"
    assert migrated.base_seconds_per_quarter == pytest.approx(0.6)
    assert migrated.cell_samples
    assert all(sample.score_tick % 480 == 0 for sample in migrated.cell_samples)
    assert all(sample.seconds_per_quarter == pytest.approx(0.6) for sample in migrated.cell_samples)
    assert all(sample.rubato_ratio == pytest.approx(1.0) for sample in migrated.cell_samples)


def test_runtime_schema_rejects_legacy_period_field() -> None:
    with pytest.raises(ValidationError, match="seconds_per_quarter"):
        AlignedResultV2.model_validate(
            {
                "take_id": "take-1",
                "bundle": {"bundle_id": "test", "revision": "r1", "timeline_id": "t1"},
                "aligner": "legacy",
                "coordinate_system": "canonical_score",
                "start_score_tick": 0,
                "end_score_tick": 960,
                "start_reference_tick": 0,
                "end_reference_tick": 960,
                "mapping_id": "map",
                "match_rate": 0.9,
                "ambiguous": False,
                "matched_notes": 1,
                "extra_notes": 0,
                "missing_notes": 0,
                "base_seconds_per_quarter": 0.6,
                "cell_samples": [
                    {
                        "score_tick": 0,
                        "period_s": 0.6,
                        "rubato_ratio": 1.0,
                        "velocity": 70,
                        "pedal": 0,
                        "quality": 0.9,
                    }
                ],
            }
        )


def test_atomic_write_removes_temporary_file_when_replace_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "profile.json"

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        performance_migration._atomic_write(target, "{}\n")

    assert list(tmp_path.iterdir()) == []
