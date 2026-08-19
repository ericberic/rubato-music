"""Tests for live MIDI playback transformations."""

from __future__ import annotations

import pytest
from mido import Message, MetaMessage, MidiFile, MidiTrack

from aimusic.accompaniment.live_midi import (
    _cue_output_events,
    _timed_output_events,
    alignment_audition_events,
    midi_cue_events,
)
from aimusic.accompaniment.oguri import PIANO_TRACK_MARKER, oguri_movement_2
from aimusic.accompaniment.rehearsal_position import (
    rehearsal_cue_span,
    rehearsal_entry_for_measure,
    score_projection,
)


from tests.oguri_guard import requires_oguri_derived

pytestmark = requires_oguri_derived


def test_volume_scales_note_velocity_but_not_expression_controllers():
    track = MidiTrack()
    track.append(Message("control_change", control=7, value=100, time=0))
    track.append(Message("control_change", control=11, value=90, time=0))
    track.append(Message("note_on", note=60, velocity=80, time=0))

    events = _timed_output_events(
        track,
        ticks_per_beat=480,
        volume=0.75,
        max_seconds=None,
    )

    messages = [message for _, message in events]
    assert messages[0].value == 100
    assert messages[1].value == 90
    assert messages[2].velocity == 60


def test_tempo_scale_changes_event_wall_time_without_changing_symbolic_order():
    track = MidiTrack()
    track.append(Message("note_on", note=60, velocity=80, time=480))
    track.append(Message("note_off", note=60, velocity=0, time=480))

    normal = _timed_output_events(track, ticks_per_beat=480, volume=1.0, max_seconds=None)
    faster = _timed_output_events(
        track,
        ticks_per_beat=480,
        volume=1.0,
        max_seconds=None,
        tempo_scale=1.6,
    )

    assert [message.type for _, message in faster] == [message.type for _, message in normal]
    assert [time for time, _ in faster] == pytest.approx([time / 1.6 for time, _ in normal])


def test_timed_output_events_carries_active_notes_into_cue_window():
    track = MidiTrack()
    track.append(Message("program_change", program=48, time=0))
    track.append(Message("note_on", note=60, velocity=80, time=960))
    track.append(Message("note_off", note=60, velocity=0, time=960))
    track.append(Message("note_on", note=64, velocity=80, time=960))

    events = _timed_output_events(
        track,
        ticks_per_beat=480,
        volume=1.0,
        max_seconds=1.0,
        start_seconds=1.5,
    )

    assert [(round(event_time, 3), message.type) for event_time, message in events] == [
        (0.0, "program_change"),
        (0.0, "note_on"),
        (0.5, "note_off"),
    ]
    assert events[1][1].note == 60


def test_oguri_first_entry_four_second_cue_has_audible_carried_notes():
    movement = oguri_movement_2()
    events = _cue_output_events(
        movement.local_path,
        volume=0.75,
        start_seconds=movement.first_solo_entry_seconds - 4.0,
        duration_seconds=4.0,
        skip_track_name_contains=(PIANO_TRACK_MARKER,),
    )

    note_ons = [
        (event_time, message)
        for event_time, message in events
        if message.type == "note_on" and message.velocity > 0
    ]
    assert note_ons
    assert min(event_time for event_time, _ in note_ons) == 0.0
    assert _active_notes_after(events) == {}


def test_oguri_movement_playback_trims_technical_preroll_to_engraved_e_downbeat():
    movement = oguri_movement_2()
    events = _cue_output_events(
        movement.local_path,
        volume=1.0,
        start_seconds=movement.first_orchestra_entry_seconds,
        duration_seconds=0.1,
        skip_track_name_contains=(PIANO_TRACK_MARKER,),
    )

    note_ons = [
        (event_time, message.note)
        for event_time, message in events
        if message.type == "note_on" and message.velocity > 0
    ]
    assert note_ons[0] == (0.0, 64)  # E4 on printed m.1 beat one.


def test_oguri_default_cue_releases_sustained_orchestra_notes_after_entry():
    movement = oguri_movement_2()
    events = _cue_output_events(
        movement.local_path,
        volume=0.75,
        start_seconds=movement.first_solo_entry_seconds - movement.default_cue_seconds,
        duration_seconds=movement.default_cue_seconds,
        skip_track_name_contains=(PIANO_TRACK_MARKER,),
    )

    post_entry_releases = [
        event_time
        for event_time, message in events
        if event_time > movement.default_cue_seconds
        and (message.type == "note_off" or (message.type == "note_on" and message.velocity == 0))
    ]
    assert post_entry_releases
    assert max(post_entry_releases) <= movement.default_cue_seconds + 2.0
    assert _active_notes_after(events) == {}


def test_measure_53_pickup_cue_contains_the_preceding_orchestra_melody():
    movement = oguri_movement_2()
    projection = score_projection("chopin_op11", 2)
    entry = rehearsal_entry_for_measure(
        projection,
        projection.timeline.tick_at(52),
        movement.solo_reference_path,
    )
    cue_seconds = 8.0
    events = _cue_output_events(
        movement.local_path,
        volume=0.75,
        start_seconds=entry.entry_source_seconds - cue_seconds,
        duration_seconds=cue_seconds,
        skip_track_name_contains=(PIANO_TRACK_MARKER,),
    )

    attacks = [
        event_time
        for event_time, message in events
        if message.type == "note_on" and message.velocity > 0
    ]
    assert entry.entry_pitch == 71
    assert len(attacks) >= 20
    # Fresh orchestra attacks continue through the lead-in; this is not just
    # the B-natural carried from before the selected window.
    assert any(5.0 < event_time < cue_seconds for event_time in attacks)


def test_semantic_cue_keeps_orchestra_playing_after_piano_entry() -> None:
    movement = oguri_movement_2()
    projection = score_projection("chopin_op11", 2)
    cue = rehearsal_cue_span(
        projection,
        selected_score_tick=projection.timeline.tick_at(52),
        solo_reference_path=movement.solo_reference_path,
        first_solo_entry_seconds=movement.first_solo_entry_seconds,
    )

    events = _cue_output_events(
        movement.local_path,
        volume=0.75,
        start_seconds=cue.cue_start_source_seconds,
        duration_seconds=None,
        skip_track_name_contains=(PIANO_TRACK_MARKER,),
    )

    post_entry_attacks = [
        event_time
        for event_time, message in events
        if event_time > cue.lead_in_seconds and message.type == "note_on" and message.velocity > 0
    ]
    assert post_entry_attacks


def test_timed_output_events_starts_later_window_at_next_note_when_no_carry():
    track = MidiTrack()
    track.append(Message("program_change", program=48, time=0))
    track.append(Message("note_on", note=60, velocity=80, time=960))
    track.append(Message("note_off", note=60, velocity=0, time=960))
    track.append(Message("note_on", note=64, velocity=80, time=960))

    events = _timed_output_events(
        track,
        ticks_per_beat=480,
        volume=1.0,
        max_seconds=1.0,
        start_seconds=2.5,
    )

    assert [(round(event_time, 3), message.type) for event_time, message in events] == [
        (0.0, "program_change"),
        (0.5, "note_on"),
    ]
    assert events[-1][1].note == 64


def test_midi_cue_events_can_isolate_one_patch_compatible_track(tmp_path):
    midi = MidiFile(ticks_per_beat=480)
    for name, channel, note in (("Violini I", 2, 72), ("Violoncelli", 5, 48)):
        track = MidiTrack()
        track.append(MetaMessage("track_name", name=name, time=0))
        track.append(Message("note_on", channel=channel, note=note, velocity=80, time=0))
        track.append(Message("note_off", channel=channel, note=note, velocity=0, time=480))
        midi.tracks.append(track)
    source = tmp_path / "orchestra.mid"
    midi.save(source)

    events = midi_cue_events(
        source,
        volume=1.0,
        start_seconds=0.0,
        duration_seconds=1.0,
        include_track_name_contains=("Violini I",),
    )

    assert [message.note for _, message in events if message.type == "note_on"] == [72]


def test_midi_cue_events_exact_name_does_not_include_similarly_named_section(tmp_path):
    midi = MidiFile(ticks_per_beat=480)
    for name, note in (("Violini I", 72), ("Violini II", 67)):
        track = MidiTrack()
        track.append(MetaMessage("track_name", name=name, time=0))
        track.append(Message("note_on", note=note, velocity=80, time=0))
        midi.tracks.append(track)
    source = tmp_path / "orchestra.mid"
    midi.save(source)

    events = midi_cue_events(
        source,
        volume=1.0,
        start_seconds=0.0,
        duration_seconds=1.0,
        include_track_names=("Violini I",),
    )

    assert [message.note for _, message in events if message.type == "note_on"] == [72]


def test_midi_cue_events_rejects_both_track_selector_modes(tmp_path):
    source = tmp_path / "orchestra.mid"
    MidiFile().save(source)

    with pytest.raises(ValueError, match="mutually exclusive"):
        midi_cue_events(
            source,
            volume=1.0,
            start_seconds=0.0,
            duration_seconds=1.0,
            include_track_names=("Violini I",),
            include_track_name_contains=("Violini",),
        )


def test_midi_cue_events_rejects_unknown_included_track(tmp_path):
    midi = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    track.append(MetaMessage("track_name", name="Violini I", time=0))
    midi.tracks.append(track)
    source = tmp_path / "orchestra.mid"
    midi.save(source)

    with pytest.raises(ValueError, match="No MIDI tracks matched"):
        midi_cue_events(
            source,
            volume=1.0,
            start_seconds=0.0,
            duration_seconds=1.0,
            include_track_name_contains=("Oboe transport",),
        )


def test_alignment_audition_uses_one_clock_for_count_in_orchestra_and_beats(tmp_path):
    midi = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    track.append(MetaMessage("track_name", name="Violini I", time=0))
    track.append(Message("program_change", channel=2, program=49, time=0))
    track.append(Message("note_on", channel=2, note=64, velocity=80, time=0))
    track.append(Message("note_off", channel=2, note=64, velocity=0, time=1920))
    midi.tracks.append(track)
    source = tmp_path / "orchestra.mid"
    midi.save(source)

    events = alignment_audition_events(
        source,
        start_seconds=0.0,
        duration_seconds=2.0,
        beat_period_seconds=0.5,
        beat_offsets_seconds=(0.0, 0.5, 1.0, 1.5),
    )

    metronome_attacks = [
        event_time
        for event_time, message in events
        if message.type == "note_on" and message.channel == 9 and message.velocity > 0
    ]
    orchestra_attacks = [
        event_time
        for event_time, message in events
        if message.type == "note_on" and message.channel == 2 and message.velocity > 0
    ]
    assert metronome_attacks == pytest.approx([0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5])
    assert orchestra_attacks == pytest.approx([2.0])
    assert (
        max(
            message.velocity
            for _, message in events
            if message.type == "note_on" and message.channel == 9
        )
        == 34
    )
    assert next(
        index
        for index, (event_time, message) in enumerate(events)
        if event_time == 2.0 and message.type == "program_change"
    ) < next(
        index
        for index, (event_time, message) in enumerate(events)
        if event_time == 2.0 and message.type == "note_on" and message.channel == 9
    )


def test_alignment_audition_can_mute_the_metronome_without_muting_orchestra(tmp_path):
    midi = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    track.append(Message("note_on", channel=2, note=64, velocity=80, time=0))
    midi.tracks.append(track)
    source = tmp_path / "orchestra.mid"
    midi.save(source)

    events = alignment_audition_events(
        source,
        start_seconds=0.0,
        duration_seconds=1.0,
        beat_period_seconds=0.25,
        beat_offsets_seconds=(0.0, 0.25, 0.5, 0.75),
        metronome_volume=0.0,
    )

    assert not any(hasattr(message, "channel") and message.channel == 9 for _, message in events)
    assert any(
        message.type == "note_on" and message.channel == 2 and message.velocity > 0
        for _, message in events
    )


def _active_notes_after(events):
    active = {}
    for _, message in events:
        if message.type == "note_on" and message.velocity > 0:
            active[(message.channel, message.note)] = message.velocity
        elif message.type == "note_off" or (message.type == "note_on" and message.velocity == 0):
            active.pop((message.channel, message.note), None)
    return active
