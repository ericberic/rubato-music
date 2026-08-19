"""Offline MIDI alignment and accompaniment retiming utilities."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from mido import Message, MetaMessage, MidiFile, MidiTrack, bpm2tempo, second2tick, tick2second

DEFAULT_RENDER_TICKS_PER_BEAT = 480
DEFAULT_RENDER_TEMPO_BPM = 120


@dataclass(frozen=True)
class MidiNoteEvent:
    """A note-on event extracted from a MIDI file with absolute time."""

    index: int
    time_seconds: float
    pitch: int
    velocity: int
    channel: int
    track_index: int


@dataclass(frozen=True)
class TempoMapPoint:
    """A global MIDI tempo segment boundary."""

    absolute_ticks: int
    absolute_seconds: float
    tempo: int


@dataclass(frozen=True)
class AlignmentPair:
    """A pitch-sequence alignment pair between performance and reference."""

    performance_index: int
    reference_index: int
    performance_time_seconds: float
    reference_time_seconds: float
    performance_pitch: int
    reference_pitch: int
    is_pitch_match: bool


@dataclass(frozen=True)
class AlignmentSummary:
    """Serializable metrics for an offline alignment."""

    reference_note_count: int
    performance_note_count: int
    pair_count: int
    pitch_match_count: int
    pitch_mismatch_count: int
    extra_performance_note_count: int
    missing_reference_note_count: int
    first_reference_time_seconds: float | None
    last_reference_time_seconds: float | None
    first_performance_time_seconds: float | None
    last_performance_time_seconds: float | None
    median_abs_residual_seconds: float | None
    p90_abs_residual_seconds: float | None


@dataclass(frozen=True)
class AlignmentResult:
    """Result of aligning performed MIDI note-ons to reference MIDI note-ons."""

    pairs: tuple[AlignmentPair, ...]
    extra_performance_indices: tuple[int, ...]
    missing_reference_indices: tuple[int, ...]
    summary: AlignmentSummary

    @property
    def matched_pairs(self) -> tuple[AlignmentPair, ...]:
        return tuple(pair for pair in self.pairs if pair.is_pitch_match)


@dataclass(frozen=True)
class TimingAnchor:
    """A stable mapping from reference time to performance time."""

    reference_time_seconds: float
    performance_time_seconds: float
    reference_index: int
    performance_index: int


@dataclass(frozen=True)
class PhraseTimingAnchor:
    """A phrase-level timing anchor summarized from multiple matched notes."""

    phrase_index: int
    reference_time_seconds: float
    performance_time_seconds: float
    reference_start_seconds: float
    reference_end_seconds: float
    performance_start_seconds: float
    performance_end_seconds: float
    matched_note_count: int

    def to_timing_anchor(self) -> TimingAnchor:
        return TimingAnchor(
            reference_time_seconds=self.reference_time_seconds,
            performance_time_seconds=self.performance_time_seconds,
            reference_index=self.phrase_index,
            performance_index=self.phrase_index,
        )


class PiecewiseLinearTimingMap:
    """Map source/reference seconds into performed seconds using matched anchors."""

    def __init__(self, anchors: tuple[TimingAnchor, ...]) -> None:
        if not anchors:
            raise ValueError("at least one timing anchor is required")
        sorted_anchors = tuple(sorted(anchors, key=lambda anchor: anchor.reference_time_seconds))
        if (
            len(sorted_anchors) > 1
            and sorted_anchors[0].reference_time_seconds
            == sorted_anchors[-1].reference_time_seconds
        ):
            raise ValueError("anchors must span a non-zero reference time duration")
        self.anchors = sorted_anchors

    def map_time(self, reference_time_seconds: float) -> float:
        anchors = self.anchors
        if len(anchors) == 1:
            anchor = anchors[0]
            return anchor.performance_time_seconds + (
                reference_time_seconds - anchor.reference_time_seconds
            )

        if reference_time_seconds <= anchors[0].reference_time_seconds:
            return _linear_interp(reference_time_seconds, anchors[0], anchors[1])
        if reference_time_seconds >= anchors[-1].reference_time_seconds:
            return _linear_interp(reference_time_seconds, anchors[-2], anchors[-1])

        lo = 0
        hi = len(anchors) - 1
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if anchors[mid].reference_time_seconds <= reference_time_seconds:
                lo = mid
            else:
                hi = mid
        return _linear_interp(reference_time_seconds, anchors[lo], anchors[hi])


def extract_note_events(
    midi_path: Path | str,
    *,
    track_name_contains: tuple[str, ...] = (),
) -> tuple[MidiNoteEvent, ...]:
    """Extract note-on events with absolute seconds from selected MIDI tracks."""

    midi = MidiFile(midi_path, clip=True)
    tempo_map = _build_tempo_map(midi)
    notes: list[MidiNoteEvent] = []
    selected_markers = tuple(marker.upper() for marker in track_name_contains)
    for track_index, track in enumerate(midi.tracks):
        if selected_markers and not _track_matches(track, selected_markers):
            continue
        elapsed_ticks = 0
        tempo_index = 0
        for message in track:
            elapsed_ticks += message.time
            elapsed_seconds, tempo_index = _seconds_at_tick(
                elapsed_ticks,
                midi.ticks_per_beat,
                tempo_map,
                tempo_index,
            )
            if message.type == "note_on" and message.velocity > 0:
                notes.append(
                    MidiNoteEvent(
                        index=len(notes),
                        time_seconds=elapsed_seconds,
                        pitch=message.note,
                        velocity=message.velocity,
                        channel=message.channel,
                        track_index=track_index,
                    )
                )
    return tuple(sorted(notes, key=lambda note: (note.time_seconds, note.index)))


def align_note_events(
    reference_notes: tuple[MidiNoteEvent, ...],
    performance_notes: tuple[MidiNoteEvent, ...],
    *,
    match_score: float = 3.0,
    mismatch_score: float = -2.0,
    gap_score: float = -1.5,
) -> AlignmentResult:
    """Align performance to reference using local pitch-sequence alignment."""

    if not reference_notes or not performance_notes:
        return _empty_alignment(reference_notes, performance_notes)

    ref_pitches = [note.pitch for note in reference_notes]
    perf_pitches = [note.pitch for note in performance_notes]
    cols = len(ref_pitches)
    previous = [0.0] * (cols + 1)
    directions: list[list[int]] = []
    best_score = 0.0
    best_i = 0
    best_j = 0

    for i, perf_pitch in enumerate(perf_pitches, start=1):
        current = [0.0] * (cols + 1)
        direction_row = [0] * (cols + 1)
        for j, ref_pitch in enumerate(ref_pitches, start=1):
            diagonal = previous[j - 1] + (
                match_score if perf_pitch == ref_pitch else mismatch_score
            )
            delete = previous[j] + gap_score
            insert = current[j - 1] + gap_score
            value = max(0.0, diagonal, delete, insert)
            current[j] = value
            if value == 0:
                direction = 0
            elif value == diagonal:
                direction = 1
            elif value == delete:
                direction = 2
            else:
                direction = 3
            direction_row[j] = direction
            if value > best_score:
                best_score = value
                best_i = i
                best_j = j
        directions.append(direction_row)
        previous = current

    i = best_i
    j = best_j
    pairs: list[AlignmentPair] = []
    extra_perf: list[int] = []
    missing_ref: list[int] = []
    while i > 0 and j > 0:
        direction = directions[i - 1][j]
        if direction == 0:
            break
        if direction == 1:
            perf_note = performance_notes[i - 1]
            ref_note = reference_notes[j - 1]
            pairs.append(
                AlignmentPair(
                    performance_index=i - 1,
                    reference_index=j - 1,
                    performance_time_seconds=perf_note.time_seconds,
                    reference_time_seconds=ref_note.time_seconds,
                    performance_pitch=perf_note.pitch,
                    reference_pitch=ref_note.pitch,
                    is_pitch_match=perf_note.pitch == ref_note.pitch,
                )
            )
            i -= 1
            j -= 1
        elif direction == 2:
            extra_perf.append(i - 1)
            i -= 1
        else:
            missing_ref.append(j - 1)
            j -= 1

    pairs.reverse()
    paired_perf = {pair.performance_index for pair in pairs}
    paired_ref = {pair.reference_index for pair in pairs}
    extra_perf = [index for index in range(len(performance_notes)) if index not in paired_perf]
    missing_ref = [index for index in range(len(reference_notes)) if index not in paired_ref]
    summary = _alignment_summary(reference_notes, performance_notes, pairs, extra_perf, missing_ref)
    return AlignmentResult(
        pairs=tuple(pairs),
        extra_performance_indices=tuple(extra_perf),
        missing_reference_indices=tuple(missing_ref),
        summary=summary,
    )


def timing_map_from_alignment(
    alignment: AlignmentResult,
    *,
    minimum_match_count: int = 2,
) -> PiecewiseLinearTimingMap:
    """Create a timing map from pitch-matched alignment pairs."""

    anchors = tuple(
        TimingAnchor(
            reference_time_seconds=pair.reference_time_seconds,
            performance_time_seconds=pair.performance_time_seconds,
            reference_index=pair.reference_index,
            performance_index=pair.performance_index,
        )
        for pair in alignment.matched_pairs
    )
    if len(anchors) < minimum_match_count:
        raise ValueError(
            f"at least {minimum_match_count} pitch-matched anchors are required; got {len(anchors)}"
        )
    return PiecewiseLinearTimingMap(anchors)


def phrase_timing_anchors_from_alignment(
    alignment: AlignmentResult,
    *,
    max_reference_gap_seconds: float = 1.5,
    max_performance_gap_seconds: float = 3.0,
    minimum_match_count: int = 2,
) -> tuple[PhraseTimingAnchor, ...]:
    """Group matched note anchors into phrase-level timing anchors.

    The phrase midpoint uses the median matched time inside each group, so a
    single locally late/early note has less influence than in a note-level map.
    """

    matches = sorted(
        alignment.matched_pairs,
        key=lambda pair: (pair.reference_time_seconds, pair.performance_time_seconds),
    )
    groups: list[list[AlignmentPair]] = []
    current: list[AlignmentPair] = []
    previous: AlignmentPair | None = None
    for pair in matches:
        starts_new_group = False
        if previous is not None:
            starts_new_group = (
                pair.reference_time_seconds - previous.reference_time_seconds
                > max_reference_gap_seconds
                or abs(pair.performance_time_seconds - previous.performance_time_seconds)
                > max_performance_gap_seconds
            )
        if starts_new_group and current:
            groups.append(current)
            current = []
        current.append(pair)
        previous = pair
    if current:
        groups.append(current)

    anchors: list[PhraseTimingAnchor] = []
    phrase_index = 0
    for group in groups:
        if len(group) < minimum_match_count:
            continue
        reference_times = [pair.reference_time_seconds for pair in group]
        performance_times = [pair.performance_time_seconds for pair in group]
        anchors.append(
            PhraseTimingAnchor(
                phrase_index=phrase_index,
                reference_time_seconds=_median(reference_times),
                performance_time_seconds=_median(performance_times),
                reference_start_seconds=min(reference_times),
                reference_end_seconds=max(reference_times),
                performance_start_seconds=min(performance_times),
                performance_end_seconds=max(performance_times),
                matched_note_count=len(group),
            )
        )
        phrase_index += 1
    return tuple(anchors)


def timing_map_from_phrase_anchors(
    anchors: tuple[PhraseTimingAnchor, ...],
    *,
    minimum_anchor_count: int = 2,
) -> PiecewiseLinearTimingMap:
    """Create a timing map from phrase-level anchors."""

    if len(anchors) < minimum_anchor_count:
        raise ValueError(
            f"at least {minimum_anchor_count} phrase anchors are required; got {len(anchors)}"
        )
    return PiecewiseLinearTimingMap(tuple(anchor.to_timing_anchor() for anchor in anchors))


def retime_midi(
    source_path: Path | str,
    output_path: Path | str,
    timing_map: PiecewiseLinearTimingMap,
    *,
    start_reference_seconds: float | None = None,
    end_reference_seconds: float | None = None,
    output_tempo_bpm: int = DEFAULT_RENDER_TEMPO_BPM,
    skip_track_name_contains: tuple[str, ...] = (),
) -> None:
    """Render a MIDI file by mapping source absolute seconds through a timing map."""

    source = MidiFile(source_path, clip=True)
    tempo_map = _build_tempo_map(source)
    output = MidiFile(type=1, ticks_per_beat=DEFAULT_RENDER_TICKS_PER_BEAT)
    tempo = bpm2tempo(output_tempo_bpm)
    markers = tuple(marker.upper() for marker in skip_track_name_contains)

    for track in source.tracks:
        rendered_track = MidiTrack()
        output.tracks.append(rendered_track)
        if markers and _track_matches(track, markers):
            continue
        events = _retimed_track_events(
            source,
            track,
            tempo_map,
            timing_map,
            start_reference_seconds=start_reference_seconds,
            end_reference_seconds=end_reference_seconds,
        )
        last_time = 0.0
        for mapped_time, message in events:
            delta = max(0.0, mapped_time - last_time)
            copied = message.copy(time=int(round(second2tick(delta, output.ticks_per_beat, tempo))))
            rendered_track.append(copied)
            last_time = mapped_time
        rendered_track.append(MetaMessage("end_of_track", time=0))

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    output.save(out_path)


def write_alignment_artifacts(
    alignment: AlignmentResult,
    *,
    trace_path: Path,
    metrics_path: Path,
) -> None:
    """Write JSONL trace and metrics for an alignment run."""

    trace_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    with trace_path.open("w", encoding="utf-8") as handle:
        for pair in alignment.pairs:
            handle.write(json.dumps(asdict(pair), sort_keys=True) + "\n")
    metrics_path.write_text(
        json.dumps(asdict(alignment.summary), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _retimed_track_events(
    source: MidiFile,
    track: MidiTrack,
    tempo_map: tuple[TempoMapPoint, ...],
    timing_map: PiecewiseLinearTimingMap,
    *,
    start_reference_seconds: float | None,
    end_reference_seconds: float | None,
) -> list[tuple[float, Message | MetaMessage]]:
    elapsed_ticks = 0
    tempo_index = 0
    events: list[tuple[float, Message | MetaMessage]] = []
    active_notes: set[tuple[int, int]] = set()
    for message in track:
        elapsed_ticks += message.time
        source_elapsed, tempo_index = _seconds_at_tick(
            elapsed_ticks,
            source.ticks_per_beat,
            tempo_map,
            tempo_index,
        )
        if message.type == "set_tempo":
            continue
        if message.type == "end_of_track":
            continue
        if message.is_meta:
            if message.type in {"track_name", "instrument_name"}:
                events.append((0.0, message.copy(time=0)))
            elif message.type in {
                "time_signature",
                "key_signature",
                "marker",
                "cue_marker",
                "text",
            }:
                mapped = max(0.0, timing_map.map_time(source_elapsed))
                events.append((mapped, message.copy(time=0)))
            continue
        if start_reference_seconds is not None and source_elapsed < start_reference_seconds:
            if _is_setup_message(message):
                events.append((0.0, message.copy(time=0)))
            continue
        if end_reference_seconds is not None and source_elapsed > end_reference_seconds:
            if not active_notes:
                break
            note_key = _note_key(message)
            if _is_note_release(message) and note_key in active_notes:
                events.append((timing_map.map_time(source_elapsed), message.copy(time=0)))
                active_notes.discard(note_key)
            continue
        note_key = _note_key(message)
        if message.type == "note_on" and message.velocity > 0 and note_key is not None:
            active_notes.add(note_key)
        elif _is_note_release(message) and note_key is not None:
            if start_reference_seconds is not None and note_key not in active_notes:
                continue
            active_notes.discard(note_key)
        mapped = max(0.0, timing_map.map_time(source_elapsed))
        events.append((mapped, message.copy(time=0)))
    return sorted(events, key=lambda item: item[0])


def _build_tempo_map(midi: MidiFile) -> tuple[TempoMapPoint, ...]:
    """Build the global MIDI tempo map across tracks."""

    tempo_changes = [(0, 500000)]
    for track in midi.tracks:
        elapsed_ticks = 0
        for message in track:
            elapsed_ticks += message.time
            if message.type == "set_tempo":
                tempo_changes.append((elapsed_ticks, message.tempo))

    tempo_changes.sort(key=lambda item: item[0])
    tempo_map: list[TempoMapPoint] = []
    current_seconds = 0.0
    current_ticks = 0
    current_tempo = 500000
    for change_ticks, tempo in tempo_changes:
        delta_ticks = change_ticks - current_ticks
        if delta_ticks > 0:
            current_seconds += tick2second(delta_ticks, midi.ticks_per_beat, current_tempo)
        tempo_map.append(
            TempoMapPoint(
                absolute_ticks=change_ticks,
                absolute_seconds=current_seconds,
                tempo=tempo,
            )
        )
        current_ticks = change_ticks
        current_tempo = tempo
    return tuple(tempo_map)


def _seconds_at_tick(
    absolute_ticks: int,
    ticks_per_beat: int,
    tempo_map: tuple[TempoMapPoint, ...],
    tempo_index: int,
) -> tuple[float, int]:
    while (
        tempo_index + 1 < len(tempo_map)
        and tempo_map[tempo_index + 1].absolute_ticks <= absolute_ticks
    ):
        tempo_index += 1
    point = tempo_map[tempo_index]
    elapsed = point.absolute_seconds + tick2second(
        absolute_ticks - point.absolute_ticks,
        ticks_per_beat,
        point.tempo,
    )
    return elapsed, tempo_index


def _alignment_summary(
    reference_notes: tuple[MidiNoteEvent, ...],
    performance_notes: tuple[MidiNoteEvent, ...],
    pairs: list[AlignmentPair],
    extra_perf: list[int],
    missing_ref: list[int],
) -> AlignmentSummary:
    matches = [pair for pair in pairs if pair.is_pitch_match]
    residuals = _timing_residuals(matches)
    return AlignmentSummary(
        reference_note_count=len(reference_notes),
        performance_note_count=len(performance_notes),
        pair_count=len(pairs),
        pitch_match_count=len(matches),
        pitch_mismatch_count=len(pairs) - len(matches),
        extra_performance_note_count=len(extra_perf),
        missing_reference_note_count=len(missing_ref),
        first_reference_time_seconds=pairs[0].reference_time_seconds if pairs else None,
        last_reference_time_seconds=pairs[-1].reference_time_seconds if pairs else None,
        first_performance_time_seconds=pairs[0].performance_time_seconds if pairs else None,
        last_performance_time_seconds=pairs[-1].performance_time_seconds if pairs else None,
        median_abs_residual_seconds=_percentile_abs(residuals, 0.5),
        p90_abs_residual_seconds=_percentile_abs(residuals, 0.9),
    )


def _timing_residuals(matches: list[AlignmentPair]) -> list[float]:
    if len(matches) < 2:
        return []
    xs = [pair.performance_time_seconds for pair in matches]
    ys = [pair.reference_time_seconds for pair in matches]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    variance = sum((x - mean_x) ** 2 for x in xs)
    if variance == 0:
        return []
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / variance
    intercept = mean_y - slope * mean_x
    return [y - (intercept + slope * x) for x, y in zip(xs, ys)]


def _percentile_abs(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(abs(value) for value in values)
    index = min(len(ordered) - 1, round(percentile * (len(ordered) - 1)))
    return ordered[index]


def _median(values: list[float]) -> float:
    if not values:
        raise ValueError("median requires at least one value")
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2


def _empty_alignment(
    reference_notes: tuple[MidiNoteEvent, ...],
    performance_notes: tuple[MidiNoteEvent, ...],
) -> AlignmentResult:
    extra_perf = list(range(len(performance_notes)))
    missing_ref = list(range(len(reference_notes)))
    summary = _alignment_summary(reference_notes, performance_notes, [], extra_perf, missing_ref)
    return AlignmentResult(
        pairs=(),
        extra_performance_indices=tuple(extra_perf),
        missing_reference_indices=tuple(missing_ref),
        summary=summary,
    )


def _track_matches(track: MidiTrack, markers: tuple[str, ...]) -> bool:
    return any(
        message.type == "track_name" and any(marker in message.name.upper() for marker in markers)
        for message in track
    )


def _is_setup_message(message: Message | MetaMessage) -> bool:
    return message.type in {"program_change", "control_change", "pitchwheel"}


def _is_note_release(message: Message | MetaMessage) -> bool:
    return message.type == "note_off" or (message.type == "note_on" and message.velocity == 0)


def _note_key(message: Message | MetaMessage) -> tuple[int, int] | None:
    if message.type not in {"note_on", "note_off"}:
        return None
    return message.channel, message.note


def _linear_interp(reference_time_seconds: float, left: TimingAnchor, right: TimingAnchor) -> float:
    ref_delta = right.reference_time_seconds - left.reference_time_seconds
    if ref_delta == 0:
        return left.performance_time_seconds
    ratio = (reference_time_seconds - left.reference_time_seconds) / ref_delta
    return left.performance_time_seconds + ratio * (
        right.performance_time_seconds - left.performance_time_seconds
    )
