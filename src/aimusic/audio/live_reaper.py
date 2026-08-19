"""Low-latency external orchestra rendering through REAPER.

Rubato remains responsible for score following, deadlines, note lifetimes and
mix control.  REAPER owns BBCSO and the CoreAudio callback.  The boundary is a
CoreMIDI virtual source plus a small file heartbeat written by the bundled
ReaScript bridge; no audio crosses a Python process or queue.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Literal

import mido
from pydantic import BaseModel, ConfigDict, Field

from aimusic.accompaniment.midi_output import (
    DeadlineAccompanimentOutput,
    MidiOutputPort,
    MidoAccompanimentOutput,
)
from aimusic.accompaniment.runtime_io import TraceSink
from aimusic.accompaniment.scheduler import ScheduledAccompanimentEvent
from aimusic.accompaniment.score_bundle import InstrumentMapEntry
from aimusic.audio.live_config import LiveVstZoneConfig
from aimusic.audio.live_vst import LiveVstError
from aimusic.mixing.policy import MixPolicy


class ReaperHeartbeat(BaseModel):
    """Status document emitted by ``rubato_reaper_bridge.lua``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    updated_at_epoch: float = Field(gt=0)
    state: Literal["ready", "setup_required", "failed"]
    message: str
    project_path: str | None = None
    midi_input_name: str | None = None
    audio_output_name: str | None = None
    sample_rate: float | None = Field(default=None, gt=0)
    block_size: int | None = Field(default=None, gt=0)
    output_latency_samples: int | None = Field(default=None, ge=0)
    ready_tracks: int = Field(default=0, ge=0)
    total_tracks: int = Field(default=0, ge=0)
    midi_event_sequence: int | None = Field(default=None, ge=1)
    midi_event_age_ms: float | None = Field(default=None, ge=0)
    midi_event_status: int | None = Field(default=None, ge=0, le=255)
    midi_event_data1: int | None = Field(default=None, ge=0, le=127)
    midi_event_data2: int | None = Field(default=None, ge=0, le=127)
    midi_events_seen: int = Field(default=0, ge=0)
    track_peak: float | None = Field(default=None, ge=0)
    master_peak: float | None = Field(default=None, ge=0)
    peak_since_start: float | None = Field(default=None, ge=0)


HeartbeatLoader = Callable[[Path], ReaperHeartbeat]
VirtualPortFactory = Callable[[str], MidiOutputPort]
AudioOutputResolver = Callable[[], str | None]


def load_reaper_heartbeat(path: Path) -> ReaperHeartbeat:
    return ReaperHeartbeat.model_validate_json(path.read_text(encoding="utf-8"))


# macOS assigns a *fresh* CoreMIDI unique ID to every virtual-source instance.
# REAPER remembers enabled MIDI-input rows by unique ID, so a churning ID makes
# REAPER fail to open Rubato's source after every backend restart ("could not be
# opened"), and no MIDI is ever consumed. Pinning a constant unique ID keeps a
# single enabled REAPER input row valid across launches. 'RBO1'.
_STABLE_MIDI_UNIQUE_ID = 0x52424F31


def _pin_coremidi_unique_id(name: str, unique_id: int = _STABLE_MIDI_UNIQUE_ID) -> bool:
    """Best-effort: pin the live virtual source's CoreMIDI unique ID.

    Returns True if the property was set. Any failure (non-macOS, missing
    framework, CoreMIDI error) leaves the OS-assigned ID in place and returns
    False; the caller still has a working port, just without the stable identity.
    """

    try:
        import ctypes
        import ctypes.util
    except Exception:  # pragma: no cover - ctypes always present on CPython
        return False
    core_midi = ctypes.util.find_library("CoreMIDI")
    core_foundation = ctypes.util.find_library("CoreFoundation")
    if not core_midi or not core_foundation:
        return False
    try:
        cm = ctypes.CDLL(core_midi)
        cf = ctypes.CDLL(core_foundation)
        cm.MIDIGetNumberOfSources.restype = ctypes.c_ulong
        cm.MIDIGetSource.restype = ctypes.c_uint32
        cm.MIDIGetSource.argtypes = [ctypes.c_ulong]
        cm.MIDIObjectSetIntegerProperty.argtypes = [
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_int32,
        ]
        cm.MIDIObjectSetIntegerProperty.restype = ctypes.c_int32
        cm.MIDIObjectGetStringProperty.argtypes = [
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        cf.CFStringGetCStringPtr.restype = ctypes.c_char_p
        cf.CFStringGetCStringPtr.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        uid_prop = ctypes.c_void_p.in_dll(cm, "kMIDIPropertyUniqueID")
        name_prop = ctypes.c_void_p.in_dll(cm, "kMIDIPropertyName")
        # SInt32; the constant fits the positive range, but stay signed-safe.
        signed = unique_id if unique_id < 2**31 else unique_id - 2**32
        for index in range(cm.MIDIGetNumberOfSources()):
            src = cm.MIDIGetSource(index)
            name_ref = ctypes.c_void_p()
            cm.MIDIObjectGetStringProperty(src, name_prop, ctypes.byref(name_ref))
            if not name_ref:
                continue
            cptr = cf.CFStringGetCStringPtr(name_ref, 0x08000100)  # UTF-8
            if not cptr or cptr.decode(errors="replace") != name:
                continue
            return cm.MIDIObjectSetIntegerProperty(src, uid_prop, signed) == 0
    except Exception:
        return False
    return False


def _virtual_output(name: str) -> MidiOutputPort:
    # RtMidi calls this an output because Rubato sends to it.  On CoreMIDI it
    # appears to other applications as a source/input named ``name``.
    port = mido.open_output(name, virtual=True)
    _pin_coremidi_unique_id(name)
    return port


def _system_default_audio_output() -> str | None:
    """Resolve what REAPER's ``CoreAudio Default`` means right now."""

    try:
        import sounddevice as sd

        output_index = int(sd.default.device[1])
        if output_index < 0:
            return None
        return str(sd.query_devices(output_index)["name"])
    except (ImportError, OSError, TypeError, ValueError):
        return None


def _instrument_map(zone: LiveVstZoneConfig) -> tuple[InstrumentMapEntry, ...]:
    routes: list[InstrumentMapEntry] = []
    owned_parts: set[str] = set()
    for binding in zone.instruments:
        for part_id in binding.stem_ids:
            if part_id in owned_parts:
                raise ValueError(f"REAPER part {part_id!r} is routed more than once")
            owned_parts.add(part_id)
            routes.append(
                InstrumentMapEntry(
                    part_id=part_id,
                    channel=binding.midi_channel,
                    program=0,
                    name=binding.instrument_id,
                    volume=100,
                )
            )
    return tuple(routes)


class ReaperMidiRouter:
    """Live-router-compatible adapter for a resident REAPER project."""

    def __init__(
        self,
        zone: LiveVstZoneConfig,
        policy: MixPolicy,
        *,
        trace_sink: TraceSink,
        startup_observer: Callable[[dict[str, Any]], None] | None = None,
        port_factory: VirtualPortFactory = _virtual_output,
        heartbeat_loader: HeartbeatLoader = load_reaper_heartbeat,
        audio_output_resolver: AudioOutputResolver = _system_default_audio_output,
        startup_timeout_seconds: float = 20.0,
        # REAPER may take a moment to (re)open the virtual source when it first
        # reappears; the stable unique ID keeps a single enabled row valid, and
        # this window plus the probe resend absorb that open latency.
        # Long enough that the bridge's own MIDI auto-reset (grace ~3s) can rebind
        # a stale input and this probe's resent CC119 still lands inside the window.
        midi_probe_timeout_seconds: float = 8.0,
        now: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        if zone.renderer != "reaper":
            raise ValueError("ReaperMidiRouter requires a REAPER zone")
        assert zone.midi_port_name is not None
        assert zone.reaper_heartbeat_path is not None
        self.zone = zone
        self._now = now
        self._wall_time = wall_time
        self._heartbeat_loader = heartbeat_loader
        self._audio_output_resolver = audio_output_resolver
        self._trace_sink = trace_sink
        self._failure_detail: dict[str, Any] | None = None
        self._closed = False
        self._port = port_factory(zone.midi_port_name)
        delegate = MidoAccompanimentOutput(
            zone.midi_port_name,
            _instrument_map(zone),
            port_factory=lambda _name: self._port,
            now=now,
            event_observer=trace_sink.write,
            # HDMI output volume is not controllable from macOS on this setup.
            # Start the resident orchestra safely; the PWA sends the explicit
            # rehearsal level again when a run begins.
            master_volume=0.20,
            mix_level_resolver=lambda part_id, score_tick: policy.audio_gain_for_part_at(
                zone.zone_id, part_id, score_tick
            ),
        )
        self._delegate = delegate
        self._output = DeadlineAccompanimentOutput(
            delegate,
            now=now,
            output_advance_ms=zone.configured_output_advance_ms,
        )
        try:
            self._wait_until_ready(startup_observer, startup_timeout_seconds)
            if midi_probe_timeout_seconds > 0:
                self._verify_midi_ingress(midi_probe_timeout_seconds)
            # Initialization CCs may have preceded REAPER's input binding. Send
            # the current master level again after the bridge proves readiness.
            self._delegate.resend_channel_volumes(sent_at=now(), reason="reaper_bridge_ready")
        except BaseException:
            self._output.close()
            self._closed = True
            raise

    def _verify_midi_ingress(self, timeout_seconds: float) -> None:
        """Prove REAPER accepts this source, not merely that it lists its name."""

        heartbeat, _ = self._current_heartbeat()
        baseline = heartbeat.midi_event_sequence if heartbeat is not None else None
        # Channel 16 is outside the four orchestra track filters. CC119 is
        # undefined, so this reaches REAPER's input history without touching a
        # BBCSO patch or producing sound.
        probe = mido.Message("control_change", channel=15, control=119, value=17)
        # Re-send across the window rather than once: REAPER binds the live
        # CoreMIDI endpoint to one of possibly several same-name device-cache
        # rows a few tens of ms after the source opens, and the bridge needs at
        # least one probe *after* it has latched that row. A single shot can race
        # that binding; repeated sends cannot, without weakening the check.
        self._port.send(probe)
        deadline = self._now() + timeout_seconds
        next_resend = self._now() + 0.25
        while self._now() < deadline:
            observed, problem = self._current_heartbeat()
            if (
                observed is not None
                and problem is None
                and observed.midi_event_sequence is not None
                and observed.midi_event_sequence != baseline
                and observed.midi_event_status == 0xBF
                and observed.midi_event_data1 == 119
                and observed.midi_event_data2 == 17
            ):
                return
            if self._now() >= next_resend:
                self._port.send(probe)
                next_resend = self._now() + 0.25
            time.sleep(0.05)
        raise LiveVstError(
            "REAPER sees Rubato Orchestra but did not receive its MIDI probe. "
            "REAPER's MIDI device cache may hold a stale same-name 'Rubato "
            "Orchestra' row bound ahead of the live endpoint; use "
            "Preferences > Audio > MIDI Inputs > Reset all MIDI devices while the "
            "source is open, and confirm Input is enabled for the live row. The "
            "REAPER bridge log records the candidate device indices and which "
            "index actually received input."
        )

    def _wait_until_ready(
        self,
        observer: Callable[[dict[str, Any]], None] | None,
        timeout_seconds: float,
    ) -> None:
        deadline = self._now() + timeout_seconds
        last_message: str | None = None
        while self._now() < deadline:
            heartbeat, problem = self._current_heartbeat()
            message = problem or (heartbeat.message if heartbeat is not None else None)
            if observer is not None and message != last_message:
                observer(
                    {
                        "state": "opening_audio",
                        "zone_id": self.zone.zone_id,
                        "instrument_id": None,
                        "loaded": heartbeat.ready_tracks if heartbeat is not None else 0,
                        "total": (
                            heartbeat.total_tracks
                            if heartbeat is not None
                            else len(self.zone.instruments)
                        ),
                        "detail": message,
                    }
                )
            last_message = message
            if heartbeat is not None and heartbeat.state == "ready" and problem is None:
                if observer is not None:
                    observer(
                        {
                            "state": "ready",
                            "zone_id": self.zone.zone_id,
                            "instrument_id": None,
                            "loaded": heartbeat.ready_tracks,
                            "total": heartbeat.total_tracks,
                            "detail": heartbeat.message,
                        }
                    )
                return
            time.sleep(0.1)
        detail = last_message or "REAPER bridge did not publish a heartbeat"
        raise LiveVstError(
            f"REAPER is not ready: {detail}. Run the Rubato bridge in the Rubato Orchestra project."
        )

    def _current_heartbeat(self) -> tuple[ReaperHeartbeat | None, str | None]:
        assert self.zone.reaper_heartbeat_path is not None
        try:
            heartbeat = self._heartbeat_loader(self.zone.reaper_heartbeat_path)
        except FileNotFoundError:
            return None, "waiting for the Rubato bridge inside REAPER"
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return None, f"invalid REAPER heartbeat: {exc}"
        age = self._wall_time() - heartbeat.updated_at_epoch
        if age < -2:
            return heartbeat, "REAPER heartbeat clock is ahead of the system clock"
        if age > self.zone.reaper_heartbeat_timeout_seconds:
            return heartbeat, f"REAPER bridge heartbeat is stale ({age:.1f}s old)"
        if heartbeat.state != "ready":
            return heartbeat, heartbeat.message
        if heartbeat.midi_input_name != self.zone.midi_port_name:
            return heartbeat, (
                f"REAPER is listening to {heartbeat.midi_input_name or 'no MIDI input'}; "
                f"expected {self.zone.midi_port_name}"
            )
        expected_output = self.zone.output_device_name.casefold()
        reported_output = heartbeat.audio_output_name or ""
        resolved_output = (
            self._audio_output_resolver()
            if reported_output.casefold() == "coreaudio default"
            else reported_output
        )
        actual_output = (resolved_output or "").casefold()
        if expected_output not in actual_output and actual_output not in expected_output:
            return heartbeat, (
                f"REAPER audio output is {resolved_output or reported_output or 'unknown'}; "
                f"expected {self.zone.output_device_name}"
            )
        if heartbeat.total_tracks != len(self.zone.instruments):
            return heartbeat, (
                f"REAPER reports {heartbeat.total_tracks} orchestra tracks; "
                f"expected {len(self.zone.instruments)}"
            )
        if heartbeat.sample_rate != self.zone.sample_rate:
            return heartbeat, (
                f"REAPER sample rate is {heartbeat.sample_rate or 'unknown'} Hz; "
                f"expected {self.zone.sample_rate:g} Hz"
            )
        if heartbeat.block_size is None or heartbeat.block_size > self.zone.block_size:
            return heartbeat, (
                f"REAPER block size is {heartbeat.block_size or 'unknown'} samples; "
                f"expected at most {self.zone.block_size} samples"
            )
        return heartbeat, None

    @property
    def is_alive(self) -> bool:
        if self._closed or bool(getattr(self._port, "closed", False)):
            return False
        heartbeat, problem = self._current_heartbeat()
        if heartbeat is not None and problem is None:
            self._failure_detail = None
            return True
        self._failure_detail = {
            "zone_id": self.zone.zone_id,
            "instrument_id": None,
            "exit_code": None,
            "last_stage": "reaper_heartbeat",
            "detail": problem or "REAPER bridge unavailable",
        }
        return False

    @property
    def failure_details(self) -> tuple[dict[str, Any], ...]:
        return (self._failure_detail,) if self._failure_detail is not None else ()

    def set_trace_sink(self, trace_sink: TraceSink) -> None:
        self._trace_sink = trace_sink
        # Mido's observer is intentionally replaceable at the resident lease
        # boundary so each run owns its own trace file.
        self._delegate.set_event_observer(trace_sink.write)

    def send(self, event: ScheduledAccompanimentEvent, *, sent_at: float) -> None:
        self._output.send(event, sent_at=sent_at)

    def cancel_pending(self, event_ids: Iterable[str]) -> bool:
        return self._output.cancel_pending(event_ids)

    def retime_release(self, event_id: str, *, scheduled_at: float) -> bool:
        return self._output.retime_release(event_id, scheduled_at=scheduled_at)

    def panic(self, *, sent_at: float, reason: str) -> None:
        self._output.panic(sent_at=sent_at, reason=reason)

    def panic_and_wait(
        self,
        *,
        sent_at: float,
        reason: str,
        timeout_seconds: float = 1.0,
    ) -> None:
        _ = timeout_seconds
        self.panic(sent_at=sent_at, reason=reason)

    def set_master_volume(self, volume: float, *, sent_at: float) -> None:
        self._output.set_master_volume(volume, sent_at=sent_at)

    def set_output_advance(self, output_advance_ms: float) -> None:
        # Apply the live advance to the deadline output so the orchestra can be
        # calibrated by ear during a run. This compensates the audio path that is
        # downstream of Rubato (REAPER -> BBCSO -> HDMI/eARC -> soundbar DSP) and
        # invisible to the trace. The zone's configured_output_advance_ms is the
        # persisted starting point; this overrides it live.
        self._output.set_output_advance(output_advance_ms)

    def apply_mix_automation(self, score_tick: int, *, sent_at: float) -> None:
        self._output.apply_mix_automation(score_tick, sent_at=sent_at)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._output.close()


__all__ = [
    "ReaperHeartbeat",
    "ReaperMidiRouter",
    "load_reaper_heartbeat",
]
