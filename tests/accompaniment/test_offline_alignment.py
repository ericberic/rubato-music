"""Tests for offline MIDI alignment and accompaniment retiming."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mido import Message, MetaMessage, MidiFile, MidiTrack, bpm2tempo, second2tick

from aimusic import cli
from aimusic.accompaniment.offline_alignment import (
    AlignmentPair,
    AlignmentResult,
    AlignmentSummary,
    PiecewiseLinearTimingMap,
    TimingAnchor,
    align_note_events,
    extract_note_events,
    phrase_timing_anchors_from_alignment,
    retime_midi,
    timing_map_from_alignment,
    timing_map_from_phrase_anchors,
    write_alignment_artifacts,
)


def test_pitch_sequence_alignment_tolerates_extra_missing_and_wrong_notes(tmp_path: Path) -> None:
    reference = tmp_path / "reference.mid"
    performance = tmp_path / "performance.mid"
    _write_notes(reference, [(0.0, 60), (1.0, 62), (2.0, 64), (3.0, 65), (4.0, 67)])
    _write_notes(
        performance,
        [
            (0.20, 60),
            (0.55, 61),  # extra/wrong flourish
            (1.40, 62),
            (2.65, 64),
            (3.80, 66),  # mismatch against 65
            (5.20, 67),
        ],
    )

    alignment = align_note_events(
        extract_note_events(reference),
        extract_note_events(performance),
    )

    assert alignment.summary.reference_note_count == 5
    assert alignment.summary.performance_note_count == 6
    assert alignment.summary.pitch_match_count >= 4
    assert alignment.summary.extra_performance_note_count >= 1
    assert alignment.summary.first_reference_time_seconds == pytest.approx(0.0)
    assert alignment.summary.last_reference_time_seconds == pytest.approx(4.0)

    timing_map = timing_map_from_alignment(alignment)
    assert timing_map.map_time(0.0) == pytest.approx(0.20)
    assert timing_map.map_time(4.0) == pytest.approx(5.20)


def test_extract_note_events_uses_global_type1_tempo_map(tmp_path: Path) -> None:
    midi_path = tmp_path / "type1_tempo.mid"
    _write_type1_with_global_tempo(midi_path)

    notes = extract_note_events(midi_path)

    assert [note.pitch for note in notes] == [60, 62]
    assert [note.time_seconds for note in notes] == pytest.approx([0.5, 1.5])


def test_timing_map_rejects_degenerate_chord_only_anchors() -> None:
    anchors = (
        TimingAnchor(
            reference_time_seconds=1.0,
            performance_time_seconds=2.0,
            reference_index=0,
            performance_index=0,
        ),
        TimingAnchor(
            reference_time_seconds=1.0,
            performance_time_seconds=2.2,
            reference_index=1,
            performance_index=1,
        ),
    )

    with pytest.raises(ValueError, match="non-zero reference time"):
        PiecewiseLinearTimingMap(anchors)


def test_write_alignment_artifacts_outputs_trace_and_metrics(tmp_path: Path) -> None:
    reference = tmp_path / "reference.mid"
    performance = tmp_path / "performance.mid"
    _write_notes(reference, [(0.0, 60), (1.0, 62), (2.0, 64)])
    _write_notes(performance, [(1.0, 60), (2.1, 62), (3.5, 64)])
    alignment = align_note_events(extract_note_events(reference), extract_note_events(performance))

    trace_path = tmp_path / "trace" / "alignment.jsonl"
    metrics_path = tmp_path / "analysis" / "metrics.json"
    write_alignment_artifacts(alignment, trace_path=trace_path, metrics_path=metrics_path)

    trace_rows = [json.loads(line) for line in trace_path.read_text().splitlines()]
    metrics = json.loads(metrics_path.read_text())
    assert len(trace_rows) == alignment.summary.pair_count
    assert metrics["pitch_match_count"] == 3


def test_render_offline_cli_reports_alignment_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))
    reference = tmp_path / "reference.mid"
    performance = tmp_path / "performance.mid"
    accompaniment = tmp_path / "accompaniment.mid"
    _write_notes(reference, [(0.0, 60)])
    _write_notes(performance, [(1.0, 60)])
    _write_notes(accompaniment, [(0.0, 48)], channel=2)

    exit_code = cli.main(
        [
            "render-offline",
            "--reference",
            str(reference),
            "--performance",
            str(performance),
            "--accompaniment",
            str(accompaniment),
            "--run-id",
            "too_short",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "at least 2 pitch-matched anchors" in captured.err
    assert (tmp_path / "runs" / "too_short" / "analysis" / "metrics.json").exists()


def test_retime_midi_maps_accompaniment_to_performed_timing(tmp_path: Path) -> None:
    reference = tmp_path / "reference.mid"
    performance = tmp_path / "performance.mid"
    accompaniment = tmp_path / "accompaniment.mid"
    rendered = tmp_path / "rendered.mid"
    _write_notes(reference, [(0.0, 60), (1.0, 62), (2.0, 64), (3.0, 65)])
    _write_notes(performance, [(2.0, 60), (3.0, 62), (5.0, 64), (8.0, 65)])
    _write_notes(accompaniment, [(1.0, 48), (2.0, 50), (3.0, 52)], channel=2)

    alignment = align_note_events(extract_note_events(reference), extract_note_events(performance))
    timing_map = timing_map_from_alignment(alignment)
    retime_midi(accompaniment, rendered, timing_map)

    rendered_notes = extract_note_events(rendered)
    assert [note.pitch for note in rendered_notes] == [48, 50, 52]
    assert [note.time_seconds for note in rendered_notes] == pytest.approx([3.0, 5.0, 8.0])


def test_phrase_timing_anchors_smooth_single_note_timing_outlier(tmp_path: Path) -> None:
    reference = tmp_path / "reference.mid"
    performance = tmp_path / "performance.mid"
    _write_notes(reference, [(0.0, 60), (0.3, 62), (0.6, 64), (4.0, 65), (4.3, 67), (4.6, 69)])
    _write_notes(performance, [(1.0, 60), (1.3, 62), (5.5, 64), (10.0, 65), (10.4, 67), (10.8, 69)])
    alignment = align_note_events(extract_note_events(reference), extract_note_events(performance))

    phrase_anchors = phrase_timing_anchors_from_alignment(
        alignment,
        max_reference_gap_seconds=1.0,
        max_performance_gap_seconds=5.0,
    )
    phrase_map = timing_map_from_phrase_anchors(phrase_anchors)
    note_map = timing_map_from_alignment(alignment)

    assert len(phrase_anchors) == 2
    assert phrase_anchors[0].matched_note_count == 3
    assert phrase_anchors[0].reference_time_seconds == pytest.approx(0.3)
    assert phrase_anchors[0].performance_time_seconds == pytest.approx(1.3)
    assert note_map.map_time(0.6) == pytest.approx(5.5)
    assert phrase_map.map_time(0.3) == pytest.approx(1.3)
    assert phrase_map.map_time(4.3) == pytest.approx(10.4)


def test_phrase_timing_anchors_split_large_non_monotonic_performance_gap() -> None:
    alignment = _manual_alignment(
        [
            (0.0, 10.0, 60),
            (0.3, 10.3, 62),
            (0.6, 1.0, 64),
            (0.9, 1.3, 65),
        ]
    )

    phrase_anchors = phrase_timing_anchors_from_alignment(
        alignment,
        max_reference_gap_seconds=1.0,
        max_performance_gap_seconds=3.0,
    )

    assert len(phrase_anchors) == 2
    assert [anchor.phrase_index for anchor in phrase_anchors] == [0, 1]
    assert [anchor.matched_note_count for anchor in phrase_anchors] == [2, 2]


def test_phrase_timing_anchors_keep_indices_contiguous_after_skipping_small_groups() -> None:
    alignment = _manual_alignment(
        [
            (0.0, 1.0, 60),
            (0.3, 1.3, 62),
            (4.0, 4.0, 64),
            (8.0, 9.0, 65),
            (8.3, 9.3, 67),
        ]
    )

    phrase_anchors = phrase_timing_anchors_from_alignment(
        alignment,
        max_reference_gap_seconds=1.0,
        minimum_match_count=2,
    )

    assert [anchor.phrase_index for anchor in phrase_anchors] == [0, 1]
    assert [anchor.matched_note_count for anchor in phrase_anchors] == [2, 2]


def test_retime_midi_uses_global_type1_tempo_map(tmp_path: Path) -> None:
    accompaniment = tmp_path / "accompaniment.mid"
    rendered = tmp_path / "rendered.mid"
    _write_type1_with_global_tempo(accompaniment, channel=2)
    timing_map = PiecewiseLinearTimingMap(
        (
            TimingAnchor(
                reference_time_seconds=0.5,
                performance_time_seconds=10.0,
                reference_index=0,
                performance_index=0,
            ),
            TimingAnchor(
                reference_time_seconds=1.5,
                performance_time_seconds=13.0,
                reference_index=1,
                performance_index=1,
            ),
        )
    )

    retime_midi(accompaniment, rendered, timing_map)

    rendered_notes = extract_note_events(rendered)
    assert [note.pitch for note in rendered_notes] == [60, 62]
    assert [note.time_seconds for note in rendered_notes] == pytest.approx([10.0, 13.0])


def _manual_alignment(pairs: list[tuple[float, float, int]]) -> AlignmentResult:
    alignment_pairs = tuple(
        AlignmentPair(
            performance_index=index,
            reference_index=index,
            performance_time_seconds=performance_time,
            reference_time_seconds=reference_time,
            performance_pitch=pitch,
            reference_pitch=pitch,
            is_pitch_match=True,
        )
        for index, (reference_time, performance_time, pitch) in enumerate(pairs)
    )
    return AlignmentResult(
        pairs=alignment_pairs,
        extra_performance_indices=(),
        missing_reference_indices=(),
        summary=AlignmentSummary(
            reference_note_count=len(alignment_pairs),
            performance_note_count=len(alignment_pairs),
            pair_count=len(alignment_pairs),
            pitch_match_count=len(alignment_pairs),
            pitch_mismatch_count=0,
            extra_performance_note_count=0,
            missing_reference_note_count=0,
            first_reference_time_seconds=alignment_pairs[0].reference_time_seconds,
            last_reference_time_seconds=alignment_pairs[-1].reference_time_seconds,
            first_performance_time_seconds=alignment_pairs[0].performance_time_seconds,
            last_performance_time_seconds=alignment_pairs[-1].performance_time_seconds,
            median_abs_residual_seconds=None,
            p90_abs_residual_seconds=None,
        ),
    )


def _write_notes(path: Path, notes: list[tuple[float, int]], *, channel: int = 0) -> None:
    midi = MidiFile(type=1, ticks_per_beat=480)
    tempo = bpm2tempo(120)
    track = MidiTrack()
    midi.tracks.append(track)
    track.append(MetaMessage("set_tempo", tempo=tempo, time=0))
    current_time = 0.0
    for start, pitch in notes:
        delta = max(0.0, start - current_time)
        track.append(
            Message(
                "note_on",
                note=pitch,
                velocity=64,
                channel=channel,
                time=int(round(second2tick(delta, midi.ticks_per_beat, tempo))),
            )
        )
        track.append(
            Message(
                "note_off",
                note=pitch,
                velocity=0,
                channel=channel,
                time=int(round(second2tick(0.2, midi.ticks_per_beat, tempo))),
            )
        )
        current_time = start + 0.2
    path.parent.mkdir(parents=True, exist_ok=True)
    midi.save(path)


def _write_type1_with_global_tempo(path: Path, *, channel: int = 0) -> None:
    midi = MidiFile(type=1, ticks_per_beat=480)
    tempo_track = MidiTrack()
    note_track = MidiTrack()
    midi.tracks.append(tempo_track)
    midi.tracks.append(note_track)
    tempo_track.append(MetaMessage("set_tempo", tempo=bpm2tempo(120), time=0))
    tempo_track.append(MetaMessage("set_tempo", tempo=bpm2tempo(60), time=480))
    tempo_track.append(MetaMessage("end_of_track", time=0))
    note_track.append(
        Message("note_on", note=60, velocity=64, channel=channel, time=480)
    )
    note_track.append(
        Message("note_off", note=60, velocity=0, channel=channel, time=120)
    )
    note_track.append(
        Message("note_on", note=62, velocity=64, channel=channel, time=360)
    )
    note_track.append(
        Message("note_off", note=62, velocity=0, channel=channel, time=120)
    )
    note_track.append(MetaMessage("end_of_track", time=0))
    path.parent.mkdir(parents=True, exist_ok=True)
    midi.save(path)
