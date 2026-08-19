"""MIDI file utilities based on mido.

These helpers keep I/O logic centralized so recording, replay, and future
score/accompaniment transforms can share deterministic file behavior.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from mido import Message, MidiFile, MidiTrack


def read_midi(path: Path | str) -> MidiFile:
    """Load a MIDI file from ``path``.

    Reading through :mod:`mido` preserves tempo/meta messages. We always
    load with ``clip=True`` to avoid accidental overflow.
    """

    return MidiFile(path, clip=True)


def write_midi(midi: MidiFile, path: Path | str) -> None:
    """Persist ``midi`` to ``path`` (overwrites existing files)."""

    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    midi.save(out_path)


def clone_track(track: MidiTrack) -> MidiTrack:
    """Create a shallow copy of ``track`` (messages are copied)."""

    new = MidiTrack()
    for msg in track:
        new.append(msg.copy(time=msg.time))
    return new


def copy_midi(src: MidiFile) -> MidiFile:
    """Return a deep copy of ``src`` (tracks + metadata)."""

    dest = MidiFile()
    dest.ticks_per_beat = src.ticks_per_beat
    for track in src.tracks:
        dest.tracks.append(clone_track(track))
    return dest


def iter_note_messages(track: MidiTrack) -> Iterable[Message]:
    """Yield note on/off messages only."""

    for msg in track:
        if msg.type in ("note_on", "note_off"):
            yield msg
