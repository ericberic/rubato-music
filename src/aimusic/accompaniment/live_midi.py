"""Live MIDI recording and playback helpers for local rehearsal."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import mido
from mido import Message, MetaMessage, MidiFile, MidiTrack, bpm2tempo, second2tick

DEFAULT_TICKS_PER_BEAT = 480
DEFAULT_RECORD_TEMPO_BPM = 120
DEFAULT_CUE_RELEASE_TAIL_SECONDS = 2.0
MIN_TEMPO_SCALE = 1.0 / 3.0
# Canonical score calibration can legitimately exceed 2x source-time scaling:
# Movement II's expressive reference is much slower than its declared MIDI
# tick clock suggests, while the PWA accepts metronome marks up to ♩=200.
MAX_TEMPO_SCALE = 4.0
LOGGER = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class RecordedMidiSummary:
    path: Path
    message_count: int
    duration_seconds: float


@dataclass(frozen=True)
class MidiPlaybackSummary:
    source_path: Path
    event_count: int
    duration_seconds: float
    volume: float
    output_name: str
    tempo_scale: float = 1.0


@dataclass(frozen=True)
class MidiCueSummary:
    source_path: Path
    event_count: int
    note_on_count: int
    start_seconds: float
    duration_seconds: float
    volume: float
    first_note_on_seconds: float | None = None
    output_name: str | None = None
    tempo_scale: float = 1.0


def panic(output_name: str) -> None:
    """Send all-notes-off/all-sound-off to every MIDI channel."""

    with mido.open_output(output_name) as out:
        send_panic(out)


def send_panic(out) -> None:
    """Send panic messages to an already-open mido output."""

    for channel in range(16):
        out.send(mido.Message("control_change", channel=channel, control=123, value=0))
        out.send(mido.Message("control_change", channel=channel, control=120, value=0))


def record_midi(
    input_name: str,
    output_path: Path,
    *,
    duration_seconds: float | None = None,
    stop_event: threading.Event | None = None,
    ticks_per_beat: int = DEFAULT_TICKS_PER_BEAT,
    tempo_bpm: int = DEFAULT_RECORD_TEMPO_BPM,
) -> RecordedMidiSummary:
    """Record raw incoming MIDI messages to a timestamped SMF file.

    The recording keeps note, pedal, and controller messages. Wall-clock timing is
    converted to MIDI ticks at a fixed tempo so the take can be replayed and
    aligned deterministically.
    """

    output_path.parent.mkdir(parents=True, exist_ok=True)
    stop_event = stop_event or threading.Event()
    captured: list[tuple[float, mido.Message]] = []
    start = time.monotonic()
    end = start + duration_seconds if duration_seconds is not None else None

    with mido.open_input(input_name) as port:
        while not stop_event.is_set():
            now = time.monotonic()
            if end is not None and now >= end:
                break
            for msg in port.iter_pending():
                if not msg.is_meta:
                    captured.append((time.monotonic() - start, msg.copy(time=0)))
            time.sleep(0.002)

    midi = MidiFile(ticks_per_beat=ticks_per_beat)
    track = MidiTrack()
    midi.tracks.append(track)
    tempo = bpm2tempo(tempo_bpm)
    track.append(MetaMessage("set_tempo", tempo=tempo, time=0))
    last_time = 0.0
    for event_time, msg in sorted(captured, key=lambda item: item[0]):
        delta_seconds = max(event_time - last_time, 0.0)
        msg.time = int(round(second2tick(delta_seconds, ticks_per_beat, tempo)))
        track.append(msg)
        last_time = event_time
    midi.save(output_path)

    duration = captured[-1][0] if captured else 0.0
    return RecordedMidiSummary(
        path=output_path,
        message_count=len(captured),
        duration_seconds=duration,
    )


def play_midi(
    source_path: Path,
    output_name: str,
    *,
    volume: float = 1.0,
    max_seconds: float | None = None,
    start_seconds: float = 0.0,
    tempo_scale: float = 1.0,
    skip_track_name_contains: tuple[str, ...] = (),
    stop_event: threading.Event | None = None,
) -> MidiPlaybackSummary:
    """Play a MIDI window to an output port with optional track skipping/volume."""

    midi = MidiFile(source_path)
    tracks = [
        track for track in midi.tracks if not _track_name_matches(track, skip_track_name_contains)
    ]
    merged = mido.merge_tracks(tracks)
    stop_event = stop_event or threading.Event()
    events = _timed_output_events(
        merged,
        ticks_per_beat=midi.ticks_per_beat,
        volume=volume,
        max_seconds=max_seconds,
        start_seconds=max(0.0, start_seconds),
        tempo_scale=tempo_scale,
    )
    LOGGER.info(
        "MIDI playback window source=%s output=%s source_start=%.6fs "
        "window_duration=%s tempo_scale=%.3f scheduled_events=%d first_event=%.6fs",
        source_path,
        output_name,
        max(0.0, start_seconds),
        "full" if max_seconds is None else f"{max_seconds:.6f}s",
        tempo_scale,
        len(events),
        events[0][0] if events else 0.0,
    )

    with mido.open_output(output_name) as out:
        _set_initial_volume(out)
        start = time.monotonic()
        try:
            for event_time, msg in events:
                if stop_event.is_set():
                    break
                delay = start + event_time - time.monotonic()
                if delay > 0:
                    if stop_event.wait(delay):
                        break
                out.send(msg)
        finally:
            send_panic(out)

    return MidiPlaybackSummary(
        source_path=source_path,
        event_count=len(events),
        duration_seconds=events[-1][0] if events else 0.0,
        volume=volume,
        output_name=output_name,
        tempo_scale=tempo_scale,
    )


def inspect_midi_cue(
    source_path: Path,
    *,
    start_seconds: float,
    duration_seconds: float,
    volume: float = 1.0,
    release_tail_seconds: float = DEFAULT_CUE_RELEASE_TAIL_SECONDS,
    skip_track_name_contains: tuple[str, ...] = (),
    tempo_scale: float = 1.0,
) -> MidiCueSummary:
    """Return the exact event counts for a sliced MIDI cue without playing it."""

    events = _cue_output_events(
        source_path,
        volume=volume,
        start_seconds=start_seconds,
        duration_seconds=duration_seconds,
        release_tail_seconds=release_tail_seconds,
        skip_track_name_contains=skip_track_name_contains,
        tempo_scale=tempo_scale,
    )
    note_on_times = [
        event_time for event_time, msg in events if msg.type == "note_on" and msg.velocity > 0
    ]
    return MidiCueSummary(
        source_path=source_path,
        event_count=len(events),
        note_on_count=len(note_on_times),
        start_seconds=start_seconds,
        duration_seconds=events[-1][0] if events else 0.0,
        volume=volume,
        first_note_on_seconds=min(note_on_times) if note_on_times else None,
        tempo_scale=tempo_scale,
    )


def play_midi_cue(
    source_path: Path,
    output_name: str,
    *,
    start_seconds: float,
    duration_seconds: float,
    volume: float = 1.0,
    release_tail_seconds: float = DEFAULT_CUE_RELEASE_TAIL_SECONDS,
    skip_track_name_contains: tuple[str, ...] = (),
    stop_event: threading.Event | None = None,
    tempo_scale: float = 1.0,
) -> MidiCueSummary:
    """Play a sliced MIDI cue to an output port."""

    events = _cue_output_events(
        source_path,
        volume=volume,
        start_seconds=start_seconds,
        duration_seconds=duration_seconds,
        release_tail_seconds=release_tail_seconds,
        skip_track_name_contains=skip_track_name_contains,
        tempo_scale=tempo_scale,
    )
    note_on_count = _note_on_count(events)
    LOGGER.info(
        "Playing MIDI cue: source=%s output=%s start=%.3fs duration=%.3fs "
        "tempo_scale=%.3f events=%d note_ons=%d volume=%.2f",
        source_path,
        output_name,
        start_seconds,
        duration_seconds,
        tempo_scale,
        len(events),
        note_on_count,
        volume,
    )
    stop_event = stop_event or threading.Event()
    with mido.open_output(output_name) as out:
        _set_initial_volume(out)
        start = time.monotonic()
        try:
            for event_time, msg in events:
                if stop_event.is_set():
                    break
                delay = start + event_time - time.monotonic()
                if delay > 0 and stop_event.wait(delay):
                    break
                out.send(msg)
        finally:
            send_panic(out)
    note_on_times = [
        event_time for event_time, msg in events if msg.type == "note_on" and msg.velocity > 0
    ]
    return MidiCueSummary(
        source_path=source_path,
        event_count=len(events),
        note_on_count=note_on_count,
        start_seconds=start_seconds,
        duration_seconds=events[-1][0] if events else 0.0,
        volume=volume,
        first_note_on_seconds=min(note_on_times) if note_on_times else None,
        output_name=output_name,
        tempo_scale=tempo_scale,
    )


def alignment_audition_events(
    source_path: Path,
    *,
    start_seconds: float,
    duration_seconds: float,
    beat_period_seconds: float,
    beat_offsets_seconds: tuple[float, ...],
    volume: float = 1.0,
    metronome_volume: float = 0.3,
    count_in_beats: int = 4,
    skip_track_name_contains: tuple[str, ...] = (),
) -> list[tuple[float, mido.Message]]:
    """Build one score-local orchestra audition on a single MIDI clock.

    Four percussion clicks lead into the selected Oguri source window, then
    the orchestral events and the hypothesised beat clicks share timestamps.
    Keeping both streams on one backend clock is essential: separate browser
    and hardware clocks would manufacture exactly the alignment error this
    audition is intended to detect.
    """

    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    if beat_period_seconds <= 0:
        raise ValueError("beat_period_seconds must be positive")
    if count_in_beats < 1:
        raise ValueError("count_in_beats must be at least one")
    if not 0.0 <= metronome_volume <= 1.0:
        raise ValueError("metronome_volume must be between zero and one")
    if any(offset < 0 or offset >= duration_seconds for offset in beat_offsets_seconds):
        raise ValueError("beat offsets must fall inside the auditioned measure")

    count_in_seconds = count_in_beats * beat_period_seconds
    orchestra = midi_cue_events(
        source_path,
        volume=volume,
        start_seconds=start_seconds,
        duration_seconds=duration_seconds,
        release_tail_seconds=0.15,
        skip_track_name_contains=skip_track_name_contains,
    )
    events = [(count_in_seconds + timestamp, message) for timestamp, message in orchestra]

    def add_click(timestamp: float, *, accent: bool) -> None:
        if metronome_volume == 0:
            return
        # General MIDI percussion channel (zero-based channel 9): low/high
        # woodblock. Oguri Movement II does not use this channel, so the click
        # stays distinct without changing any orchestral program state.
        note = 77 if accent else 76
        base_velocity = 112 if accent else 92
        velocity = max(1, round(base_velocity * metronome_volume))
        events.append((timestamp, Message("note_on", channel=9, note=note, velocity=velocity)))
        events.append((timestamp + 0.06, Message("note_off", channel=9, note=note, velocity=0)))

    for index in range(count_in_beats):
        add_click(index * beat_period_seconds, accent=index == 0)
    for index, offset in enumerate(beat_offsets_seconds):
        add_click(count_in_seconds + offset, accent=index == 0)

    # Python's sort is stable, so source setup messages already at the measure
    # downbeat remain ahead of the simultaneous metronome note.
    events.sort(key=lambda item: item[0])
    return events


def play_alignment_audition(
    source_path: Path,
    output_name: str,
    *,
    start_seconds: float,
    duration_seconds: float,
    beat_period_seconds: float,
    beat_offsets_seconds: tuple[float, ...],
    volume: float = 1.0,
    metronome_volume: float = 0.3,
    count_in_beats: int = 4,
    skip_track_name_contains: tuple[str, ...] = (),
    stop_event: threading.Event | None = None,
) -> MidiCueSummary:
    """Play one count-in + orchestral measure alignment audition."""

    events = alignment_audition_events(
        source_path,
        start_seconds=start_seconds,
        duration_seconds=duration_seconds,
        beat_period_seconds=beat_period_seconds,
        beat_offsets_seconds=beat_offsets_seconds,
        volume=volume,
        metronome_volume=metronome_volume,
        count_in_beats=count_in_beats,
        skip_track_name_contains=skip_track_name_contains,
    )
    stop_event = stop_event or threading.Event()
    with mido.open_output(output_name) as out:
        _set_initial_volume(out)
        started_at = time.monotonic()
        try:
            for event_time, message in events:
                if stop_event.is_set():
                    break
                delay = started_at + event_time - time.monotonic()
                if delay > 0 and stop_event.wait(delay):
                    break
                out.send(message)
        finally:
            send_panic(out)

    note_on_times = [
        event_time
        for event_time, message in events
        if message.type == "note_on" and message.velocity > 0 and message.channel != 9
    ]
    return MidiCueSummary(
        source_path=source_path,
        event_count=len(events),
        note_on_count=len(note_on_times),
        start_seconds=start_seconds,
        duration_seconds=events[-1][0] if events else 0.0,
        volume=volume,
        first_note_on_seconds=min(note_on_times) if note_on_times else None,
        output_name=output_name,
    )


def _cue_output_events(
    source_path: Path,
    *,
    volume: float,
    start_seconds: float,
    duration_seconds: float | None,
    release_tail_seconds: float = DEFAULT_CUE_RELEASE_TAIL_SECONDS,
    skip_track_name_contains: tuple[str, ...] = (),
    tempo_scale: float = 1.0,
) -> list[tuple[float, mido.Message]]:
    return midi_cue_events(
        source_path,
        volume=volume,
        start_seconds=start_seconds,
        duration_seconds=duration_seconds,
        release_tail_seconds=release_tail_seconds,
        skip_track_name_contains=skip_track_name_contains,
        tempo_scale=tempo_scale,
    )


def midi_cue_events(
    source_path: Path,
    *,
    volume: float,
    start_seconds: float,
    duration_seconds: float | None,
    release_tail_seconds: float = DEFAULT_CUE_RELEASE_TAIL_SECONDS,
    include_track_names: tuple[str, ...] = (),
    include_track_name_contains: tuple[str, ...] = (),
    skip_track_name_contains: tuple[str, ...] = (),
    tempo_scale: float = 1.0,
) -> list[tuple[float, mido.Message]]:
    """Build timestamped channel events for a bounded MIDI cue.

    The public event seam is shared by realtime MIDI playback and optional
    software-instrument rendering.  Track inclusion is useful for a VST layer:
    one BBCSO instance owns one loaded patch, so feeding it every orchestral
    part would collapse unlike instruments into that patch.
    """

    midi = MidiFile(source_path)
    if include_track_names and include_track_name_contains:
        raise ValueError(
            "include_track_names and include_track_name_contains are mutually exclusive"
        )
    tracks = [
        track
        for track in midi.tracks
        if (
            (not include_track_names or _track_name_is(track, include_track_names))
            and (
                not include_track_name_contains
                or _track_name_matches(track, include_track_name_contains)
            )
        )
        and not _track_name_matches(track, skip_track_name_contains)
    ]
    if (include_track_names or include_track_name_contains) and not tracks:
        requested = ", ".join(include_track_names or include_track_name_contains)
        raise ValueError(f"No MIDI tracks matched: {requested}")
    merged = mido.merge_tracks(tracks)
    return _timed_output_events(
        merged,
        ticks_per_beat=midi.ticks_per_beat,
        volume=volume,
        max_seconds=duration_seconds,
        start_seconds=start_seconds,
        release_tail_seconds=release_tail_seconds,
        tempo_scale=tempo_scale,
    )


def _track_name_matches(track: MidiTrack, markers: tuple[str, ...]) -> bool:
    if not markers:
        return False
    normalized = tuple(marker.upper() for marker in markers)
    for msg in track:
        if msg.type == "track_name":
            name = msg.name.upper()
            if any(marker in name for marker in normalized):
                return True
    return False


def _track_name_is(track: MidiTrack, names: tuple[str, ...]) -> bool:
    """Return whether a track has one of the requested complete names."""

    normalized = {name.strip().casefold() for name in names}
    return any(
        msg.name.strip().casefold() in normalized for msg in track if msg.type == "track_name"
    )


def _timed_output_events(
    merged_track: MidiTrack,
    *,
    ticks_per_beat: int,
    volume: float,
    max_seconds: float | None,
    start_seconds: float = 0.0,
    release_tail_seconds: float = 0.0,
    tempo_scale: float = 1.0,
) -> list[tuple[float, mido.Message]]:
    tempo_scale = _validated_tempo_scale(tempo_scale)
    tempo = 500000
    elapsed = 0.0
    events: list[tuple[float, mido.Message]] = []
    setup_messages: dict[tuple[int, ...], mido.Message] = {}
    active_notes: dict[tuple[int, int], mido.Message] = {}
    carried_notes_inserted = False
    release_tail_end = None
    if max_seconds is not None:
        release_tail_end = max_seconds + max(0.0, release_tail_seconds)
    for msg in merged_track:
        elapsed += mido.tick2second(msg.time, ticks_per_beat, tempo)
        if msg.type == "set_tempo":
            tempo = msg.tempo
        relative_time = elapsed - start_seconds
        if msg.is_meta:
            continue
        scaled = _scale_message(msg, volume)
        if relative_time < 0:
            if _is_setup_message(scaled):
                setup_messages[_setup_key(scaled)] = scaled.copy(time=0)
            _update_active_notes(active_notes, scaled)
            continue
        if not carried_notes_inserted:
            events.extend((0.0, setup_msg) for setup_msg in setup_messages.values())
            events.extend((0.0, active_note) for active_note in active_notes.values())
            carried_notes_inserted = True
        if max_seconds is not None and relative_time > max_seconds:
            if (
                release_tail_end is not None
                and relative_time <= release_tail_end
                and _is_note_off(scaled)
                and _note_key(scaled) in active_notes
            ):
                events.append((relative_time / tempo_scale, scaled))
                _update_active_notes(active_notes, scaled)
                if not active_notes:
                    break
                continue
            if release_tail_end is None or relative_time > release_tail_end:
                break
            continue
        events.append((relative_time / tempo_scale, scaled))
        _update_active_notes(active_notes, scaled)
    if max_seconds is not None and release_tail_seconds > 0 and active_notes:
        if not carried_notes_inserted:
            events.extend((0.0, setup_msg) for setup_msg in setup_messages.values())
            events.extend((0.0, active_note) for active_note in active_notes.values())
        forced_time = (max_seconds + release_tail_seconds) / tempo_scale
        events.extend(
            (
                forced_time,
                Message(
                    "note_off",
                    channel=channel,
                    note=note,
                    velocity=0,
                    time=0,
                ),
            )
            for channel, note in active_notes
        )
    return events


def _validated_tempo_scale(value: float) -> float:
    if not MIN_TEMPO_SCALE <= value <= MAX_TEMPO_SCALE:
        raise ValueError("tempo_scale must be between 1/3 and 4.0")
    return value


def _update_active_notes(
    active_notes: dict[tuple[int, int], mido.Message],
    msg: mido.Message,
) -> None:
    if msg.type not in {"note_on", "note_off"}:
        return
    key = _note_key(msg)
    if msg.type == "note_on" and msg.velocity > 0:
        active_notes[key] = msg.copy(time=0)
        return
    active_notes.pop(key, None)


def _note_key(msg: mido.Message) -> tuple[int, int]:
    return (msg.channel, msg.note)


def _is_note_off(msg: mido.Message) -> bool:
    return msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0)


def _note_on_count(events: list[tuple[float, mido.Message]]) -> int:
    return sum(1 for _, msg in events if msg.type == "note_on" and msg.velocity > 0)


def _is_setup_message(msg: mido.Message) -> bool:
    return msg.type in {"program_change", "control_change", "pitchwheel"}


def _setup_key(msg: mido.Message) -> tuple[int, ...]:
    channel = getattr(msg, "channel", 0)
    if msg.type == "control_change":
        return (0, channel, msg.control)
    if msg.type == "program_change":
        return (1, channel)
    if msg.type == "pitchwheel":
        return (2, channel)
    return (3, channel)


def _scale_message(msg: mido.Message, volume: float) -> mido.Message:
    scaled = msg.copy()
    clamped_volume = max(0.0, min(1.5, volume))
    if scaled.type == "note_on" and scaled.velocity > 0:
        scaled.velocity = max(1, min(127, round(scaled.velocity * clamped_volume)))
    return scaled


def _set_initial_volume(out) -> None:
    """Reset channel volume/expression and let note velocity carry the scalar."""

    for channel in range(16):
        out.send(Message("control_change", channel=channel, control=7, value=127))
        out.send(Message("control_change", channel=channel, control=11, value=127))
