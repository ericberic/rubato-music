"""Tests for multi-instance BBCSO orchestra rendering."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from mido import Message, MetaMessage, MidiFile, MidiTrack

from aimusic.audio import orchestra_render
from aimusic.audio.orchestra_render import (
    MAX_OUTPUT_PEAK,
    OGURI_MOVEMENT_2_PARTS,
    BbcsoPart,
    ScoreCueWindow,
    missing_plugin_states,
    render_oguri_bbcso_orchestra,
)


def test_default_part_set_covers_every_sounding_oguri_orchestra_section():
    assert [part.track_name for part in OGURI_MOVEMENT_2_PARTS] == [
        "Violini I",
        "Violini II",
        "Viole",
        "Violoncelli",
        "Contrabassi",
        "Corni (E)",
        "Flauti",
        "Clarinetti (C)",
        "Fagotti",
    ]


def test_missing_plugin_states_reports_only_uncaptured_files(tmp_path):
    parts = (
        BbcsoPart("Violini I", "violin.state"),
        BbcsoPart("Viole", "viola.state"),
    )
    (tmp_path / "violin.state").write_bytes(b"violin")

    assert missing_plugin_states(tmp_path, parts=parts) == (tmp_path / "viola.state",)


def test_render_uses_one_state_per_exact_track_and_applies_peak_safety(
    tmp_path, monkeypatch
):
    midi_path = _write_midi(
        tmp_path / "orchestra.mid",
        (("Violini I", 72), ("Violini II", 67)),
    )
    parts = (
        BbcsoPart("Violini I", "violin1.state"),
        BbcsoPart("Violini II", "violin2.state"),
    )
    (tmp_path / "violin1.state").write_bytes(b"0.4")
    (tmp_path / "violin2.state").write_bytes(b"0.7")
    plugin_path = tmp_path / "BBCSO.vst3"
    plugin_path.write_text("fixture")
    written: list[np.ndarray] = []

    class FakePlugin:
        is_instrument = True

        def __init__(self):
            self.raw_state = b"0"

        def process(self, messages, *, duration, sample_rate, **_kwargs):
            assert sum(message[0][0] & 0xF0 == 0x90 for message in messages) == 1
            return np.full((2, round(duration * sample_rate)), float(self.raw_state))

    class FakeAudioFile:
        def __init__(self, _path, _mode, _sample_rate, _channels):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def write(self, audio):
            written.append(audio.copy())

    class FakePedalboard:
        @staticmethod
        def load_plugin(*_args, **_kwargs):
            return FakePlugin()

    monkeypatch.setattr(
        orchestra_render,
        "_pedalboard_api",
        lambda: (FakePedalboard, FakeAudioFile),
    )
    output_path = tmp_path / "mix.wav"

    summary = render_oguri_bbcso_orchestra(
        midi_path,
        output_path,
        window=ScoreCueWindow(
            start_measure=1,
            end_measure_exclusive=2,
            source_start_seconds=0.0,
            source_end_seconds=1.0,
        ),
        state_dir=tmp_path,
        plugin_path=plugin_path,
        parts=parts,
        sample_rate=10.0,
        render_tail_seconds=0.0,
        mix_gain=1.0,
    )

    assert [stem.note_on_count for stem in summary.stems] == [1, 1]
    assert summary.peak_before_safety_gain == np.float32(1.1)
    assert summary.peak == pytest.approx(MAX_OUTPUT_PEAK)
    assert np.max(written[0]) == pytest.approx(MAX_OUTPUT_PEAK)


def _write_midi(path: Path, tracks: tuple[tuple[str, int], ...]) -> Path:
    midi = MidiFile(ticks_per_beat=480)
    for name, note in tracks:
        track = MidiTrack()
        track.append(MetaMessage("track_name", name=name, time=0))
        track.append(Message("note_on", note=note, velocity=80, time=0))
        track.append(Message("note_off", note=note, velocity=0, time=480))
        midi.tracks.append(track)
    midi.save(path)
    return path
