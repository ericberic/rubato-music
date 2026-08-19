"""Crash-isolated real-time VST instrument workers for named audio zones.

The conductor transfers immutable scheduled notes roughly 100 ms before their
acoustic target. Each BBCSO patch renders in an isolated process; one zone
mixer sums those blocks and writes them through a single PortAudio/CoreAudio
stream. No WAV file exists in this path.

Pedalboard is imported only inside the child process.  Base tests can therefore
exercise routing, timing, and telemetry without the optional audio dependency or
a physical device.
"""

from __future__ import annotations

import heapq
import math
import multiprocessing as mp
import os
import queue
import resource
import signal
import statistics
import threading
import time
import traceback
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import mido
import numpy as np

from aimusic.accompaniment.runtime_contracts import (
    AudioWorkerTrace,
    MidiOutputTrace,
    MixStateTrace,
    TelemetryLevel,
)
from aimusic.accompaniment.runtime_io import TraceSink
from aimusic.accompaniment.scheduler import ScheduledAccompanimentEvent
from aimusic.audio.live_config import LiveVstZoneConfig, VstInstrumentBinding
from aimusic.audio.mix_gauges import MixGaugeBlock
from aimusic.mixing.policy import MixPolicy


class LiveVstError(RuntimeError):
    """The live renderer could not start or failed during a run."""

    def __init__(self, message: str, *, worker_exit_code: int | None = None) -> None:
        super().__init__(message)
        self.worker_exit_code = worker_exit_code


@dataclass(frozen=True)
class _NoteCommand:
    event_id: str
    part_id: str
    pitch: int
    velocity: int
    target_acoustic_time: float
    duration_seconds: float
    committed_at: float


@dataclass(order=True)
class _TimedMidi:
    target_device_time: float
    sequence: int
    instrument_id: str = field(compare=False)
    message: bytes = field(compare=False)
    event_id: str = field(compare=False)
    received_at: float = field(compare=False)
    kind: str = field(compare=False)
    barrier_id: str | None = field(default=None, compare=False)


@dataclass
class _WindowStats:
    started_at: float
    render_ms: list[float] = field(default_factory=list)
    write_ms: list[float] = field(default_factory=list)
    command_to_render_ms: list[float] = field(default_factory=list)
    plugin_ms: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    late_events: int = 0
    underruns: int = 0
    queue_high_watermark: int = 0


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _rss_mb() -> float:
    value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # macOS reports bytes; Linux reports KiB.
    return value / (1024 * 1024) if value > 10_000_000 else value / 1024


def _queue_depth(value: Any) -> int:
    """Return best-effort queue depth; macOS cannot inspect mp.Queue semaphores."""

    try:
        return int(value.qsize())
    except (AttributeError, NotImplementedError):
        return 0


def _bindings_for_part(
    bindings: tuple[VstInstrumentBinding, ...], part_id: str
) -> tuple[VstInstrumentBinding, ...]:
    exact = tuple(binding for binding in bindings if part_id in binding.stem_ids)
    if exact:
        return exact
    return tuple(binding for binding in bindings if "orchestra" in binding.stem_ids)


def _score_tick_at(acoustic_time: float, anchors: tuple[tuple[float, int], ...]) -> int:
    if not anchors:
        return 0
    if len(anchors) == 1:
        return anchors[0][1]
    (left_time, left_tick), (right_time, right_tick) = anchors[-2:]
    elapsed = right_time - left_time
    if elapsed <= 0:
        return right_tick
    ticks_per_second = (right_tick - left_tick) / elapsed
    return max(0, round(right_tick + (acoustic_time - right_time) * ticks_per_second))


def _instrument_gain(
    policy: MixPolicy,
    zone_id: str,
    binding: VstInstrumentBinding,
    score_tick: int,
) -> float:
    values = []
    for stem_id in binding.stem_ids:
        if stem_id == "orchestra":
            values.append(policy.audio_gain_at(zone_id, stem_id, score_tick))
        else:
            values.append(policy.audio_gain_for_part_at(zone_id, stem_id, score_tick))
    return max(values, default=0.0)


def _master_gain(fader_position: float) -> float:
    """Map the performer fader to a safer, approximately perceptual gain."""

    return fader_position * fader_position


def _load_plugins(
    zone: LiveVstZoneConfig,
    *,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:  # pragma: no cover - exercised by opt-in renderer tests
    import pedalboard

    plugins: dict[str, Any] = {}
    total = len(zone.instruments)
    for index, binding in enumerate(zone.instruments):
        if progress is not None:
            progress(
                {
                    "state": "loading",
                    "zone_id": zone.zone_id,
                    "instrument_id": binding.instrument_id,
                    "loaded": index,
                    "total": total,
                }
            )
        plugin = pedalboard.load_plugin(
            str(binding.plugin_path),
            initialization_timeout=zone.plugin_initialization_timeout_seconds,
        )
        if not plugin.is_instrument:
            raise ValueError(f"plug-in is not an instrument: {binding.plugin_path}")
        plugin.raw_state = binding.plugin_state_path.read_bytes()
        block_seconds = zone.block_size / zone.sample_rate
        for _ in range(zone.warmup_blocks):
            plugin.process(
                [],
                duration=block_seconds,
                sample_rate=zone.sample_rate,
                num_channels=2,
                buffer_size=zone.block_size,
                reset=False,
            )
        plugins[binding.instrument_id] = plugin
    return plugins


class _SoundDeviceOutput:
    """Adapter from Rubato's planar blocks to sounddevice's interleaved stream."""

    def __init__(self, zone: LiveVstZoneConfig) -> None:
        import sounddevice as sd

        self._sample_rate = zone.sample_rate
        self._stream = sd.OutputStream(
            device=_sounddevice_output_index(zone.output_device_name),
            samplerate=zone.sample_rate,
            blocksize=zone.block_size,
            channels=2,
            dtype="float32",
            latency="low",
        )

    def __enter__(self) -> _SoundDeviceOutput:
        self._stream.start()
        return self

    def write(self, block: np.ndarray, sample_rate: int) -> None:
        if sample_rate != self._sample_rate:
            raise ValueError(
                f"audio block sample rate {sample_rate} does not match {self._sample_rate}"
            )
        planar = np.asarray(block, dtype=np.float32)
        self._stream.write(np.ascontiguousarray(planar.T))

    def close(self) -> None:
        try:
            self._stream.stop()
        finally:
            self._stream.close()


def _sounddevice_output_index(name: str) -> int:
    import sounddevice as sd

    devices = tuple(sd.query_devices())
    for index, device in enumerate(devices):
        if device["name"] == name and int(device["max_output_channels"]) >= 2:
            return index
    available = tuple(
        str(device["name"])
        for device in devices
        if int(device["max_output_channels"]) >= 2
    )
    names = ", ".join(available) if available else "none"
    raise ValueError(f"audio output {name!r} is unavailable; available outputs: {names}")


def _open_audio_stream(
    zone: LiveVstZoneConfig,
) -> Any:  # pragma: no cover - exercised by opt-in renderer tests
    return _SoundDeviceOutput(zone)


def _validate_audio_device(
    zone: LiveVstZoneConfig,
) -> None:  # pragma: no cover - exercised by opt-in renderer tests
    import sounddevice as sd

    device_index = _sounddevice_output_index(zone.output_device_name)
    sd.check_output_settings(
        device=device_index,
        channels=2,
        dtype="float32",
        samplerate=zone.sample_rate,
    )


def _worker_main(
    zone: LiveVstZoneConfig,
    policy: MixPolicy,
    commands: Any,
    transport: Any,
    responses: Any,
    rendered_until: Any,
    plugin_loader: Callable[[LiveVstZoneConfig], dict[str, Any]] = _load_plugins,
    stream_factory: Callable[[LiveVstZoneConfig], Any] = _open_audio_stream,
    mix_gauges: MixGaugeBlock | None = None,
    telemetry_level: TelemetryLevel = TelemetryLevel.COUNTERS,
    audio_blocks: Any | None = None,
) -> None:
    """Child entry point. Plug-ins stay isolated; zone audio may be deferred."""

    block_seconds = zone.block_size / zone.sample_rate
    deferred_audio_output = audio_blocks is not None
    audio_queue = (
        audio_blocks
        if deferred_audio_output
        else queue.Queue(zone.prefill_blocks + 2)
    )
    writer_measurements: queue.Queue[tuple[str, float]] = queue.Queue()
    writer_stop = threading.Event()
    writer_error: list[BaseException] = []
    stream = None
    writer: threading.Thread | None = None
    collect_counters = telemetry_level != TelemetryLevel.OFF
    startup_stage = "validating_audio_device"

    def publish_startup(payload: dict[str, Any]) -> None:
        responses.put(("startup", payload))
        if telemetry_level != TelemetryLevel.OFF:
            responses.put(
                (
                    "trace",
                    AudioWorkerTrace(
                        type="audio_worker",
                        monotonic_time=time.monotonic(),
                        zone_id=zone.zone_id,
                        action="startup",
                        instrument_id=payload.get("instrument_id"),
                        block_frames=zone.block_size,
                        sample_rate=zone.sample_rate,
                        rss_mb=_rss_mb(),
                        detail=str(payload.get("state")),
                    ).model_dump(mode="python"),
                )
            )

    try:
        if stream_factory is _open_audio_stream:
            _validate_audio_device(zone)
        startup_stage = "loading_plugins"
        publish_startup(
            {
                "state": "loading",
                "zone_id": zone.zone_id,
                "instrument_id": None,
                "loaded": 0,
                "total": len(zone.instruments),
            }
        )
        if plugin_loader is _load_plugins:
            plugins = plugin_loader(
                zone,
                progress=publish_startup,
            )
        else:
            plugins = plugin_loader(zone)
        startup_stage = "opening_audio"
        publish_startup(
            {
                "state": "opening_audio",
                "zone_id": zone.zone_id,
                "instrument_id": None,
                "loaded": len(zone.instruments),
                "total": len(zone.instruments),
            }
        )
        if deferred_audio_output:
            # BBCSO plug-ins remain isolated, but a zone must own exactly one
            # CoreAudio stream. Multiple concurrent HDMI streams can block
            # their writers indefinitely. The parent zone mixer opens that
            # one stream after every patch host reaches this barrier.
            origin = 0.0
        else:
            stream = stream_factory(zone)
            stream.__enter__()
            origin = time.monotonic() + max(0.05, block_seconds * zone.prefill_blocks)

        def write_blocks() -> None:
            try:
                delay = origin - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                while not writer_stop.is_set():
                    try:
                        block = audio_queue.get(timeout=block_seconds)
                    except queue.Empty:
                        writer_measurements.put(("underrun", 0.0))
                        block = np.zeros((2, zone.block_size), dtype=np.float32)
                    if block is None:
                        return
                    started = time.monotonic()
                    stream.write(block, zone.sample_rate)
                    writer_measurements.put(
                        ("write", max(0.0, (time.monotonic() - started) * 1000))
                    )
            except BaseException as exc:  # surfaced by the render loop
                writer_error.append(exc)

        pending: list[_TimedMidi] = []
        event_targets: dict[str, float] = {}
        sequence = 0
        anchors: tuple[tuple[float, int], ...] = ()
        master_volume = 1.0
        window = _WindowStats(started_at=time.monotonic())

        if not deferred_audio_output:
            # Prime the output with silence. This makes readiness mean that the
            # CoreAudio stream is already running, not merely that BBCSO loaded.
            for _ in range(zone.prefill_blocks):
                audio_queue.put(np.zeros((2, zone.block_size), dtype=np.float32))
            writer = threading.Thread(
                target=write_blocks,
                name=f"rubato-vst-device-{zone.zone_id}",
                daemon=True,
            )
            writer.start()
        if telemetry_level != TelemetryLevel.OFF:
            responses.put(
                (
                    "trace",
                    AudioWorkerTrace(
                        type="audio_worker",
                        monotonic_time=time.monotonic(),
                        zone_id=zone.zone_id,
                        action="ready",
                        block_frames=zone.block_size,
                        sample_rate=zone.sample_rate,
                        detail=zone.output_device_name,
                    ).model_dump(mode="python"),
                )
            )
        responses.put(("ready", None))

        if deferred_audio_output:
            while True:
                kind, payload = commands.get()
                if kind == "stop":
                    responses.put(("stopped", None))
                    return
                if kind == "prime":
                    startup_stage = "priming_plugins"
                    barrier_id = str(payload["barrier_id"])
                    for binding in zone.instruments:
                        plugins[binding.instrument_id].process(
                            [],
                            duration=block_seconds,
                            sample_rate=zone.sample_rate,
                            num_channels=2,
                            buffer_size=zone.block_size,
                            reset=False,
                        )
                    responses.put(("prime_complete", barrier_id))
                    continue
                if kind == "activate":
                    origin = float(payload)
                    startup_stage = "rendering"
                    break

        block_index = 0 if deferred_audio_output else zone.prefill_blocks
        while True:
            if writer_error:
                raise LiveVstError(f"CoreAudio writer failed: {writer_error[0]}")

            while True:
                try:
                    kind, payload = commands.get_nowait()
                except queue.Empty:
                    break
                received_at = time.monotonic()
                if kind == "stop":
                    responses.put(("stopped", None))
                    return
                if kind == "note":
                    note = _NoteCommand(**payload)
                    target_device = (
                        note.target_acoustic_time - zone.configured_output_advance_ms / 1000.0
                    )
                    event_targets[note.event_id] = target_device
                    for binding in _bindings_for_part(zone.instruments, note.part_id):
                        sequence += 1
                        channel = binding.midi_channel
                        heapq.heappush(
                            pending,
                            _TimedMidi(
                                target_device,
                                sequence,
                                binding.instrument_id,
                                bytes(
                                    mido.Message(
                                        "note_on",
                                        note=note.pitch,
                                        velocity=max(1, note.velocity),
                                        channel=channel,
                                    ).bytes()
                                ),
                                note.event_id,
                                received_at,
                                "note_on",
                            ),
                        )
                        sequence += 1
                        heapq.heappush(
                            pending,
                            _TimedMidi(
                                target_device + note.duration_seconds,
                                sequence,
                                binding.instrument_id,
                                bytes(
                                    mido.Message(
                                        "note_off", note=note.pitch, channel=channel
                                    ).bytes()
                                ),
                                note.event_id,
                                received_at,
                                "note_off",
                            ),
                        )
                elif kind == "cancel":
                    event_ids = set(payload)
                    pending = [item for item in pending if item.event_id not in event_ids]
                    heapq.heapify(pending)
                    for event_id in event_ids:
                        event_targets.pop(event_id, None)
                elif kind == "retime_release":
                    event_id, target_acoustic_time = payload
                    target_device = (
                        target_acoustic_time - zone.configured_output_advance_ms / 1000.0
                    )
                    for item in pending:
                        if item.event_id == event_id and item.kind == "note_off":
                            item.target_device_time = target_device
                    heapq.heapify(pending)
                    responses.put(
                        ("retime_release_applied", (event_id, target_device))
                    )
                elif kind == "volume":
                    master_volume = _master_gain(float(payload))
                    responses.put(
                        (
                            "midi_output",
                            MidiOutputTrace(
                                type="midi_output",
                                monotonic_time=received_at,
                                action="master_volume",
                                renderer="live_vst",
                                zone_id=zone.zone_id,
                                instrument_id=(
                                    zone.instruments[0].instrument_id
                                    if len(zone.instruments) == 1
                                    else None
                                ),
                                master_volume=master_volume,
                            ).model_dump(mode="python"),
                        )
                    )
                elif kind == "panic":
                    if isinstance(payload, dict):
                        barrier_id = str(payload["barrier_id"])
                    else:  # Backward-compatible with direct worker probes.
                        barrier_id = None
                    pending.clear()
                    event_targets.clear()
                    panic_time = max(origin + block_index * block_seconds, time.monotonic())
                    for binding in zone.instruments:
                        for control in (120, 123):
                            sequence += 1
                            heapq.heappush(
                                pending,
                                _TimedMidi(
                                    panic_time,
                                    sequence,
                                    binding.instrument_id,
                                    bytes(
                                        mido.Message(
                                            "control_change",
                                            channel=binding.midi_channel,
                                            control=control,
                                            value=0,
                                        ).bytes()
                                    ),
                                    f"panic-{uuid.uuid4().hex}",
                                    received_at,
                                    "panic",
                                    barrier_id,
                                ),
                            )

            while True:
                try:
                    anchor = transport.get_nowait()
                except queue.Empty:
                    break
                anchors = (*anchors[-1:], (float(anchor[0]), int(anchor[1])))

            block_start = origin + block_index * block_seconds
            block_end = block_start + block_seconds
            by_instrument: dict[str, list[tuple[bytes, float]]] = defaultdict(list)
            delivered: list[_TimedMidi] = []
            while pending and pending[0].target_device_time < block_end:
                item = heapq.heappop(pending)
                delivered.append(item)
                offset = item.target_device_time - block_start
                if offset < 0:
                    if collect_counters:
                        window.late_events += 1
                    offset = 0.0
                by_instrument[item.instrument_id].append((item.message, offset))
                if collect_counters:
                    window.command_to_render_ms.append(
                        max(0.0, (time.monotonic() - item.received_at) * 1000)
                    )
                if item.kind == "note_off":
                    event_targets.pop(item.event_id, None)

            render_started = time.monotonic() if collect_counters else 0.0
            mixed = np.zeros((2, zone.block_size), dtype=np.float32)
            acoustic_start = block_start + zone.configured_output_advance_ms / 1000.0
            acoustic_end = block_end + zone.configured_output_advance_ms / 1000.0
            start_tick = _score_tick_at(acoustic_start, anchors)
            end_tick = _score_tick_at(acoustic_end, anchors)
            gain_states: list[tuple[VstInstrumentBinding, float, float]] = []
            completed_panic_barriers: set[str] = set()
            for binding in zone.instruments:
                plugin_started = time.monotonic() if collect_counters else 0.0
                rendered = plugins[binding.instrument_id].process(
                    by_instrument.get(binding.instrument_id, []),
                    duration=block_seconds,
                    sample_rate=zone.sample_rate,
                    num_channels=2,
                    buffer_size=zone.block_size,
                    reset=False,
                )
                for item in delivered:
                    if item.instrument_id != binding.instrument_id:
                        continue
                    message = mido.Message.from_bytes(list(item.message))
                    responses.put(
                        (
                            "midi_output",
                            MidiOutputTrace(
                                type="midi_output",
                                monotonic_time=time.monotonic(),
                                action=item.kind,
                                renderer="live_vst",
                                zone_id=zone.zone_id,
                                instrument_id=binding.instrument_id,
                                event_id=item.event_id,
                                channel=getattr(message, "channel", None),
                                pitch=getattr(message, "note", None),
                                velocity=getattr(message, "velocity", None),
                                control=getattr(message, "control", None),
                                value=getattr(message, "value", None),
                                requested_send_time=item.target_device_time,
                                output_lateness_ms=max(
                                    0.0, (block_start - item.target_device_time) * 1000
                                ),
                            ).model_dump(mode="python"),
                        )
                    )
                    if item.kind == "panic" and item.barrier_id is not None:
                        completed_panic_barriers.add(item.barrier_id)
                if collect_counters:
                    plugin_ms = max(0.0, (time.monotonic() - plugin_started) * 1000)
                    window.plugin_ms[binding.instrument_id].append(plugin_ms)
                rendered = np.asarray(rendered, dtype=np.float32)
                if rendered.shape != mixed.shape:
                    raise LiveVstError(
                        f"{binding.instrument_id} returned {rendered.shape}; expected {mixed.shape}"
                    )
                start_gain = _instrument_gain(policy, zone.zone_id, binding, start_tick)
                end_gain = _instrument_gain(policy, zone.zone_id, binding, end_tick)
                gain = np.linspace(
                    start_gain * master_volume,
                    end_gain * master_volume,
                    zone.block_size,
                    dtype=np.float32,
                )
                mixed += rendered * gain[np.newaxis, :]
                gain_states.append((binding, start_gain, end_gain))

            # Acknowledge only after every all-notes-off message in this block
            # has crossed the plug-in process boundary. The resident-router
            # lease uses this barrier before moving observations back to the
            # preload trace, so the just-finished run owns its complete tail.
            for barrier_id in completed_panic_barriers:
                responses.put(("panic_complete", barrier_id))

            if mix_gauges is not None:
                observed_at = time.monotonic()
                output_rms = float(np.sqrt(np.mean(np.square(mixed, dtype=np.float64))))
                output_peak = float(np.max(np.abs(mixed), initial=0.0))
                for binding, start_gain, end_gain in gain_states:
                    mix_gauges.publish(
                        binding.instrument_id,
                        rendered_at=observed_at,
                        score_tick_start=start_tick,
                        score_tick_end=end_tick,
                        route_gain_start=start_gain,
                        route_gain_end=end_gain,
                        master_gain=master_volume,
                        effective_gain_start=start_gain * master_volume,
                        effective_gain_end=end_gain * master_volume,
                        output_rms=output_rms,
                        output_peak=output_peak,
                    )

            if collect_counters:
                render_ms = max(0.0, (time.monotonic() - render_started) * 1000)
                window.render_ms.append(render_ms)
            if deferred_audio_output:
                # Preserve the renderer timeline across isolated patch hosts.
                # The zone mixer must never combine different score-time
                # blocks or advance while one patch is missing.
                audio_queue.put((block_index, mixed))
            else:
                audio_queue.put(mixed)
            if collect_counters:
                window.queue_high_watermark = max(
                    window.queue_high_watermark,
                    _queue_depth(audio_queue),
                )
            rendered_until.value = block_end
            block_index += 1

            if deferred_audio_output and block_index == 1:
                # Block zero is a sacrificial synchronization block. BBCSO's
                # first concurrent render after resume has variable native
                # wake latency. Pause here so the parent can anchor score time
                # only after every isolated host has demonstrably rendered.
                while True:
                    kind, payload = commands.get()
                    if kind == "stop":
                        responses.put(("stopped", None))
                        return
                    if kind == "rebase":
                        origin = float(payload)
                        rendered_until.value = origin + block_seconds
                        break
                startup_stage = "rendering"

            while True:
                try:
                    measurement, value = writer_measurements.get_nowait()
                except queue.Empty:
                    break
                if not collect_counters:
                    continue
                if measurement == "write":
                    window.write_ms.append(value)
                else:
                    window.underruns += 1

            now = time.monotonic()
            elapsed = now - window.started_at
            if elapsed >= 1.0 and collect_counters:
                utilizations = [value / (block_seconds * 1000) for value in window.render_ms]
                common = dict(
                    type="audio_worker",
                    monotonic_time=now,
                    zone_id=zone.zone_id,
                    action="window",
                    window_seconds=elapsed,
                    blocks=len(window.render_ms),
                    block_frames=zone.block_size,
                    sample_rate=zone.sample_rate,
                    render_queue_depth=_queue_depth(audio_queue),
                    render_queue_high_watermark=window.queue_high_watermark,
                    render_ms_median=(
                        statistics.median(window.render_ms) if window.render_ms else None
                    ),
                    render_ms_p95=_percentile(window.render_ms, 0.95),
                    render_ms_max=max(window.render_ms, default=None),
                    render_utilization_p95=_percentile(utilizations, 0.95),
                    render_utilization_max=max(utilizations, default=None),
                    device_write_ms_p95=_percentile(window.write_ms, 0.95),
                    command_to_render_ms_p95=_percentile(window.command_to_render_ms, 0.95),
                    late_events=window.late_events,
                    underruns=window.underruns,
                    rss_mb=_rss_mb(),
                )
                responses.put(
                    (
                        "trace",
                        AudioWorkerTrace(**common).model_dump(mode="python"),
                    )
                )
                for instrument_id, values in window.plugin_ms.items():
                    instrument_sample = common | {
                        "instrument_id": instrument_id,
                        "render_ms_median": statistics.median(values),
                        "render_ms_p95": _percentile(values, 0.95),
                        "render_ms_max": max(values),
                    }
                    responses.put(
                        (
                            "trace",
                            AudioWorkerTrace(**instrument_sample).model_dump(mode="python"),
                        )
                    )
                window = _WindowStats(started_at=now)
    except BaseException as exc:
        error_traceback = traceback.format_exc()
        responses.put(
            (
                "trace",
                AudioWorkerTrace(
                    type="audio_worker",
                    monotonic_time=time.monotonic(),
                    zone_id=zone.zone_id,
                    action="error",
                    block_frames=zone.block_size,
                    sample_rate=zone.sample_rate,
                    detail=repr(exc),
                    startup_stage=startup_stage,
                    error_type=type(exc).__name__,
                    traceback=error_traceback,
                ).model_dump(mode="python"),
            )
        )
        responses.put(
            (
                "error",
                {
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                    "repr": repr(exc),
                    "stage": startup_stage,
                    "traceback": error_traceback,
                },
            )
        )
    finally:
        writer_stop.set()
        try:
            audio_queue.put_nowait(None)
        except queue.Full:
            pass
        if writer is not None:
            writer.join(timeout=2.0)
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


class LiveVstZoneWorker:
    """Parent-side handle for one live, isolated VST/CoreAudio zone."""

    def __init__(
        self,
        zone: LiveVstZoneConfig,
        policy: MixPolicy,
        *,
        trace_sink: TraceSink,
        startup_observer: Callable[[dict[str, Any]], None] | None = None,
        context: Any | None = None,
        start_timeout_seconds: float = 120.0,
        telemetry_level: TelemetryLevel = TelemetryLevel.COUNTERS,
        mix_sample_interval_seconds: float = 0.1,
        deferred_audio_output: bool = False,
    ) -> None:
        if mix_sample_interval_seconds <= 0:
            raise ValueError("mix sample interval must be positive")
        self.zone = zone
        self._policy = policy
        self._trace_sink = trace_sink
        self._startup_observer = startup_observer
        self._ctx = context or mp.get_context("spawn")
        self._telemetry_level = telemetry_level
        self._deferred_audio_output = deferred_audio_output
        self._mix_sample_interval_seconds = mix_sample_interval_seconds
        self._mix_gauges = (
            MixGaugeBlock(
                tuple(binding.instrument_id for binding in zone.instruments),
                context=self._ctx,
            )
            if telemetry_level == TelemetryLevel.TRACE
            else None
        )
        self._mix_sequences: dict[str, int] = {}
        self._mix_observer_stop = threading.Event()
        self._mix_observer: threading.Thread | None = None
        self._response_observer_stop = threading.Event()
        self._response_observer: threading.Thread | None = None
        self._response_condition = threading.Condition()
        self._completed_prime_barriers: set[str] = set()
        self._completed_panic_barriers: set[str] = set()
        self._commands = self._ctx.Queue(maxsize=4096)
        self._transport = self._ctx.Queue(maxsize=1)
        self._responses = self._ctx.Queue()
        self._audio_blocks = (
            self._ctx.Queue(maxsize=zone.prefill_blocks + 2)
            if deferred_audio_output
            else None
        )
        self._rendered_until = self._ctx.Value("d", 0.0, lock=False)
        self._pending_targets: dict[str, float] = {}
        self._release_targets: dict[str, float] = {}
        self._release_retime_sent_at: dict[str, float] = {}
        self._release_retime_queued: dict[str, float] = {}
        self._closed = False
        self._suspended = False
        self._error: str | None = None
        self._error_payload: dict[str, Any] | None = None
        self._last_startup: dict[str, Any] | None = None
        self._process = self._ctx.Process(
            target=_worker_main,
            args=(
                zone,
                policy,
                self._commands,
                self._transport,
                self._responses,
                self._rendered_until,
            ),
            kwargs={
                "mix_gauges": self._mix_gauges,
                "telemetry_level": telemetry_level,
                "audio_blocks": self._audio_blocks,
            },
            name=f"rubato-vst-{zone.zone_id}",
            daemon=True,
        )
        self._process.start()
        deadline = time.monotonic() + start_timeout_seconds
        while time.monotonic() < deadline:
            try:
                kind, payload = self._responses.get(timeout=0.1)
            except queue.Empty:
                if not self._process.is_alive():
                    self._process.join(timeout=0.2)
                    try:
                        kind, payload = self._responses.get(timeout=0.3)
                    except queue.Empty:
                        kind = ""
                        payload = None
                    if kind:
                        self._accept_response(kind, payload)
                    raise self._startup_error("exited during startup")
                continue
            self._accept_response(kind, payload)
            if kind == "error":
                raise self._startup_error("failed during startup")
            if kind == "ready":
                self._response_observer = threading.Thread(
                    target=self._observe_responses,
                    name=f"rubato-vst-responses-{zone.zone_id}",
                    daemon=True,
                )
                self._response_observer.start()
                if self._mix_gauges is not None:
                    self._mix_observer = threading.Thread(
                        target=self._observe_mix_state,
                        name=f"rubato-mix-observer-{zone.zone_id}",
                        daemon=True,
                    )
                    self._mix_observer.start()
                if self._startup_observer is not None:
                    self._startup_observer(
                        {
                            "state": "ready",
                            "zone_id": zone.zone_id,
                            "instrument_id": None,
                            "loaded": len(zone.instruments),
                            "total": len(zone.instruments),
                        }
                    )
                return
        self.close()
        raise self._startup_error("did not become ready")

    def _startup_error(self, fallback: str) -> LiveVstError:
        if self._error_payload is not None:
            stage = self._error_payload.get("stage", "unknown stage")
            error_type = self._error_payload.get("error_type", "Error")
            detail = self._error_payload.get("detail", self._error)
            return LiveVstError(f"VST worker {self.zone.zone_id} {stage}: {error_type}: {detail}")
        last_stage = (self._last_startup or {}).get("state", "before first progress")
        exit_code = self._process.exitcode
        instrument_id = (self._last_startup or {}).get("instrument_id")
        worker_name = self.zone.zone_id
        if instrument_id:
            worker_name = f"{worker_name}/{instrument_id}"
        message = (
            f"VST worker {worker_name} {fallback}; "
            f"exit_code={exit_code}, last_stage={last_stage}"
        )
        self._trace_sink.write(
            AudioWorkerTrace(
                type="audio_worker",
                monotonic_time=time.monotonic(),
                zone_id=self.zone.zone_id,
                action="error",
                instrument_id=instrument_id,
                block_frames=self.zone.block_size,
                sample_rate=self.zone.sample_rate,
                detail=message,
                startup_stage=str(last_stage),
                error_type="NativeWorkerExit",
                worker_exit_code=exit_code,
            )
        )
        return LiveVstError(message, worker_exit_code=exit_code)

    @property
    def is_alive(self) -> bool:
        return self._process.is_alive() and self._error is None

    def accepts_part(self, part_id: str) -> bool:
        return bool(_bindings_for_part(self.zone.instruments, part_id))

    @property
    def failure_detail(self) -> dict[str, Any] | None:
        if self.is_alive:
            return None
        binding = self.zone.instruments[0] if len(self.zone.instruments) == 1 else None
        return {
            "zone_id": self.zone.zone_id,
            "instrument_id": binding.instrument_id if binding is not None else None,
            "exit_code": self._process.exitcode,
            "last_stage": (self._last_startup or {}).get("state"),
            "error": self._error,
            "block_frames": self.zone.block_size,
            "sample_rate": self.zone.sample_rate,
        }

    def set_trace_sink(self, trace_sink: TraceSink) -> None:
        """Redirect parent-side observations when a resident worker is leased."""

        self._trace_sink = trace_sink

    @property
    def audio_blocks(self) -> Any:
        if self._audio_blocks is None:
            raise LiveVstError(f"VST worker {self.zone.zone_id} owns its audio stream")
        return self._audio_blocks

    def activate(self, *, origin: float) -> None:
        if not self._deferred_audio_output:
            return
        self._put(("activate", origin))

    def rebase(self, *, origin: float) -> None:
        """Anchor score time after the sacrificial first block is available."""

        if not self._deferred_audio_output:
            raise LiveVstError(f"VST worker {self.zone.zone_id} cannot be rebased")
        self._put(("rebase", origin))

    def prime(self) -> str:
        """Wake a resumed plug-in host before assigning its score-time origin."""

        if not self._deferred_audio_output:
            raise LiveVstError(f"VST worker {self.zone.zone_id} cannot be primed externally")
        barrier_id = uuid.uuid4().hex
        self._put(("prime", {"barrier_id": barrier_id}))
        return barrier_id

    def wait_for_prime(self, barrier_id: str, *, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        with self._response_condition:
            while barrier_id not in self._completed_prime_barriers:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self.is_alive:
                    return False
                self._response_condition.wait(timeout=min(remaining, 0.05))
            self._completed_prime_barriers.remove(barrier_id)
            return True

    def suspend(self) -> None:
        """Quiesce a loaded native host while the next BBCSO host initializes."""

        if self._process.pid is None or not self.is_alive:
            raise LiveVstError(f"VST worker {self.zone.zone_id} cannot be suspended")
        os.kill(self._process.pid, signal.SIGSTOP)
        self._suspended = True

    def resume(self) -> None:
        if not self._suspended:
            return
        if self._process.pid is not None:
            os.kill(self._process.pid, signal.SIGCONT)
        self._suspended = False

    def send(self, event: ScheduledAccompanimentEvent, *, sent_at: float) -> None:
        score_event = event.event
        if score_event.pitch is None:
            return
        duration = event.duration_seconds
        if duration is None:
            duration = score_event.duration_beats * 60.0 / event.tempo_bpm
        payload = _NoteCommand(
            event_id=score_event.event_id,
            part_id=score_event.part_id,
            pitch=score_event.pitch,
            velocity=score_event.velocity if score_event.velocity is not None else 64,
            target_acoustic_time=event.perf_time,
            duration_seconds=max(0.001, duration),
            committed_at=event.committed_at if event.committed_at is not None else sent_at,
        )
        self._pending_targets[payload.event_id] = (
            payload.target_acoustic_time - self.zone.configured_output_advance_ms / 1000.0
        )
        self._release_targets[payload.event_id] = (
            self._pending_targets[payload.event_id] + payload.duration_seconds
        )
        self._release_retime_sent_at[payload.event_id] = time.monotonic()
        self._put(("note", payload.__dict__))

    def update_transport(self, *, acoustic_time: float, score_tick: int) -> None:
        self._ensure_available()
        try:
            while True:
                self._transport.get_nowait()
        except queue.Empty:
            pass
        try:
            self._transport.put_nowait((acoustic_time, score_tick))
        except queue.Full:
            # The child won the race after our drain. Preserve non-blocking
            # conductor semantics; the next 2 ms control tick replaces it.
            pass
        self._drain_responses()

    def cancel_pending(self, event_ids: Iterable[str]) -> bool:
        ids = tuple(event_ids)
        if not ids:
            return True
        rendered_until = float(self._rendered_until.value)
        if any(
            self._pending_targets.get(event_id, -math.inf) <= rendered_until for event_id in ids
        ):
            return False
        self._put(("cancel", ids))
        for event_id in ids:
            self._pending_targets.pop(event_id, None)
            self._release_targets.pop(event_id, None)
            self._release_retime_sent_at.pop(event_id, None)
            self._release_retime_queued.pop(event_id, None)
        return True

    def retime_release(self, event_id: str, *, scheduled_at: float) -> bool:
        current = self._release_targets.get(event_id)
        if current is None:
            return False
        target_device = scheduled_at - self.zone.configured_output_advance_ms / 1000.0
        rendered_until = float(self._rendered_until.value)
        if current <= rendered_until:
            self._pending_targets.pop(event_id, None)
            self._release_targets.pop(event_id, None)
            self._release_retime_sent_at.pop(event_id, None)
            self._release_retime_queued.pop(event_id, None)
            return False
        # The symbolic loop may run hundreds of times per audio block. Only a
        # musically meaningful deadline change should cross the process queue;
        # otherwise identical retimes can starve note-off and panic commands.
        if abs(target_device - current) < self.zone.block_size / self.zone.sample_rate:
            return True
        now = time.monotonic()
        self._release_targets[event_id] = target_device
        if event_id in self._release_retime_queued:
            return True
        if now - self._release_retime_sent_at.get(event_id, 0.0) < 0.1:
            return True
        self._release_retime_sent_at[event_id] = now
        self._release_retime_queued[event_id] = target_device
        self._put(("retime_release", (event_id, scheduled_at)))
        return True

    def set_master_volume(self, volume: float) -> None:
        if not 0 <= volume <= 1:
            raise ValueError("volume must be between 0 and 1")
        self._put(("volume", volume))

    def panic(self, reason: str) -> str:
        self._pending_targets.clear()
        self._release_targets.clear()
        self._release_retime_sent_at.clear()
        self._release_retime_queued.clear()
        barrier_id = uuid.uuid4().hex
        self._put(
            ("panic", {"reason": reason, "barrier_id": barrier_id}),
            priority=True,
        )
        return barrier_id

    def wait_for_panic(self, barrier_id: str, *, timeout_seconds: float) -> bool:
        """Wait until the child has processed this panic through the plug-in."""

        deadline = time.monotonic() + timeout_seconds
        with self._response_condition:
            while barrier_id not in self._completed_panic_barriers:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self.is_alive:
                    return False
                self._response_condition.wait(timeout=min(remaining, 0.05))
            self._completed_panic_barriers.remove(barrier_id)
            return True

    def close(self) -> None:
        if self._closed:
            return
        self.resume()
        self._closed = True
        try:
            self._commands.put(("stop", None), timeout=0.25)
        except (OSError, ValueError, queue.Full):
            pass
        self._process.join(timeout=5.0)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=2.0)
        self._response_observer_stop.set()
        if self._response_observer is not None:
            self._response_observer.join(timeout=1.0)
        self._drain_responses(raise_error=False)
        self._mix_observer_stop.set()
        if self._mix_observer is not None:
            self._mix_observer.join(timeout=1.0)
        self._sample_mix_state()

    def _observe_mix_state(self) -> None:
        while not self._mix_observer_stop.wait(self._mix_sample_interval_seconds):
            self._sample_mix_state()

    def _sample_mix_state(self) -> None:
        if self._mix_gauges is None:
            return
        now = time.monotonic()
        for sample in self._mix_gauges.sample():
            if sample.sequence <= self._mix_sequences.get(sample.instrument_id, 0):
                continue
            self._mix_sequences[sample.instrument_id] = sample.sequence
            self._trace_sink.write(
                MixStateTrace(
                    type="mix_state",
                    monotonic_time=now,
                    rendered_at=sample.rendered_at,
                    renderer="live_vst",
                    mix_program_id=self._policy.program_id,
                    mix_program_revision=self._policy.program_revision,
                    zone_id=self.zone.zone_id,
                    instrument_id=sample.instrument_id,
                    score_tick_start=max(0, sample.score_tick_start),
                    score_tick_end=max(0, sample.score_tick_end),
                    route_gain_start=sample.route_gain_start,
                    route_gain_end=sample.route_gain_end,
                    master_gain=sample.master_gain,
                    effective_gain_start=sample.effective_gain_start,
                    effective_gain_end=sample.effective_gain_end,
                    output_rms=sample.output_rms,
                    output_peak=sample.output_peak,
                )
            )

    def _put(self, command: tuple[str, Any], *, priority: bool = False) -> None:
        self._ensure_available()
        try:
            self._commands.put_nowait(command)
        except queue.Full as exc:
            if priority:
                # Panic invalidates every queued note/retime. Discarding that
                # stale work is exactly its contract and guarantees the safety
                # command can enter even after a producer bug fills the queue.
                try:
                    while True:
                        self._commands.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._commands.put(command, timeout=0.25)
                    self._drain_responses()
                    return
                except queue.Full:
                    pass
            raise LiveVstError(f"VST command queue overflowed for {self.zone.zone_id}") from exc
        self._drain_responses()

    def _observe_responses(self) -> None:
        """Continuously move child observations off the IPC queue.

        Audio rendering is autonomous while a resident BBCSO host is idle.
        Polling the response queue only when the scheduler happens to submit a
        command strands renderer telemetry and can misroute the tail of a live
        run. One off-audio-thread consumer makes the observation path causal
        and keeps native failures visible even between performances.
        """

        while not self._response_observer_stop.is_set():
            try:
                kind, payload = self._responses.get(timeout=0.1)
            except queue.Empty:
                continue
            self._accept_response(kind, payload)

    def _ensure_available(self) -> None:
        if self._closed:
            raise LiveVstError(f"VST worker {self.zone.zone_id} is closed")
        self._drain_responses()
        if self._error is not None:
            raise LiveVstError(f"VST worker {self.zone.zone_id} failed: {self._error}")
        if not self._process.is_alive():
            raise LiveVstError(f"VST worker {self.zone.zone_id} exited")

    def _drain_responses(self, *, raise_error: bool = True) -> None:
        # After startup the dedicated observer is the sole queue consumer.
        # Competing get_nowait calls can reorder a panic barrier relative to
        # its trace rows and were the source of missing resident-run evidence.
        if self._response_observer is not None and self._response_observer.is_alive():
            if raise_error and self._error is not None:
                raise LiveVstError(f"VST worker {self.zone.zone_id} failed: {self._error}")
            return
        while True:
            try:
                kind, payload = self._responses.get_nowait()
            except queue.Empty:
                break
            self._accept_response(kind, payload)
        if raise_error and self._error is not None:
            raise LiveVstError(f"VST worker {self.zone.zone_id} failed: {self._error}")

    def _accept_response(self, kind: str, payload: Any) -> None:
        if kind == "trace":
            self._trace_sink.write(AudioWorkerTrace.model_validate(payload))
        elif kind == "midi_output":
            row = MidiOutputTrace.model_validate(payload)
            if row.action == "note_off" and row.event_id is not None:
                self._pending_targets.pop(row.event_id, None)
                self._release_targets.pop(row.event_id, None)
                self._release_retime_sent_at.pop(row.event_id, None)
                self._release_retime_queued.pop(row.event_id, None)
            self._trace_sink.write(row)
        elif kind == "retime_release_applied":
            event_id, applied_target = payload
            event_id = str(event_id)
            self._trace_sink.write(
                MidiOutputTrace(
                    type="midi_output",
                    monotonic_time=time.monotonic(),
                    action="release_retime",
                    renderer="live_vst",
                    zone_id=self.zone.zone_id,
                    instrument_id=(
                        self.zone.instruments[0].instrument_id
                        if len(self.zone.instruments) == 1
                        else None
                    ),
                    event_id=event_id,
                    scheduled_note_off_time=float(applied_target),
                    reason="applied_to_audio_timeline",
                )
            )
            self._release_retime_queued.pop(event_id, None)
            desired_target = self._release_targets.get(event_id)
            if desired_target is None:
                return
            block_seconds = self.zone.block_size / self.zone.sample_rate
            if (
                desired_target > float(self._rendered_until.value)
                and abs(desired_target - float(applied_target)) >= block_seconds
            ):
                self._release_retime_queued[event_id] = desired_target
                self._release_retime_sent_at[event_id] = time.monotonic()
                scheduled_at = (
                    desired_target + self.zone.configured_output_advance_ms / 1000.0
                )
                self._put(("retime_release", (event_id, scheduled_at)))
        elif kind == "prime_complete":
            with self._response_condition:
                self._completed_prime_barriers.add(str(payload))
                self._response_condition.notify_all()
        elif kind == "panic_complete":
            with self._response_condition:
                self._completed_panic_barriers.add(str(payload))
                self._response_condition.notify_all()
        elif kind == "startup" and self._startup_observer is not None:
            self._last_startup = dict(payload)
            self._startup_observer(dict(payload))
        elif kind == "startup":
            self._last_startup = dict(payload)
        elif kind == "error":
            self._error_payload = dict(payload) if isinstance(payload, dict) else None
            self._error = (
                str(self._error_payload.get("detail"))
                if self._error_payload is not None
                else str(payload)
            )
            if self._startup_observer is not None:
                self._startup_observer(
                    {
                        "state": "failed",
                        "zone_id": self.zone.zone_id,
                        "instrument_id": None,
                        "loaded": 0,
                        "total": len(self.zone.instruments),
                        "detail": self._error,
                        "error_type": (
                            self._error_payload.get("error_type")
                            if self._error_payload is not None
                            else None
                        ),
                        "stage": (
                            self._error_payload.get("stage")
                            if self._error_payload is not None
                            else None
                        ),
                    }
                )


class LiveVstZoneMixer:
    """One PortAudio/CoreAudio writer that sums isolated patch-host blocks."""

    def __init__(
        self,
        zone: LiveVstZoneConfig,
        workers: tuple[LiveVstZoneWorker, ...],
        *,
        trace_sink: TraceSink,
        stream_factory: Callable[[LiveVstZoneConfig], Any] = _open_audio_stream,
    ) -> None:
        if not workers:
            raise ValueError("a live VST zone mixer needs at least one worker")
        self.zone = zone
        self._workers = workers
        self._trace_sink = trace_sink
        self._block_seconds = zone.block_size / zone.sample_rate
        self._stop = threading.Event()
        self._primed = threading.Event()
        self._error: BaseException | None = None
        self._stream = stream_factory(zone)
        prime_barriers = tuple((worker, worker.prime()) for worker in workers)
        prime_deadline = time.monotonic() + 15.0
        unprimed = []
        for worker, barrier_id in prime_barriers:
            remaining = max(0.0, prime_deadline - time.monotonic())
            if not worker.wait_for_prime(barrier_id, timeout_seconds=remaining):
                unprimed.append(worker.zone.instruments[0].instrument_id)
        if unprimed:
            names = ", ".join(unprimed)
            raise LiveVstError(f"BBCSO patches did not wake after preload: {names}")
        self._stream.__enter__()
        self._origin = 0.0
        self._thread = threading.Thread(
            target=self._write_blocks,
            name=f"rubato-vst-zone-mixer-{zone.zone_id}",
            daemon=True,
        )
        provisional_origin = time.monotonic()
        for worker in workers:
            worker.activate(origin=provisional_origin)
        self._thread.start()
        if not self._primed.wait(timeout=15.0):
            self.close()
            detail = str(self._error) if self._error is not None else "first audio block timed out"
            raise LiveVstError(f"CoreAudio zone {zone.zone_id} did not prime: {detail}")

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive() and self._error is None

    @property
    def failure_detail(self) -> dict[str, Any] | None:
        if self._error is None:
            return None
        return {
            "zone_id": self.zone.zone_id,
            "stage": "audio_output",
            "detail": str(self._error),
            "error_type": type(self._error).__name__,
        }

    def set_trace_sink(self, trace_sink: TraceSink) -> None:
        self._trace_sink = trace_sink

    def _write_blocks(self) -> None:
        stopped_workers: set[int] = set()
        expected_block_index = 0
        try:
            while not self._stop.is_set() and len(stopped_workers) < len(self._workers):
                mixed = np.zeros((2, self.zone.block_size), dtype=np.float32)
                for index, worker in enumerate(self._workers):
                    if index in stopped_workers:
                        continue
                    try:
                        item = worker.audio_blocks.get(
                            timeout=(
                                15.0
                                if expected_block_index == 0
                                else max(0.25, self._block_seconds * 12)
                            )
                        )
                    except queue.Empty:
                        raise LiveVstError(
                            f"BBCSO patch {worker.zone.instruments[0].instrument_id} "
                            f"missed audio block {expected_block_index}"
                        ) from None
                    if item is None:
                        stopped_workers.add(index)
                        continue
                    block_index, block = item
                    if block_index != expected_block_index:
                        raise LiveVstError(
                            f"BBCSO patch {worker.zone.instruments[0].instrument_id} returned "
                            f"audio block {block_index}; expected {expected_block_index}"
                        )
                    mixed += np.asarray(block, dtype=np.float32)
                if stopped_workers:
                    break
                if expected_block_index == 0:
                    self._origin = time.monotonic() + max(
                        0.05, self._block_seconds * self.zone.prefill_blocks
                    )
                    for worker in self._workers:
                        worker.rebase(origin=self._origin)
                    delay = self._origin - time.monotonic()
                    if delay > 0 and self._stop.wait(delay):
                        break
                self._stream.write(mixed, self.zone.sample_rate)
                self._primed.set()
                expected_block_index += 1
        except BaseException as exc:
            self._error = exc
            self._primed.set()

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
        try:
            self._stream.close()
        except Exception:
            pass


class LiveVstRouter:
    """Fan committed orchestral notes and score transport into live VST zones."""

    def __init__(
        self,
        workers: tuple[LiveVstZoneWorker, ...],
        mixers: tuple[LiveVstZoneMixer, ...] = (),
    ) -> None:
        self._workers = workers
        self._mixers = mixers
        self._event_workers: dict[str, tuple[LiveVstZoneWorker, ...]] = {}

    @property
    def is_alive(self) -> bool:
        return (
            bool(self._workers)
            and all(worker.is_alive for worker in self._workers)
            and all(mixer.is_alive for mixer in self._mixers)
        )

    @property
    def failure_details(self) -> tuple[dict[str, Any], ...]:
        worker_details = tuple(
            detail
            for worker in self._workers
            if (detail := worker.failure_detail) is not None
        )
        mixer_details = tuple(
            detail
            for mixer in self._mixers
            if (detail := mixer.failure_detail) is not None
        )
        return worker_details + mixer_details

    def set_trace_sink(self, trace_sink: TraceSink) -> None:
        for worker in self._workers:
            worker.set_trace_sink(trace_sink)
        for mixer in self._mixers:
            mixer.set_trace_sink(trace_sink)

    def send(self, event: ScheduledAccompanimentEvent, *, sent_at: float) -> None:
        workers = tuple(
            worker for worker in self._workers if worker.accepts_part(event.event.part_id)
        )
        if workers:
            self._event_workers[event.event.event_id] = workers
        for worker in workers:
            worker.send(event, sent_at=sent_at)

    def cancel_pending(self, event_ids: Iterable[str]) -> bool:
        ids = tuple(event_ids)
        results = []
        for worker in self._workers:
            owned = tuple(
                event_id
                for event_id in ids
                if worker in self._event_workers.get(event_id, ())
            )
            if owned:
                results.append(worker.cancel_pending(owned))
        if all(results):
            for event_id in ids:
                self._event_workers.pop(event_id, None)
        return all(results)

    def retime_release(self, event_id: str, *, scheduled_at: float) -> bool:
        results = [
            worker.retime_release(event_id, scheduled_at=scheduled_at)
            for worker in self._event_workers.get(event_id, ())
        ]
        return all(results) if results else True

    def panic(self, *, sent_at: float, reason: str) -> None:
        _ = sent_at
        self._event_workers.clear()
        for worker in self._workers:
            worker.panic(reason)

    def panic_and_wait(
        self,
        *,
        sent_at: float,
        reason: str,
        timeout_seconds: float = 1.0,
    ) -> None:
        """Release every worker and wait for plug-in-bound acknowledgements."""

        _ = sent_at
        self._event_workers.clear()
        barriers = tuple((worker, worker.panic(reason)) for worker in self._workers)
        deadline = time.monotonic() + timeout_seconds
        incomplete = []
        for worker, barrier_id in barriers:
            remaining = max(0.0, deadline - time.monotonic())
            if not worker.wait_for_panic(barrier_id, timeout_seconds=remaining):
                incomplete.append(worker.zone.zone_id)
        if incomplete:
            names = ", ".join(incomplete)
            raise LiveVstError(f"VST panic acknowledgement timed out for {names}")

    def set_master_volume(self, volume: float, *, sent_at: float) -> None:
        _ = sent_at
        for worker in self._workers:
            worker.set_master_volume(volume)

    def set_output_advance(self, output_advance_ms: float) -> None:
        _ = output_advance_ms
        # Zone advance is a calibrated machine binding, not a performer control.

    def apply_mix_automation(self, score_tick: int, *, sent_at: float) -> None:
        for worker in self._workers:
            worker.update_transport(acoustic_time=sent_at, score_tick=score_tick)

    def close(self) -> None:
        self._event_workers.clear()
        errors = []
        for worker in self._workers:
            try:
                worker.close()
            except Exception as exc:  # close every zone before surfacing one failure
                errors.append(exc)
        for mixer in self._mixers:
            try:
                mixer.close()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise LiveVstError(f"could not close every VST zone: {errors[0]}")


class MultiZoneAccompanimentOutput:
    """One scheduler output that owns Yamaha and/or live audio zones.

    Either side may be absent: ``vst=None`` is Yamaha-only, and ``yamaha=None``
    is VST-only (the performer chose no MIDI output, so the orchestra sounds only
    through the live audio zones -- e.g. BBCSO to a room speaker -- with no MIDI
    copy to double it). At least one must be present.
    """

    def __init__(self, yamaha: Any | None, vst: LiveVstRouter | None = None) -> None:
        if yamaha is None and vst is None:
            raise ValueError("MultiZoneAccompanimentOutput needs a Yamaha or a VST output")
        self._yamaha = yamaha
        self._vst = vst

    def send(self, event: ScheduledAccompanimentEvent, *, sent_at: float) -> None:
        if self._yamaha is not None:
            self._yamaha.send(event, sent_at=sent_at)
        if self._vst is not None:
            self._vst.send(event, sent_at=sent_at)

    def cancel_pending(self, event_ids: Iterable[str]) -> bool:
        ids = tuple(event_ids)
        if self._vst is not None and not self._vst.cancel_pending(ids):
            return False
        return self._yamaha.cancel_pending(ids) if self._yamaha is not None else True

    def retime_release(self, event_id: str, *, scheduled_at: float) -> bool:
        yamaha = (
            self._yamaha.retime_release(event_id, scheduled_at=scheduled_at)
            if self._yamaha is not None
            else True
        )
        vst = (
            self._vst.retime_release(event_id, scheduled_at=scheduled_at)
            if self._vst is not None
            else True
        )
        return yamaha and vst

    def panic(self, *, sent_at: float, reason: str) -> None:
        if self._yamaha is not None:
            self._yamaha.panic(sent_at=sent_at, reason=reason)
        if self._vst is not None:
            self._vst.panic(sent_at=sent_at, reason=reason)

    def set_master_volume(self, volume: float, *, sent_at: float) -> None:
        if self._yamaha is not None:
            self._yamaha.set_master_volume(volume, sent_at=sent_at)
        if self._vst is not None:
            self._vst.set_master_volume(volume, sent_at=sent_at)

    def set_output_advance(self, output_advance_ms: float) -> None:
        if self._yamaha is not None:
            self._yamaha.set_output_advance(output_advance_ms)
        if self._vst is not None:
            self._vst.set_output_advance(output_advance_ms)

    def apply_mix_automation(self, score_tick: int, *, sent_at: float) -> None:
        if self._yamaha is not None:
            self._yamaha.apply_mix_automation(score_tick, sent_at=sent_at)
        if self._vst is not None:
            self._vst.apply_mix_automation(score_tick, sent_at=sent_at)

    def close(self) -> None:
        error: Exception | None = None
        if self._vst is not None:
            try:
                self._vst.close()
            except Exception as exc:
                error = exc
        if self._yamaha is not None:
            self._yamaha.close()
        if error is not None:
            raise error


__all__ = [
    "LiveVstError",
    "LiveVstRouter",
    "LiveVstZoneMixer",
    "LiveVstZoneWorker",
    "MultiZoneAccompanimentOutput",
]
