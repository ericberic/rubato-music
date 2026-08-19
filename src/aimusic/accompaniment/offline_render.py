"""High-level offline accompaniment render workflow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mido import MetaMessage, MidiFile, MidiTrack, bpm2tempo

from aimusic.accompaniment.offline_alignment import (
    AlignmentResult,
    align_note_events,
    extract_note_events,
    retime_midi,
    timing_map_from_alignment,
    write_alignment_artifacts,
)
from aimusic.core import paths


@dataclass(frozen=True)
class OfflineRenderResult:
    """Artifacts produced by an offline accompaniment render."""

    alignment: AlignmentResult
    trace_path: Path
    metrics_path: Path
    output_path: Path


def combine_review_midi(
    *,
    solo_path: Path | str,
    accompaniment_path: Path | str,
    output_path: Path | str,
    tempo_bpm: int = 120,
) -> Path:
    """Combine fixed-tempo solo and accompaniment into one rehearsal MIDI.

    Both inputs are rendered onto the same 120 BPM/480 PPQ wall-clock grid by
    the take-review pipeline. Keeping their tracks separate preserves channels,
    programs, controllers, and note lifetimes while giving browser and Yamaha
    playback a single artifact to start and stop atomically.
    """

    sources = (MidiFile(solo_path, clip=True), MidiFile(accompaniment_path, clip=True))
    ticks_per_beat = sources[0].ticks_per_beat
    if any(source.ticks_per_beat != ticks_per_beat for source in sources):
        raise ValueError("review MIDI inputs must use the same ticks_per_beat")

    combined = MidiFile(type=1, ticks_per_beat=ticks_per_beat)
    conductor = MidiTrack()
    conductor.append(MetaMessage("track_name", name="Rubato rehearsal review", time=0))
    conductor.append(MetaMessage("set_tempo", tempo=bpm2tempo(tempo_bpm), time=0))
    conductor.append(MetaMessage("end_of_track", time=0))
    combined.tracks.append(conductor)

    for source in sources:
        for track in source.tracks:
            copied_track = MidiTrack()
            carried_delta = 0
            for message in track:
                carried_delta += message.time
                if message.type in {"set_tempo", "end_of_track"}:
                    continue
                copied_track.append(message.copy(time=carried_delta))
                carried_delta = 0
            copied_track.append(MetaMessage("end_of_track", time=carried_delta))
            combined.tracks.append(copied_track)

    resolved = Path(output_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    combined.save(resolved)
    return resolved


def align_midi_inputs(
    reference_path: Path | str,
    performance_path: Path | str,
    *,
    reference_track_marker: str | None = None,
) -> AlignmentResult:
    """Align a performance MIDI file against a reference solo MIDI file."""

    markers = (reference_track_marker,) if reference_track_marker else ()
    reference_notes = extract_note_events(reference_path, track_name_contains=markers)
    performance_notes = extract_note_events(performance_path)
    return align_note_events(reference_notes, performance_notes)


def render_offline_midi(
    *,
    reference_path: Path | str,
    performance_path: Path | str,
    accompaniment_path: Path | str,
    run_id: str,
    reference_track_marker: str | None = None,
) -> OfflineRenderResult:
    """Align solo MIDI and retime accompaniment MIDI into a run directory."""

    alignment = align_midi_inputs(
        reference_path,
        performance_path,
        reference_track_marker=reference_track_marker,
    )
    trace_path = paths.run_trace_dir(run_id) / "alignment.jsonl"
    metrics_path = paths.run_analysis_dir(run_id) / "metrics.json"
    output_path = paths.run_output_dir(run_id) / "accompaniment.mid"
    write_alignment_artifacts(alignment, trace_path=trace_path, metrics_path=metrics_path)
    timing_map = timing_map_from_alignment(alignment)
    retime_midi(accompaniment_path, output_path, timing_map)
    return OfflineRenderResult(
        alignment=alignment,
        trace_path=trace_path,
        metrics_path=metrics_path,
        output_path=output_path,
    )
