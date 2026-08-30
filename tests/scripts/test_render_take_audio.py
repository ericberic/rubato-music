"""Tests for the take-audio reconstruction in scripts/render_take_audio.py.

Both bugs these guard against were found rendering a real take: a dropped
`retrigger_note_off` left an orchestra note droning for the whole piece, and
anchoring the solo on solo.mid's t=0 (a pedal press before the first note)
desynced it from the orchestra.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import mido
import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "render_take_audio.py"
_spec = importlib.util.spec_from_file_location("render_take_audio", _SCRIPT)
rta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rta)


def _balance(events: list[tuple[float, mido.Message]]) -> dict[tuple[int, int], int]:
    bal: dict[tuple[int, int], int] = {}
    for _t, m in events:
        key = (m.channel, m.note)
        if m.type == "note_on" and m.velocity > 0:
            bal[key] = bal.get(key, 0) + 1
        elif m.type == "note_off":
            bal[key] = bal.get(key, 0) - 1
    return bal


def test_retrigger_note_off_is_reconstructed_as_a_release() -> None:
    # A held pitch re-struck: note_on, retrigger_note_off (target_perf_time
    # absent -> monotonic_time), note_on, note_off. Dropping the retrigger left
    # this note droning to the end.
    rows = [
        {"type": "midi_output", "action": "note_on", "pitch": 63, "velocity": 40, "channel": 15, "target_perf_time": 10.0},
        {"type": "midi_output", "action": "retrigger_note_off", "pitch": 63, "velocity": 0, "channel": 15, "monotonic_time": 10.9},
        {"type": "midi_output", "action": "note_on", "pitch": 63, "velocity": 42, "channel": 15, "target_perf_time": 11.0},
        {"type": "midi_output", "action": "note_off", "pitch": 63, "velocity": 0, "channel": 15, "target_perf_time": 12.0},
    ]
    events = rta._orchestra_events(rows)
    assert _balance(events)[(15, 63)] == 0  # no stuck note
    offs = [(t, m) for t, m in events if m.type == "note_off" and m.note == 63]
    assert any(abs(t - 10.9) < 1e-9 for t, _ in offs)  # the retrigger became a real release


def test_safety_net_closes_and_warns_on_an_unbalanced_note(capsys) -> None:
    rows = [
        {"type": "midi_output", "action": "note_on", "pitch": 60, "velocity": 50, "channel": 2, "target_perf_time": 5.0},
    ]
    events = rta._orchestra_events(rows)
    assert _balance(events)[(2, 60)] == 0  # the net closed it
    assert "WARNING" in capsys.readouterr().out
    off = [t for t, m in events if m.type == "note_off" and m.note == 60]
    assert off and off[0] > 5.0  # closed just after the last event


def _solo_mid(pedal_lead_seconds: float) -> mido.MidiFile:
    """A solo.mid whose t=0 is a pedal press `pedal_lead_seconds` before note 1."""
    mid = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=500000, time=0))  # 480 ticks = 0.5s
    track.append(mido.Message("control_change", control=64, value=127, time=0))
    lead_ticks = round(pedal_lead_seconds / 0.5 * 480)
    track.append(mido.Message("note_on", note=60, velocity=64, time=lead_ticks))
    track.append(mido.Message("note_off", note=60, velocity=0, time=480))
    return mid


def test_solo_is_anchored_on_the_first_note_on_not_the_capture_origin() -> None:
    # Pedal pressed 1.0s before the first note. The first note's true perf_time
    # is 1000.0; the solo must land there, not 1000.0 + 1.0 (the t=0 anchor bug).
    solo = rta._solo_events([1000.0], _solo_mid(pedal_lead_seconds=1.0))
    first_on = next(t for t, m in solo if m.type == "note_on" and m.velocity > 0)
    assert first_on == pytest.approx(1000.0, abs=1e-6)
    pedal = next(t for t, m in solo if m.type == "control_change" and m.control == 64)
    assert pedal == pytest.approx(999.0, abs=1e-6)  # the lead pedal sits before the note


def test_combine_orders_releases_before_onsets_at_the_same_instant() -> None:
    orch = [
        (100.0, mido.Message("note_off", note=64, velocity=0, channel=2)),
        (100.0, mido.Message("note_on", note=67, velocity=50, channel=2)),
    ]
    events = rta._combine(orch, [])
    assert [m.type for _t, m in events] == ["note_off", "note_on"]
    assert events[0][0] == 0.0  # rebased to start at zero
