from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

import mido
import pytest

from aimusic.accompaniment.following import PerformedNote
from aimusic.accompaniment.pthmm_live_follower import (
    PthmmLiveFollower,
    _reference_note_array,
)

pytestmark = pytest.mark.matchmaker


def _require_matchmaker() -> None:
    try:
        distribution("pymatchmaker")
    except PackageNotFoundError:
        pytest.skip("requires the optional live dependency pymatchmaker")


def _score(path: Path) -> Path:
    midi = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    midi.tracks.append(track)
    last_tick = 0
    for beat, pitch in enumerate((60, 62, 64, 65, 67, 69)):
        tick = beat * 480
        track.append(mido.Message("note_on", note=pitch, velocity=80, time=tick - last_tick))
        track.append(mido.Message("note_off", note=pitch, velocity=0, time=120))
        last_tick = tick + 120
    midi.save(path)
    return path


def test_reference_loader_preserves_midi_quarter_coordinates(tmp_path: Path) -> None:
    notes = _reference_note_array(_score(tmp_path / "score.mid"))
    assert notes["onset_beat"].tolist() == pytest.approx(range(6))
    assert notes["pitch"].tolist() == [60, 62, 64, 65, 67, 69]


def test_narrow_follower_tracks_pitch_sequence(tmp_path: Path) -> None:
    _require_matchmaker()
    follower = PthmmLiveFollower(
        _score(tmp_path / "score.mid"),
        max_wait_seconds=0.3,
        minimum_lock_updates=3,
    )
    try:
        updates = [
            follower.observe(PerformedNote(perf_time=beat * 0.5, pitch=pitch, velocity=80))
            for beat, pitch in enumerate((60, 62, 64, 65, 67, 69))
        ]
    finally:
        follower.close()
    assert all(update is not None for update in updates)
    assert [update.score_beat for update in updates if update is not None] == pytest.approx(
        range(6)
    )
    assert updates[2] is not None and updates[2].confidence == 0.5
    assert updates[2].raw_state["dependency_scope"] == "live_pitch_only"


def test_pitch_hmm_import_does_not_load_offline_audio_or_plotting_stack() -> None:
    _require_matchmaker()
    command = (
        "import json,sys; "
        "from aimusic.accompaniment.pthmm_live_follower import _load_pitch_hmm; "
        "_load_pitch_hmm(); "
        "print(json.dumps({name: name in sys.modules for name in "
        "['matplotlib','librosa','partitura']}))"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    result = subprocess.run(
        [sys.executable, "-c", command],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert json.loads(result.stdout) == {
        "matplotlib": False,
        "librosa": False,
        "partitura": False,
    }
