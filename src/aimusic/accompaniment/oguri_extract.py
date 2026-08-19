"""Extract Oguri MIDI source tracks into solo-reference/accompaniment files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mido import MidiFile, MidiTrack

from aimusic.accompaniment.oguri import PIANO_TRACK_MARKER, OguriMovement


@dataclass(frozen=True)
class OguriExtractionSummary:
    source_path: Path
    solo_reference_path: Path
    orchestra_accompaniment_path: Path
    solo_track_count: int
    orchestra_track_count: int
    solo_note_count: int
    orchestra_note_count: int


def extract_oguri_movement(movement: OguriMovement) -> OguriExtractionSummary:
    """Write solo-reference and orchestra-accompaniment MIDI files.

    Both output files preserve the source MIDI timing grid. The solo reference
    includes global metadata/setup plus the `PIANO SOLO` track. The orchestra
    accompaniment includes global metadata/setup plus all non-solo tracks.
    """

    source = MidiFile(movement.local_path, clip=True)
    metadata_tracks: list[MidiTrack] = []
    solo_tracks: list[MidiTrack] = []
    orchestra_tracks: list[MidiTrack] = []

    for track in source.tracks:
        if _is_piano_solo_track(track):
            solo_tracks.append(track)
        elif _has_note_events(track):
            orchestra_tracks.append(track)
        else:
            metadata_tracks.append(track)

    if not solo_tracks:
        raise ValueError(f"No {PIANO_TRACK_MARKER!r} track found in {movement.local_path}")
    if not orchestra_tracks:
        raise ValueError(f"No orchestra note tracks found in {movement.local_path}")

    movement.derived_dir.mkdir(parents=True, exist_ok=True)
    solo_midi = _build_midi(source, [*metadata_tracks, *solo_tracks])
    orchestra_midi = _build_midi(source, [*metadata_tracks, *orchestra_tracks])
    solo_midi.save(movement.solo_reference_path)
    orchestra_midi.save(movement.orchestra_accompaniment_path)

    return OguriExtractionSummary(
        source_path=movement.local_path,
        solo_reference_path=movement.solo_reference_path,
        orchestra_accompaniment_path=movement.orchestra_accompaniment_path,
        solo_track_count=len(solo_midi.tracks),
        orchestra_track_count=len(orchestra_midi.tracks),
        solo_note_count=_count_notes(solo_midi),
        orchestra_note_count=_count_notes(orchestra_midi),
    )


def _build_midi(source: MidiFile, tracks: list[MidiTrack]) -> MidiFile:
    midi_type = 1 if len(tracks) > 1 else source.type
    midi = MidiFile(type=midi_type, ticks_per_beat=source.ticks_per_beat)
    for track in tracks:
        midi.tracks.append(_clone_track(track))
    return midi


def _clone_track(track: MidiTrack) -> MidiTrack:
    clone = MidiTrack()
    for message in track:
        clone.append(message.copy(time=message.time))
    return clone


def _is_piano_solo_track(track: MidiTrack) -> bool:
    marker = PIANO_TRACK_MARKER.upper()
    return any(
        message.type == "track_name" and marker in message.name.upper()
        for message in track
    )


def _has_note_events(track: MidiTrack) -> bool:
    return any(
        message.type == "note_on" and message.velocity > 0
        for message in track
    )


def _count_notes(midi: MidiFile) -> int:
    return sum(
        1
        for track in midi.tracks
        for message in track
        if message.type == "note_on" and message.velocity > 0
    )
