"""Tests for the private-use Oguri second-movement MIDI source."""

from __future__ import annotations

from mido import MidiFile

from aimusic.accompaniment.oguri import PIANO_TRACK_MARKER, oguri_movement_2


from tests.oguri_guard import requires_oguri_source

pytestmark = requires_oguri_source


def test_oguri_movement_2_source_shape():
    source = oguri_movement_2()
    midi = MidiFile(source.local_path)

    assert source.local_path.exists()
    assert midi.type == 1
    assert midi.ticks_per_beat == source.expected_ticks_per_beat
    assert len(midi.tracks) == source.expected_track_count

    track_names = [
        msg.name.strip()
        for track in midi.tracks
        for msg in track
        if msg.type == "track_name"
    ]
    assert PIANO_TRACK_MARKER in track_names
    assert "Violini I" in track_names
    assert "Fagotti" in track_names
