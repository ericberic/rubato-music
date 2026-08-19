from __future__ import annotations

import threading
import time
from pathlib import Path

import mido
import pytest

from aimusic.accompaniment.midi_output import (
    DeadlineAccompanimentOutput,
    MidoAccompanimentOutput,
)
from aimusic.accompaniment.scheduler import ScheduledAccompanimentEvent
from aimusic.accompaniment.score_bundle import ScoreBundle
from aimusic.accompaniment.section_policy import AccompanimentMode

FIXTURE = Path("tests/fixtures/score_bundles/synthetic_accompaniment_capability")


class FakePort:
    def __init__(self) -> None:
        self.messages: list[mido.Message] = []
        self.closed = False

    def send(self, message: mido.Message) -> None:
        self.messages.append(message.copy())

    def close(self) -> None:
        self.closed = True


class FakeClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def now(self) -> float:
        return self.value


class DelayedNoteOnPort(FakePort):
    def __init__(self, clock: FakeClock, delay: float) -> None:
        super().__init__()
        self.clock = clock
        self.delay = delay

    def send(self, message: mido.Message) -> None:
        super().send(message)
        if message.type == "note_on":
            self.clock.value += self.delay


def test_mido_output_initializes_routes_and_schedules_note_off() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    port = FakePort()
    output = MidoAccompanimentOutput(
        "fake",
        bundle.instrument_map,
        port_factory=lambda _: port,
        now=lambda: 10.0,
        autostart=False,
    )
    event = ScheduledAccompanimentEvent(
        event=bundle.accompaniment_events[0],
        perf_time=10.0,
        section_mode=AccompanimentMode.FOLLOW,
        tempo_bpm=120.0,
    )

    output.send(event, sent_at=10.0)
    output.flush_due(10.99)
    assert [message.type for message in port.messages[:6]] == [
        "program_change",
        "control_change",
        "program_change",
        "control_change",
        "control_change",
        "note_on",
    ]
    assert [message.value for message in port.messages if message.type == "control_change"][:2] == [
        round(92 * 0.75),
        round(86 * 0.75),
    ]
    assert port.messages[-1].type == "note_on"

    output.flush_due(11.0)
    assert port.messages[-1].type == "note_off"
    assert port.messages[-1].note == event.event.pitch

    output.close()
    assert port.closed
    assert sum(message.type == "control_change" for message in port.messages) >= 34


def test_count_off_uses_explicit_percussion_channel_volume_and_trace() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    port = FakePort()
    observed = []
    output = MidoAccompanimentOutput(
        "fake",
        bundle.instrument_map,
        port_factory=lambda _: port,
        event_observer=observed.append,
        now=lambda: 10.0,
        master_volume=0.5,
        autostart=False,
    )

    output.send_count_off(
        mido.Message("note_on", channel=9, note=77, velocity=112),
        sent_at=10.0,
    )
    output.send_count_off(
        mido.Message("note_off", channel=9, note=77, velocity=0),
        sent_at=10.06,
    )

    cue_messages = [message for message in port.messages if getattr(message, "note", None) == 77]
    assert [message.type for message in cue_messages] == ["note_on", "note_off"]
    assert [message.channel for message in cue_messages] == [9, 9]
    cue_volume = next(
        message
        for message in port.messages
        if message.type == "control_change" and message.channel == 9
    )
    assert cue_volume.value == 50
    cue_rows = [row for row in observed if row.reason == "count_off_cue"]
    assert [row.action for row in cue_rows] == [
        "count_off_note_on",
        "count_off_note_off",
    ]
    assert all(row.channel == 9 and row.master_volume == 0.5 for row in cue_rows)
    output.close()


def test_count_off_route_can_be_explicitly_overridden_for_single_channel_vst() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    port = FakePort()
    output = MidoAccompanimentOutput(
        "fake",
        bundle.instrument_map,
        port_factory=lambda _: port,
        now=lambda: 10.0,
        count_off_channel=0,
        autostart=False,
    )

    output.send_count_off(
        mido.Message("note_on", channel=9, note=77, velocity=112),
        sent_at=10.0,
    )

    cue = next(message for message in port.messages if getattr(message, "note", None) == 77)
    assert cue.channel == 0
    output.close()


def test_retrigger_invalidates_older_note_off() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    clock = FakeClock(0.0)
    port = FakePort()
    output = MidoAccompanimentOutput(
        "fake",
        bundle.instrument_map,
        port_factory=lambda _: port,
        now=clock.now,
        autostart=False,
    )
    event = ScheduledAccompanimentEvent(
        event=bundle.accompaniment_events[0],
        perf_time=0.0,
        section_mode=AccompanimentMode.FOLLOW,
        tempo_bpm=120.0,
    )

    output.send(event, sent_at=0.0)
    clock.value = 0.5
    output.send(event, sent_at=0.5)
    note_off_count_after_retrigger = sum(message.type == "note_off" for message in port.messages)
    output.flush_due(1.0)
    assert (
        sum(message.type == "note_off" for message in port.messages)
        == note_off_count_after_retrigger
    )
    output.flush_due(1.5)
    assert sum(message.type == "note_off" for message in port.messages) == (
        note_off_count_after_retrigger + 1
    )

    output.close()


def test_output_uses_reference_duration_and_traces_actual_note_lifecycle() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    port = FakePort()
    observed = []
    output = MidoAccompanimentOutput(
        "fake",
        bundle.instrument_map,
        port_factory=lambda _: port,
        event_observer=observed.append,
        now=lambda: 10.0,
        autostart=False,
    )
    event = ScheduledAccompanimentEvent(
        event=bundle.accompaniment_events[0],
        perf_time=10.0,
        section_mode=AccompanimentMode.FOLLOW,
        tempo_bpm=30.0,
        duration_seconds=0.25,
    )

    output.send(event, sent_at=10.0)
    output.flush_due(10.24)
    assert [row.action for row in observed if row.action != "channel_volume"] == ["note_on"]
    output.flush_due(10.25)
    lifecycle = [row for row in observed if row.action != "channel_volume"]
    assert [row.action for row in lifecycle] == ["note_on", "note_off"]
    assert lifecycle[0].scheduled_note_off_time == 10.25
    assert lifecycle[0].velocity == event.event.velocity
    assert lifecycle[1].scheduled_note_off_time == 10.25
    assert lifecycle[1].output_lateness_ms == 0
    output.close()
    assert observed[-1].action == "panic"


def test_sounding_note_release_can_follow_a_later_tempo_estimate() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    port = FakePort()
    output = MidoAccompanimentOutput(
        "fake",
        bundle.instrument_map,
        port_factory=lambda _: port,
        now=lambda: 10.0,
        autostart=False,
    )
    event = ScheduledAccompanimentEvent(
        event=bundle.accompaniment_events[0],
        perf_time=10.0,
        section_mode=AccompanimentMode.FOLLOW,
        tempo_bpm=120.0,
        duration_seconds=0.25,
    )

    output.send(event, sent_at=10.0)
    assert output.retime_release(event.event.event_id, scheduled_at=10.75)
    output.flush_due(10.25)
    assert [message.type for message in port.messages].count("note_off") == 0
    output.flush_due(10.75)
    assert [message.type for message in port.messages].count("note_off") == 1
    assert not output.retime_release(event.event.event_id, scheduled_at=11.0)
    output.close()


def test_master_volume_scales_channel_mix_live_without_flattening_velocity() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    port = FakePort()
    observed = []
    output = MidoAccompanimentOutput(
        "fake",
        bundle.instrument_map,
        port_factory=lambda _: port,
        event_observer=observed.append,
        master_volume=0.5,
        autostart=False,
    )
    event = ScheduledAccompanimentEvent(
        event=bundle.accompaniment_events[0],
        perf_time=10.0,
        section_mode=AccompanimentMode.FOLLOW,
        tempo_bpm=120.0,
    )

    assert [
        (message.channel, message.value)
        for message in port.messages
        if message.type == "control_change"
    ] == [
        (0, round(92 * 0.5)),
        (1, round(86 * 0.5)),
        (9, round(100 * 0.5)),
    ]
    output.send(event, sent_at=10.0)
    output.set_master_volume(0.25, sent_at=10.1)

    note_on = next(message for message in port.messages if message.type == "note_on")
    assert note_on.velocity == event.event.velocity
    live_values = [
        (message.channel, message.value)
        for message in port.messages
        if message.type == "control_change"
    ][-3:]
    assert live_values == [
        (0, round(92 * 0.25)),
        (1, round(86 * 0.25)),
        (9, round(100 * 0.25)),
    ]
    controls = [row for row in observed if row.action == "channel_volume"]
    assert controls[-1].master_volume == 0.25
    assert controls[-1].reason == "performer_control"
    output.close()


def test_score_mix_policy_scales_cc7_without_changing_note_velocity() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    port = FakePort()
    event = ScheduledAccompanimentEvent(
        event=bundle.accompaniment_events[0],
        perf_time=10.0,
        section_mode=AccompanimentMode.FOLLOW,
        tempo_bpm=120.0,
    )
    output = MidoAccompanimentOutput(
        "fake",
        bundle.instrument_map,
        port_factory=lambda _: port,
        now=lambda: 10.0,
        master_volume=0.8,
        mix_level_resolver=lambda _part_id, _score_tick: 0.25,
        autostart=False,
    )

    output.send(event, sent_at=10.0)

    route = bundle.instrument_map[0]
    policy_cc7 = [
        message
        for message in port.messages
        if message.type == "control_change" and message.channel == route.channel
    ][-1]
    note_on = next(message for message in reversed(port.messages) if message.type == "note_on")
    assert policy_cc7.value == round((route.volume or 100) * 0.8 * 0.25)
    assert note_on.velocity == event.event.velocity
    output.close()


def test_score_mix_automation_updates_held_notes_without_new_note_on() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    port = FakePort()
    event = ScheduledAccompanimentEvent(
        event=bundle.accompaniment_events[0],
        perf_time=10.0,
        section_mode=AccompanimentMode.FOLLOW,
        tempo_bpm=60.0,
    )
    output = MidoAccompanimentOutput(
        "fake",
        bundle.instrument_map,
        port_factory=lambda _: port,
        now=lambda: 10.0,
        master_volume=1.0,
        mix_level_resolver=lambda _part_id, score_tick: 0.25 if score_tick < 2_000 else 0.75,
        autostart=False,
    )

    output.send(event, sent_at=10.0)
    note_on_count = sum(message.type == "note_on" for message in port.messages)
    route = next(item for item in bundle.instrument_map if item.part_id == event.event.part_id)

    output.apply_mix_automation(2_000, sent_at=11.0)

    assert sum(message.type == "note_on" for message in port.messages) == note_on_count
    values = [
        message.value
        for message in port.messages
        if message.type == "control_change"
        and message.control == 7
        and message.channel == route.channel
    ]
    assert values[-2:] == [
        round((route.volume or 100) * 0.25),
        round((route.volume or 100) * 0.75),
    ]
    output.close()


def test_backend_note_on_delay_is_traced_and_does_not_shorten_release() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    clock = FakeClock(10.0)
    port = DelayedNoteOnPort(clock, 0.04)
    observed = []
    output = MidoAccompanimentOutput(
        "fake",
        bundle.instrument_map,
        port_factory=lambda _: port,
        now=clock.now,
        event_observer=observed.append,
        autostart=False,
    )
    event = ScheduledAccompanimentEvent(
        event=bundle.accompaniment_events[0],
        perf_time=10.0,
        section_mode=AccompanimentMode.FOLLOW,
        tempo_bpm=120.0,
        duration_seconds=0.25,
    )

    output.send(event, sent_at=10.0)
    note_on = next(row for row in observed if row.action == "note_on")
    assert note_on.monotonic_time == 10.04
    assert note_on.output_lateness_ms == pytest.approx(40)
    assert note_on.send_call_duration_ms == pytest.approx(40)
    assert note_on.scheduled_note_off_time == pytest.approx(10.29)

    output.flush_due(10.28)
    assert not any(row.action == "note_off" for row in observed)
    output.flush_due(10.29)
    note_off = next(row for row in observed if row.action == "note_off")
    assert note_off.output_lateness_ms == pytest.approx(0)
    assert note_off.send_call_duration_ms == pytest.approx(0)
    output.close()


def test_note_off_anchors_to_actual_backend_completion_when_commit_was_late() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    clock = FakeClock(10.0)
    port = DelayedNoteOnPort(clock, 0.04)
    observed = []
    output = MidoAccompanimentOutput(
        "fake",
        bundle.instrument_map,
        port_factory=lambda _: port,
        now=clock.now,
        event_observer=observed.append,
        autostart=False,
    )
    event = ScheduledAccompanimentEvent(
        event=bundle.accompaniment_events[0],
        perf_time=9.9,
        section_mode=AccompanimentMode.FOLLOW,
        tempo_bpm=120.0,
        duration_seconds=0.25,
    )

    output.send(event, sent_at=9.9)

    note_on = next(row for row in observed if row.action == "note_on")
    assert note_on.monotonic_time == pytest.approx(10.04)
    assert note_on.scheduled_note_off_time == pytest.approx(10.29)
    output.close()


def test_deadline_output_waits_off_the_follower_thread_and_applies_advance() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    clock = FakeClock(10.0)
    port = FakePort()
    observed = []
    device = MidoAccompanimentOutput(
        "fake",
        bundle.instrument_map,
        port_factory=lambda _: port,
        now=clock.now,
        event_observer=observed.append,
        autostart=False,
    )
    output = DeadlineAccompanimentOutput(
        device,
        now=clock.now,
        output_advance_ms=10,
        autostart=False,
    )
    event = ScheduledAccompanimentEvent(
        event=bundle.accompaniment_events[0],
        perf_time=10.1,
        section_mode=AccompanimentMode.FOLLOW,
        tempo_bpm=120.0,
        duration_seconds=0.25,
    )

    output.send(event, sent_at=10.0)
    assert output.pending_count == 1
    assert not any(message.type == "note_on" for message in port.messages)

    clock.value = 10.089
    output.flush_due()
    assert not any(message.type == "note_on" for message in port.messages)

    clock.value = 10.09
    output.flush_due()
    assert output.pending_count == 0
    note_on = next(row for row in observed if row.action == "note_on")
    assert note_on.monotonic_time == pytest.approx(10.09)
    assert note_on.output_lateness_ms == pytest.approx(-10)
    assert note_on.committed_at == pytest.approx(10.0)
    output.close()


def test_deadline_output_retimes_release_before_or_after_note_on_delivery() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    clock = FakeClock(10.0)
    port = FakePort()
    device = MidoAccompanimentOutput(
        "fake",
        bundle.instrument_map,
        port_factory=lambda _: port,
        now=clock.now,
        autostart=False,
    )
    output = DeadlineAccompanimentOutput(device, now=clock.now, autostart=False)
    event = ScheduledAccompanimentEvent(
        event=bundle.accompaniment_events[0],
        perf_time=10.1,
        section_mode=AccompanimentMode.FOLLOW,
        tempo_bpm=120.0,
        duration_seconds=0.25,
    )

    output.send(event, sent_at=10.0)
    assert output.retime_release(event.event.event_id, scheduled_at=10.8)
    clock.value = 10.1
    output.flush_due()
    assert output.retime_release(event.event.event_id, scheduled_at=11.0)
    device.flush_due(10.8)
    assert [message.type for message in port.messages].count("note_off") == 0
    device.flush_due(11.0)
    assert [message.type for message in port.messages].count("note_off") == 1
    output.close()


def test_deadline_output_retunes_advance_for_events_committed_after_the_change() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    clock = FakeClock(10.0)
    port = FakePort()
    device = MidoAccompanimentOutput(
        "fake",
        bundle.instrument_map,
        port_factory=lambda _: port,
        now=clock.now,
        autostart=False,
    )
    output = DeadlineAccompanimentOutput(
        device,
        now=clock.now,
        output_advance_ms=0,
        autostart=False,
    )
    event = ScheduledAccompanimentEvent(
        event=bundle.accompaniment_events[0],
        perf_time=10.1,
        section_mode=AccompanimentMode.FOLLOW,
        tempo_bpm=120.0,
        duration_seconds=0.25,
    )

    # With a 40 ms advance the event's deadline moves earlier by 40 ms; it is
    # not yet due one tick before that new deadline, and due at it.
    output.set_output_advance(40.0)
    assert output.output_advance_ms == pytest.approx(40.0)
    output.send(event, sent_at=10.0)

    clock.value = 10.059
    output.flush_due()
    assert output.pending_count == 1

    clock.value = 10.06
    output.flush_due()
    assert output.pending_count == 0
    assert any(message.type == "note_on" for message in port.messages)
    output.close()


def test_deadline_output_queues_volume_outside_the_control_thread() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    clock = FakeClock(10.0)
    port = FakePort()
    device = MidoAccompanimentOutput(
        "fake",
        bundle.instrument_map,
        port_factory=lambda _: port,
        now=clock.now,
        autostart=False,
    )
    output = DeadlineAccompanimentOutput(
        device,
        now=clock.now,
        autostart=False,
    )
    initial_controls = len(
        [message for message in port.messages if message.type == "control_change"]
    )

    output.set_master_volume(0.25, sent_at=10.0)
    assert (
        len([message for message in port.messages if message.type == "control_change"])
        == initial_controls
    )

    output.flush_due(10.0)
    assert [
        (message.channel, message.value)
        for message in port.messages
        if message.type == "control_change"
    ][-3:] == [
        (0, round(92 * 0.25)),
        (1, round(86 * 0.25)),
        (9, round(100 * 0.25)),
    ]
    output.close()


def test_deadline_panic_barrier_cannot_emit_a_popped_command_after_panic() -> None:
    bundle = ScoreBundle.load(FIXTURE)
    port = FakePort()
    device = MidoAccompanimentOutput(
        "fake",
        bundle.instrument_map,
        port_factory=lambda _: port,
        now=lambda: 0.0,
        autostart=False,
    )
    output = DeadlineAccompanimentOutput(device, now=lambda: 0.0)
    event = ScheduledAccompanimentEvent(
        event=bundle.accompaniment_events[0],
        perf_time=0.0,
        section_mode=AccompanimentMode.FOLLOW,
        tempo_bpm=120.0,
    )

    # Hold the delegate boundary so the worker can pop the due command but
    # cannot emit it. Panic then advances the cancellation barrier.
    output._delegate_lock.acquire()
    output.send(event, sent_at=0.0)
    deadline = time.monotonic() + 1.0
    while output.pending_count:
        if time.monotonic() >= deadline:
            raise AssertionError("deadline worker did not pop the due command")
    panic_thread = threading.Thread(target=lambda: output.panic(sent_at=0.0, reason="test_panic"))
    panic_thread.start()
    while output._barrier == 0:
        if time.monotonic() >= deadline:
            raise AssertionError("panic did not advance the cancellation barrier")
    output._delegate_lock.release()
    panic_thread.join(timeout=1.0)

    assert not any(message.type == "note_on" for message in port.messages)
    output.close()
