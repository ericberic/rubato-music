"""Tests for Oguri solo/accompaniment extraction."""

from __future__ import annotations

from mido import MidiFile

from aimusic.accompaniment.oguri import PIANO_TRACK_MARKER, oguri_movement_2
from aimusic.accompaniment.oguri_extract import extract_oguri_movement


from tests.oguri_guard import requires_oguri_source

pytestmark = requires_oguri_source


def test_extract_oguri_movement_2_outputs_reference_and_accompaniment():
    movement = oguri_movement_2()
    summary = extract_oguri_movement(movement)

    assert summary.solo_reference_path.exists()
    assert summary.orchestra_accompaniment_path.exists()
    assert summary.solo_note_count == 2831
    assert summary.orchestra_note_count == 1285

    solo = MidiFile(summary.solo_reference_path)
    orchestra = MidiFile(summary.orchestra_accompaniment_path)

    assert solo.ticks_per_beat == movement.expected_ticks_per_beat
    assert orchestra.ticks_per_beat == movement.expected_ticks_per_beat
    assert _track_names(solo).count(PIANO_TRACK_MARKER) == 1
    assert PIANO_TRACK_MARKER not in _track_names(orchestra)
    assert "Violini I" in _track_names(orchestra)
    assert "Fagotti" in _track_names(orchestra)


def _track_names(midi: MidiFile) -> list[str]:
    return [
        message.name.strip()
        for track in midi.tracks
        for message in track
        if message.type == "track_name"
    ]
