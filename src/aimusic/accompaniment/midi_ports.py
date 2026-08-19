"""MIDI port helpers for local live-accompaniment setup."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class MidiPorts:
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    backend_available: bool = True


# Enumerate in a fresh interpreter and print one JSON line. ``ok`` distinguishes
# "backend missing" from "backend fine, no ports" (rubato#98).
_ENUMERATE_SNIPPET = (
    "import json, sys\n"
    "try:\n"
    "    import mido\n"
    "    out = {'ok': True, 'inputs': list(mido.get_input_names()),"
    " 'outputs': list(mido.get_output_names())}\n"
    "except Exception:\n"
    "    out = {'ok': False, 'inputs': [], 'outputs': []}\n"
    "sys.stdout.write(json.dumps(out))\n"
)


def list_midi_ports() -> MidiPorts:
    """Return currently visible MIDI input and output port names.

    Enumeration runs in a short-lived subprocess. On macOS, python-rtmidi's
    CoreMIDI client snapshots the device list for the life of the process, so a
    long-running server that started before a device was connected (or before a
    Yamaha power-cycle) never sees it -- re-enumerating in-process returns the
    same stale list, which is why the earlier in-process "refresh" (rubato#122)
    did not pick up hot-plugged hardware. A fresh interpreter gets a fresh
    CoreMIDI client and the current device list every time. Enumeration is an
    on-demand refresh action, not a hot path, so the process cost is fine.

    Falls back to empty lists with ``backend_available=False`` when no MIDI
    backend (the optional ``live`` extra's python-rtmidi) is available, so
    hardware-less environments don't 500 on this endpoint. That flag
    distinguishes "no backend" from "backend fine, no ports right now" -- both
    otherwise present as identical empty lists in the UI (rubato#98).
    """

    try:
        result = subprocess.run(
            [sys.executable, "-c", _ENUMERATE_SNIPPET],
            capture_output=True,
            text=True,
            timeout=5,
        )
        data = json.loads(result.stdout)
        return MidiPorts(
            inputs=tuple(data.get("inputs", ())),
            outputs=tuple(data.get("outputs", ())),
            backend_available=bool(data.get("ok")),
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return MidiPorts(inputs=(), outputs=(), backend_available=False)


_HARDWARE_KEYWORD_SUBSTRINGS = (
    "clavinova",
    "yamaha",
    "clp",
    "keyboard",
    "piano",
    "usb midi",
    "usb-midi",
    "digital piano",
    "roland",
    "kawai",
    "casio",
    "nord",
    "korg",
    "arturia",
    "studiologic",
    "privia",
    "numa",
    "widi",
    "um-one",
    "dexibell",
    "kurzweil",
)

_VIRTUAL_BUS_SUBSTRINGS = (
    "iac driver",
    "midi through",
    "virtual",
    "pianoteq",
    "keyscape",
    "ableton",
    "mainstage",
    "kontakt",
    "loopmidi",
    "rtpmidi",
    "bus",
    "synth",
)


def is_hardware_piano_port(port_name: str) -> bool:
    """Return True if the MIDI port string likely identifies physical piano hardware."""

    if not port_name:
        return False
    lower = port_name.lower()
    if any(virtual in lower for virtual in _VIRTUAL_BUS_SUBSTRINGS):
        return False
    return any(keyword in lower for keyword in _HARDWARE_KEYWORD_SUBSTRINGS)


def select_preferred_port(
    available_ports: tuple[str, ...],
    *,
    current_selection: str | None = None,
    preferred_port: str | None = None,
) -> str:
    """Select the best MIDI port name from available ports.

    Prioritizes an explicitly requested preferred port, then an existing valid
    hardware selection, then any newly discovered physical piano hardware
    (e.g. Yamaha/Clavinova), falling back to the first available port or empty string.
    """

    if not available_ports:
        return ""

    if preferred_port and preferred_port in available_ports:
        return preferred_port

    if current_selection and current_selection in available_ports:
        if is_hardware_piano_port(current_selection):
            return current_selection

    # Search for the first physical piano hardware port
    for port in available_ports:
        if is_hardware_piano_port(port):
            return port

    if current_selection and current_selection in available_ports:
        return current_selection

    return available_ports[0]


__all__ = [
    "MidiPorts",
    "is_hardware_piano_port",
    "list_midi_ports",
    "select_preferred_port",
]

