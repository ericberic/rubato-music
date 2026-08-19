"""Shared MIDI timing helpers."""

from __future__ import annotations

from mido import MidiFile

_DEFAULT_TEMPO_US = 500_000  # 120 BPM


def first_tempo_us(midi: MidiFile) -> int:
    """Return the first tempo meta event (microseconds per beat)."""

    for track in midi.tracks:
        for msg in track:
            if msg.type == "set_tempo":
                return msg.tempo
    return _DEFAULT_TEMPO_US


def ticks_to_ms(ticks: int | float, ticks_per_beat: int, tempo_us: int) -> float:
    """Convert ticks to milliseconds using ``tempo_us``."""

    if ticks <= 0 or ticks_per_beat <= 0 or tempo_us <= 0:
        return 0.0
    return (ticks / ticks_per_beat) * (tempo_us / 1000.0)


def ms_to_ticks(ms: float, ticks_per_beat: int, tempo_us: int) -> float:
    """Convert milliseconds to ticks."""

    if ms <= 0 or ticks_per_beat <= 0 or tempo_us <= 0:
        return 0.0
    return ms * ticks_per_beat * (1000.0 / tempo_us)
