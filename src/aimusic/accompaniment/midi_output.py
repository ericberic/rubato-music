"""Mido-backed accompaniment rendering at the live-runtime boundary.

The scheduler deals in score events.  This module owns the device-specific
details: part-to-channel/program routing, note lifetimes, and panic.  Its port
factory and clock are injectable so no MIDI device or wall-clock sleep is
needed in deterministic tests.
"""

from __future__ import annotations

import dataclasses
import heapq
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol

import mido

from aimusic.accompaniment.runtime_contracts import MidiOutputTrace
from aimusic.accompaniment.scheduler import ScheduledAccompanimentEvent
from aimusic.accompaniment.score_bundle import InstrumentMapEntry


class MidiOutputPort(Protocol):
    """Small subset of a mido output port used by the renderer."""

    def send(self, message: mido.Message) -> None: ...

    def close(self) -> None: ...


MidiOutputFactory = Callable[[str], MidiOutputPort]
MixLevelResolver = Callable[[str, int], float]


@dataclass(order=True)
class _DeadlineCommand:
    deadline: float
    sequence: int
    kind: str = field(compare=False)
    committed_at: float = field(compare=False)
    barrier: int = field(compare=False)
    event: ScheduledAccompanimentEvent | None = field(default=None, compare=False)
    volume: float | None = field(default=None, compare=False)


class DeadlineAccompanimentOutput:
    """Deliver committed note-ons on a dedicated monotonic deadline worker.

    Score following and mutable retiming remain single-threaded. Once the
    scheduler freezes an event, this queue owns only immutable output commands,
    so a slow follower update cannot delay their final wait.
    """

    def __init__(
        self,
        delegate,
        *,
        now: Callable[[], float] = time.monotonic,
        output_advance_ms: float = 0.0,
        autostart: bool = True,
    ) -> None:
        if output_advance_ms < 0:
            raise ValueError("output_advance_ms must be non-negative")
        self._delegate = delegate
        self._now = now
        self._output_advance_seconds = output_advance_ms / 1000.0
        self._condition = threading.Condition()
        self._delegate_lock = threading.Lock()
        self._pending: list[_DeadlineCommand] = []
        self._sequence = 0
        self._barrier = 0
        self._closed = False
        self._error: BaseException | None = None
        self._worker: threading.Thread | None = None
        if autostart:
            self._worker = threading.Thread(
                target=self._deadline_worker,
                name="rubato-midi-deadline",
                daemon=True,
            )
            self._worker.start()

    def send(self, event: ScheduledAccompanimentEvent, *, sent_at: float) -> None:
        deadline = event.perf_time - self._output_advance_seconds
        self._enqueue(
            _DeadlineCommand(
                deadline=deadline,
                sequence=0,
                kind="note_on",
                committed_at=sent_at,
                barrier=0,
                event=event,
            )
        )

    def cancel_pending(self, event_ids: Iterable[str]) -> bool:
        """Cancel an entire committed chord before delivery, all-or-nothing."""

        requested = set(event_ids)
        if not requested:
            return True
        with self._condition:
            self._raise_if_unavailable()
            pending_ids = {
                command.event.event.event_id
                for command in self._pending
                if command.kind == "note_on" and command.event is not None
            }
            if not requested <= pending_ids:
                return False
            self._pending = [
                command
                for command in self._pending
                if not (
                    command.kind == "note_on"
                    and command.event is not None
                    and command.event.event.event_id in requested
                )
            ]
            heapq.heapify(self._pending)
            self._condition.notify_all()
            return True

    def retime_release(self, event_id: str, *, scheduled_at: float) -> bool:
        """Retime either a queued note-on's duration or its sounding release."""

        with self._condition:
            self._raise_if_unavailable()
            for command in self._pending:
                if (
                    command.kind == "note_on"
                    and command.event is not None
                    and command.event.event.event_id == event_id
                ):
                    command.event = dataclasses.replace(
                        command.event,
                        duration_seconds=max(0.001, scheduled_at - command.deadline),
                    )
                    return True
        # Do not hold the queue condition while crossing the delegate lock:
        # `_execute` takes these locks in the opposite temporal order.
        with self._delegate_lock:
            return self._delegate.retime_release(event_id, scheduled_at=scheduled_at)

    def set_output_advance(self, output_advance_ms: float) -> None:
        """Retune the hardware output-advance mid-run for by-ear calibration.

        Only affects events committed after this call: already-queued commands
        keep the deadline computed at their send time. Safe to change live as
        long as the advance never exceeds the scheduler's dispatch horizon (the
        engine enforces that), so an event is always transferred to this worker
        before its advanced deadline.
        """

        if output_advance_ms < 0:
            raise ValueError("output_advance_ms must be non-negative")
        with self._condition:
            self._output_advance_seconds = output_advance_ms / 1000.0

    @property
    def output_advance_ms(self) -> float:
        with self._condition:
            return self._output_advance_seconds * 1000.0

    def set_master_volume(self, volume: float, *, sent_at: float) -> None:
        if not 0 <= volume <= 1:
            raise ValueError("volume must be between 0 and 1")
        self._enqueue(
            _DeadlineCommand(
                deadline=sent_at,
                sequence=0,
                kind="volume",
                committed_at=sent_at,
                barrier=0,
                volume=volume,
            )
        )

    def apply_mix_automation(self, score_tick: int, *, sent_at: float) -> None:
        """Apply the score-continuous mix position outside note scheduling."""

        with self._delegate_lock:
            self._delegate.apply_mix_automation(score_tick, sent_at=sent_at)

    def panic(self, *, sent_at: float, reason: str) -> None:
        with self._condition:
            self._barrier += 1
            self._pending.clear()
            self._condition.notify_all()
        with self._delegate_lock:
            self._delegate.panic(sent_at=sent_at, reason=reason)

    def flush_due(self, now: float | None = None) -> None:
        """Execute due commands deterministically when autostart is disabled."""

        deadline = self._now() if now is None else now
        while True:
            with self._condition:
                self._raise_if_unavailable()
                if not self._pending or self._pending[0].deadline > deadline:
                    return
                command = heapq.heappop(self._pending)
            self._execute(command)

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._barrier += 1
            self._pending.clear()
            self._condition.notify_all()
        if self._worker is not None:
            self._worker.join(timeout=1.0)
        with self._delegate_lock:
            self._delegate.close()

    @property
    def pending_count(self) -> int:
        with self._condition:
            return len(self._pending)

    @property
    def next_deadline(self) -> float | None:
        """Earliest immutable output deadline, for deterministic event loops."""

        with self._condition:
            return self._pending[0].deadline if self._pending else None

    def _enqueue(self, command: _DeadlineCommand) -> None:
        with self._condition:
            self._raise_if_unavailable()
            self._sequence += 1
            command.sequence = self._sequence
            command.barrier = self._barrier
            heapq.heappush(self._pending, command)
            self._condition.notify_all()

    def _raise_if_unavailable(self) -> None:
        if self._error is not None:
            raise RuntimeError("MIDI deadline worker failed") from self._error
        if self._closed:
            raise RuntimeError("MIDI deadline output is closed")

    def _execute(self, command: _DeadlineCommand) -> None:
        with self._delegate_lock:
            with self._condition:
                if command.barrier != self._barrier or self._closed:
                    return
            if command.kind == "note_on":
                assert command.event is not None
                self._delegate.send(
                    dataclasses.replace(
                        command.event,
                        committed_at=command.committed_at,
                    ),
                    sent_at=command.deadline,
                )
            elif command.kind == "volume":
                assert command.volume is not None
                self._delegate.set_master_volume(
                    command.volume,
                    sent_at=command.committed_at,
                )
            else:  # pragma: no cover - internal construction is exhaustive
                raise AssertionError(f"Unknown deadline command: {command.kind}")

    def _deadline_worker(self) -> None:
        try:
            while True:
                with self._condition:
                    if self._closed:
                        return
                    if not self._pending:
                        self._condition.wait()
                        continue
                    delay = self._pending[0].deadline - self._now()
                    if delay > 0:
                        self._condition.wait(timeout=delay)
                        continue
                    command = heapq.heappop(self._pending)
                self._execute(command)
        except BaseException as exc:
            with self._condition:
                self._error = exc
                self._pending.clear()
                self._condition.notify_all()


class MidoAccompanimentOutput:
    """Render scheduled score events to one MIDI output port.

    ``DeadlineAccompanimentOutput`` invokes this renderer at the immutable
    output deadline. This layer serializes actual device writes, emits the
    note-on, and queues its corresponding note-off. Re-triggering a
    channel/pitch invalidates the older pending note-off so it cannot silence
    the newer note.
    """

    def __init__(
        self,
        output_name: str,
        instrument_map: tuple[InstrumentMapEntry, ...],
        *,
        port_factory: MidiOutputFactory = mido.open_output,
        now: Callable[[], float] = time.monotonic,
        event_observer: Callable[[MidiOutputTrace], None] | None = None,
        master_volume: float = 0.75,
        count_off_channel: int = 9,
        mix_level_resolver: MixLevelResolver | None = None,
        autostart: bool = True,
    ) -> None:
        if not 0 <= master_volume <= 1:
            raise ValueError("master_volume must be between 0 and 1")
        if not 0 <= count_off_channel <= 15:
            raise ValueError("count_off_channel must be between 0 and 15")
        self.output_name = output_name
        self._routing = {entry.part_id: entry for entry in instrument_map}
        self._channel_base_volume: dict[int, int] = {}
        channel_parts: dict[int, list[str]] = {}
        for route in instrument_map:
            self._channel_base_volume.setdefault(
                route.channel, route.volume if route.volume is not None else 100
            )
            channel_parts.setdefault(route.channel, []).append(route.part_id)
        self._channel_part_ids = {
            channel: tuple(part_ids) for channel, part_ids in channel_parts.items()
        }
        self._master_volume = master_volume
        self._mix_level_resolver = mix_level_resolver
        self._channel_mix_volume: dict[int, float] = {}
        self._channel_last_volume_value: dict[int, int] = {}
        # The cue route is explicit instead of borrowing the first orchestral
        # part. Channel 9 is the Yamaha/GM percussion default; a single-channel
        # VST may opt into its own route through RuntimeConfig.
        self._count_off_channel = count_off_channel
        self._channel_base_volume.setdefault(count_off_channel, 100)
        self._port = port_factory(output_name)
        self._port_lock = threading.Lock()
        self._now = now
        self._event_observer = event_observer or (lambda event: None)
        self._condition = threading.Condition()
        self._pending: list[tuple[float, int, int, int, str]] = []
        self._tokens: dict[tuple[int, int], tuple[int, str]] = {}
        self._next_token = 0
        self._closed = False
        self._worker: threading.Thread | None = None
        self._initialize_channels()
        if autostart:
            self._worker = threading.Thread(target=self._note_off_worker, daemon=True)
            self._worker.start()

    def send(self, event: ScheduledAccompanimentEvent, *, sent_at: float) -> None:
        score_event = event.event
        if score_event.pitch is None:
            return
        route = self._routing.get(score_event.part_id)
        if route is None:
            raise ValueError(f"No instrument route for part {score_event.part_id}")
        velocity = score_event.velocity if score_event.velocity is not None else 64
        if self._mix_level_resolver is not None:
            self.apply_mix_automation(round(score_event.beat * 960), sent_at=sent_at)
        key = (route.channel, score_event.pitch)
        observations: list[MidiOutputTrace] = []
        with self._condition:
            self._require_open()
            # A repeated note needs an explicit release before its new attack.
            if key in self._tokens:
                with self._port_lock:
                    self._port.send(
                        mido.Message(
                            "note_off",
                            channel=route.channel,
                            note=score_event.pitch,
                        )
                    )
                _, prior_event_id = self._tokens[key]
                observations.append(
                    self._trace(
                        "retrigger_note_off",
                        event_id=prior_event_id,
                        channel=route.channel,
                        pitch=score_event.pitch,
                    )
                )
            self._next_token += 1
            token = self._next_token
            self._tokens[key] = (token, score_event.event_id)
            send_started = self._now()
            with self._port_lock:
                self._port.send(
                    mido.Message(
                        "note_on",
                        channel=route.channel,
                        note=score_event.pitch,
                        velocity=max(0, min(127, velocity)),
                    )
                )
            actually_sent_at = self._now()
            duration_seconds = event.duration_seconds
            if duration_seconds is None:
                duration_seconds = score_event.duration_beats * 60.0 / event.tempo_bpm
            # Anchor the release to the actual adapter call, including any
            # blocking time spent inside the MIDI backend. Otherwise a slow
            # note-on send shortens the audible note by that same delay.
            note_off_time = actually_sent_at + duration_seconds
            heapq.heappush(
                self._pending,
                (
                    note_off_time,
                    route.channel,
                    score_event.pitch,
                    token,
                    score_event.event_id,
                ),
            )
            observations.append(
                self._trace(
                    "note_on",
                    event_id=score_event.event_id,
                    channel=route.channel,
                    pitch=score_event.pitch,
                    velocity=max(0, min(127, velocity)),
                    target_perf_time=event.perf_time,
                    requested_send_time=sent_at,
                    scheduled_note_off_time=note_off_time,
                    output_lateness_ms=(actually_sent_at - event.perf_time) * 1000,
                    send_call_duration_ms=(actually_sent_at - send_started) * 1000,
                    at=actually_sent_at,
                    committed_at=event.committed_at,
                )
            )
            self._condition.notify_all()
        self._publish(observations)

    def send_count_off(self, message: mido.Message, *, sent_at: float) -> None:
        """Render a count-off cue through the owned device/routing boundary."""

        if message.type not in {"note_on", "note_off"}:
            raise ValueError("count-off output accepts note_on/note_off only")
        routed = message.copy(channel=self._count_off_channel)
        send_started = self._now()
        with self._condition:
            self._require_open()
            with self._port_lock:
                self._port.send(routed)
        actually_sent_at = self._now()
        self._event_observer(
            self._trace(
                (
                    "count_off_note_on"
                    if routed.type == "note_on" and routed.velocity > 0
                    else "count_off_note_off"
                ),
                channel=routed.channel,
                pitch=routed.note,
                velocity=routed.velocity,
                requested_send_time=sent_at,
                send_call_duration_ms=(actually_sent_at - send_started) * 1000,
                master_volume=self._master_volume,
                reason="count_off_cue",
                at=actually_sent_at,
            )
        )

    def cancel_pending(self, event_ids: Iterable[str]) -> bool:
        """Direct MIDI sends are already audible and cannot be recalled."""

        return not tuple(event_ids)

    def retime_release(self, event_id: str, *, scheduled_at: float) -> bool:
        """Move one still-sounding note-off to its latest score-derived time."""

        with self._condition:
            active = next(
                ((key, token) for key, token in self._tokens.items() if token[1] == event_id),
                None,
            )
            if active is None:
                return False
            (channel, pitch), (token, _) = active
            for index, item in enumerate(self._pending):
                _old_time, queued_channel, queued_pitch, queued_token, queued_event_id = item
                if (
                    queued_channel == channel
                    and queued_pitch == pitch
                    and queued_token == token
                    and queued_event_id == event_id
                ):
                    self._pending[index] = (
                        scheduled_at,
                        channel,
                        pitch,
                        token,
                        event_id,
                    )
                    heapq.heapify(self._pending)
                    self._condition.notify_all()
                    return True
            return False

    def flush_due(self, now: float | None = None) -> None:
        """Emit due note-offs; public for deterministic clock-driven tests."""

        deadline = self._now() if now is None else now
        observations: list[MidiOutputTrace] = []
        with self._condition:
            while self._pending and self._pending[0][0] <= deadline:
                scheduled_at, channel, pitch, token, event_id = heapq.heappop(self._pending)
                key = (channel, pitch)
                active = self._tokens.get(key)
                if active is None or active[0] != token:
                    continue
                send_started = self._now()
                with self._port_lock:
                    self._port.send(mido.Message("note_off", channel=channel, note=pitch))
                adapter_returned_at = self._now()
                actually_sent_at = deadline if now is not None else adapter_returned_at
                del self._tokens[key]
                observations.append(
                    self._trace(
                        "note_off",
                        event_id=event_id,
                        channel=channel,
                        pitch=pitch,
                        scheduled_note_off_time=scheduled_at,
                        output_lateness_ms=(actually_sent_at - scheduled_at) * 1000,
                        send_call_duration_ms=max(
                            0.0,
                            (adapter_returned_at - send_started) * 1000,
                        ),
                        at=actually_sent_at,
                    )
                )
        self._publish(observations)

    def set_master_volume(self, volume: float, *, sent_at: float) -> None:
        """Apply the performer mix control to every accompaniment channel.

        CC7 owns the stable orchestra/piano balance.  Per-note velocity remains
        untouched so the source orchestration keeps its internal dynamics.
        Because CC7 also affects sounding notes, a live fader move is audible
        immediately rather than only on the next attack.
        """

        if not 0 <= volume <= 1:
            raise ValueError("volume must be between 0 and 1")
        with self._condition:
            self._require_open()
            self._master_volume = volume
        self._send_channel_volumes(
            requested_at=sent_at,
            reason="performer_control",
            master_volume=volume,
        )

    def set_event_observer(self, observer: Callable[[MidiOutputTrace], None] | None) -> None:
        """Move telemetry to a new run without reopening the owned MIDI port."""

        with self._condition:
            self._require_open()
            self._event_observer = observer or (lambda event: None)

    def resend_channel_volumes(self, *, sent_at: float, reason: str) -> None:
        """Force current CC7 state after a downstream synth reconnects."""

        with self._condition:
            self._require_open()
            self._channel_last_volume_value.clear()
        self._send_channel_volumes(requested_at=sent_at, reason=reason)

    def apply_mix_automation(self, score_tick: int, *, sent_at: float) -> None:
        """Emit quantized CC7 changes for every score-routed channel.

        The live control loop calls this independently of note-on events, so a
        held orchestral note follows an authored swell through rests and ties.
        Messages are suppressed until the final quantized CC7 value changes.
        """

        if score_tick < 0:
            raise ValueError("score_tick must be non-negative")
        resolver = self._mix_level_resolver
        if resolver is None:
            return
        levels: dict[int, float] = {}
        for channel, part_ids in self._channel_part_ids.items():
            part_levels = tuple(resolver(part_id, score_tick) for part_id in part_ids)
            if any(not 0 <= level <= 1 for level in part_levels):
                raise ValueError("mix level resolver must return a value between 0 and 1")
            levels[channel] = max(part_levels, default=0.0)
        with self._condition:
            self._require_open()
            self._channel_mix_volume.update(levels)
        for channel in sorted(levels):
            self._send_channel_volume(
                channel,
                requested_at=sent_at,
                reason="mix_policy",
            )

    def panic(self, *, sent_at: float, reason: str) -> None:
        observation: MidiOutputTrace | None = None
        with self._condition:
            if self._closed:
                return
            self._pending.clear()
            self._tokens.clear()
            with self._port_lock:
                for channel in range(16):
                    self._port.send(
                        mido.Message(
                            "control_change",
                            channel=channel,
                            control=123,
                            value=0,
                        )
                    )
                    self._port.send(
                        mido.Message(
                            "control_change",
                            channel=channel,
                            control=120,
                            value=0,
                        )
                    )
            observation = self._trace("panic", reason=reason, at=sent_at)
            self._condition.notify_all()
        self._publish([observation])

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
        self.panic(sent_at=self._now(), reason="output_close")
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if self._worker is not None:
            self._worker.join(timeout=1.0)
        with self._port_lock:
            self._port.close()

    def __enter__(self) -> "MidoAccompanimentOutput":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _initialize_channels(self) -> None:
        initialized_channels: set[int] = set()
        for route in sorted(self._routing.values(), key=lambda item: item.channel):
            with self._port_lock:
                self._port.send(
                    mido.Message(
                        "program_change",
                        channel=route.channel,
                        program=route.program,
                    )
                )
            if route.channel not in initialized_channels:
                self._send_channel_volume(
                    route.channel,
                    requested_at=self._now(),
                    reason="runtime_initialization",
                )
                initialized_channels.add(route.channel)
        if self._count_off_channel not in initialized_channels:
            self._send_channel_volume(
                self._count_off_channel,
                requested_at=self._now(),
                reason="runtime_initialization",
            )

    def _send_channel_volumes(
        self,
        *,
        requested_at: float,
        reason: str,
        master_volume: float | None = None,
    ) -> None:
        for channel in sorted(self._channel_base_volume):
            self._send_channel_volume(
                channel,
                requested_at=requested_at,
                reason=reason,
                master_volume=master_volume,
            )

    def _send_channel_volume(
        self,
        channel: int,
        *,
        requested_at: float,
        reason: str,
        master_volume: float | None = None,
    ) -> None:
        base_volume = self._channel_base_volume[channel]
        applied_volume = self._master_volume if master_volume is None else master_volume
        mix_volume = self._channel_mix_volume.get(channel, 1.0)
        value = max(0, min(127, round(base_volume * applied_volume * mix_volume)))
        if self._channel_last_volume_value.get(channel) == value:
            return
        self._channel_last_volume_value[channel] = value
        send_started = self._now()
        with self._port_lock:
            self._port.send(
                mido.Message(
                    "control_change",
                    channel=channel,
                    control=7,
                    value=value,
                )
            )
        actually_sent_at = self._now()
        self._event_observer(
            self._trace(
                "channel_volume",
                channel=channel,
                control=7,
                value=value,
                requested_send_time=requested_at,
                send_call_duration_ms=(actually_sent_at - send_started) * 1000,
                master_volume=applied_volume,
                reason=reason,
                at=actually_sent_at,
            )
        )

    def _note_off_worker(self) -> None:
        while True:
            with self._condition:
                if self._closed:
                    return
                if not self._pending:
                    self._condition.wait()
                    continue
                delay = self._pending[0][0] - self._now()
                if delay > 0:
                    self._condition.wait(timeout=delay)
                    continue
            self.flush_due()

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("MIDI accompaniment output is closed")

    def _trace(
        self,
        action: str,
        *,
        event_id: str | None = None,
        channel: int | None = None,
        pitch: int | None = None,
        velocity: int | None = None,
        control: int | None = None,
        value: int | None = None,
        target_perf_time: float | None = None,
        requested_send_time: float | None = None,
        scheduled_note_off_time: float | None = None,
        output_lateness_ms: float | None = None,
        send_call_duration_ms: float | None = None,
        master_volume: float | None = None,
        reason: str | None = None,
        at: float | None = None,
        committed_at: float | None = None,
    ) -> MidiOutputTrace:
        return MidiOutputTrace(
            type="midi_output",
            monotonic_time=self._now() if at is None else at,
            action=action,
            event_id=event_id,
            channel=channel,
            pitch=pitch,
            velocity=velocity,
            control=control,
            value=value,
            target_perf_time=target_perf_time,
            requested_send_time=requested_send_time,
            scheduled_note_off_time=scheduled_note_off_time,
            output_lateness_ms=output_lateness_ms,
            send_call_duration_ms=send_call_duration_ms,
            master_volume=master_volume,
            reason=reason,
            committed_at=committed_at,
        )

    def _publish(self, observations: list[MidiOutputTrace]) -> None:
        for observation in observations:
            self._event_observer(observation)


__all__ = [
    "DeadlineAccompanimentOutput",
    "MidiOutputFactory",
    "MidiOutputPort",
    "MidoAccompanimentOutput",
]
