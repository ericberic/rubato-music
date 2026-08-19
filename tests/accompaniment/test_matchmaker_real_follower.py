from __future__ import annotations

import math
import warnings
from pathlib import Path

import mido
import pytest

pytest.importorskip("matchmaker", reason="Install with `uv sync --extra live`")

from aimusic.accompaniment.conversion import convert_mxl_excerpt_to_bundle
from aimusic.accompaniment.following import PerformedNote
from aimusic.accompaniment.matchmaker_follower import (
    MatchmakerStreamFollower,
    run_matchmaker_midi_file,
)
from aimusic.accompaniment.simulation import generate_synthetic_performance

SOURCE_MXL = Path("assets/scores/chopin_op11_i_allegro_maestoso/source/score.mxl")
TICKS_PER_BEAT = 480
TEST_TEMPO_BPM = 120
SECONDS_PER_BEAT = 60 / TEST_TEMPO_BPM

pytestmark = pytest.mark.matchmaker


def _write_score_midi(path: Path, notes: list[tuple[float, int, float, int]]) -> Path:
    midi = mido.MidiFile(ticks_per_beat=TICKS_PER_BEAT)
    track = mido.MidiTrack()
    midi.tracks.append(track)
    track.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(TEST_TEMPO_BPM), time=0))
    _append_note_events(track, _score_note_rows(notes))
    midi.save(path)
    return path


def _write_performance_midi(
    path: Path,
    notes: list[tuple[float, int, float, int]],
) -> Path:
    midi = mido.MidiFile(ticks_per_beat=TICKS_PER_BEAT)
    track = mido.MidiTrack()
    midi.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(TEST_TEMPO_BPM), time=0))
    _append_note_events(track, _performance_note_rows(notes))
    midi.save(path)
    return path


def _score_note_rows(
    notes: list[tuple[float, int, float, int]],
) -> list[tuple[int, bool, int, int]]:
    rows = []
    for beat, pitch, duration_beats, velocity in notes:
        start_tick = int(round(beat * TICKS_PER_BEAT))
        end_tick = int(round((beat + duration_beats) * TICKS_PER_BEAT))
        rows.append((start_tick, True, pitch, velocity))
        rows.append((end_tick, False, pitch, 0))
    return rows


def _performance_note_rows(
    notes: list[tuple[float, int, float, int]],
) -> list[tuple[int, bool, int, int]]:
    rows = []
    ticks_per_second = TICKS_PER_BEAT / SECONDS_PER_BEAT
    for perf_time, pitch, duration_seconds, velocity in notes:
        start_tick = int(round(perf_time * ticks_per_second))
        end_tick = int(round((perf_time + duration_seconds) * ticks_per_second))
        rows.append((start_tick, True, pitch, velocity))
        rows.append((end_tick, False, pitch, 0))
    return rows


def _append_note_events(track: mido.MidiTrack, rows: list[tuple[int, bool, int, int]]) -> None:
    last_tick = 0
    for tick, is_note_on, pitch, velocity in sorted(rows, key=lambda row: (row[0], row[1])):
        message_type = "note_on" if is_note_on else "note_off"
        track.append(
            mido.Message(
                message_type,
                note=pitch,
                velocity=velocity,
                time=tick - last_tick,
            )
        )
        last_tick = tick


def test_matchmaker_real_midi_follower_tracks_synthetic_score(tmp_path: Path) -> None:
    score_notes = [
        (float(beat), pitch, 0.5, 80)
        for beat, pitch in enumerate([60, 62, 64, 65, 67, 69, 71, 72])
    ]
    performance_notes = [
        (beat * 0.67, pitch, 0.18, velocity) for beat, pitch, _duration, velocity in score_notes
    ]
    score_file = _write_score_midi(tmp_path / "score.mid", score_notes)
    performance_file = _write_performance_midi(tmp_path / "performance.mid", performance_notes)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        run = run_matchmaker_midi_file(
            score_file,
            performance_file,
            method="arzt",
            kwargs={
                "processor": "chord_onset",
                "piano_range": True,
                "polling_period": 0.001,
                "window_size": 2,
                "start_window_size": 2,
                "step_size": 5,
            },
        )

    assert [update.score_beat for update in run.updates] == pytest.approx(
        [note[0] for note in score_notes]
    )
    assert [update.perf_time for update in run.updates] == pytest.approx(
        [note[0] for note in performance_notes], abs=0.002
    )
    assert {update.raw_state["follower"] for update in run.updates} == {"matchmaker"}


def test_matchmaker_real_bytes_stream_adapter_tracks_incrementally(tmp_path: Path) -> None:
    score_notes = [
        (float(beat), pitch, 0.5, 80)
        for beat, pitch in enumerate([60, 62, 64, 65, 67, 69])
    ]
    score_file = _write_score_midi(tmp_path / "stream-score.mid", score_notes)
    follower = MatchmakerStreamFollower(
        score_file,
        method="pthmm",
        max_wait_seconds=0.3,
        minimum_lock_updates=3,
    )
    try:
        updates = [
            follower.observe(
                PerformedNote(perf_time=beat * 0.5, pitch=pitch, velocity=80)
            )
            for beat, pitch, _duration, _velocity in score_notes
        ]
    finally:
        follower.close()

    assert all(update is not None for update in updates)
    assert [update.score_beat for update in updates if update is not None] == pytest.approx(
        range(6)
    )
    assert [update.confidence for update in updates if update is not None] == [
        0.0,
        0.0,
        0.5,
        0.5,
        0.5,
        0.5,
    ]


def test_matchmaker_real_midi_follower_tracks_real_chopin_excerpt(
    tmp_path: Path,
) -> None:
    bundle = convert_mxl_excerpt_to_bundle(
        SOURCE_MXL,
        tmp_path / "bundle",
        start_measure=139,
        end_measure=141,
        version="matchmaker-test-m139-141",
    )
    score_notes = [
        (event.beat, event.pitch, event.duration_beats, event.velocity or 72)
        for event in bundle.solo_events
        if event.pitch is not None
    ]
    performance = generate_synthetic_performance(
        bundle.solo_events,
        beat_period_fn=lambda beat: 0.44 + 0.04 * math.sin(beat),
    )
    performance_notes = [
        (note.perf_time, note.pitch, 0.18, note.velocity) for note in performance
    ]
    expected_onsets = sorted(
        {event.beat for event in bundle.solo_events if event.pitch is not None}
    )
    score_file = _write_score_midi(tmp_path / "chopin_solo_score.mid", score_notes)
    performance_file = _write_performance_midi(
        tmp_path / "chopin_solo_performance.mid",
        performance_notes,
    )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        run = run_matchmaker_midi_file(score_file, performance_file, method="arzt")

    score_beats = [update.score_beat for update in run.updates]
    assert score_beats == pytest.approx(expected_onsets, abs=0.001)
    assert all(current <= later for current, later in zip(score_beats, score_beats[1:]))
    assert len(run.updates) == len(expected_onsets)
