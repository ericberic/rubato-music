"""Unit tests for MIDI port listing and preferred port selection heuristics."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from aimusic.accompaniment.midi_ports import (
    is_hardware_piano_port,
    list_midi_ports,
    select_preferred_port,
)


def test_is_hardware_piano_port_identifies_major_hardware_vendors() -> None:
    assert is_hardware_piano_port("Clavinova")
    assert is_hardware_piano_port("Yamaha CLP-795GP")
    assert is_hardware_piano_port("Roland FP-30X")
    assert is_hardware_piano_port("Roland RD-2000")
    assert is_hardware_piano_port("Kawai MP11SE")
    assert is_hardware_piano_port("Kawai VPC1")
    assert is_hardware_piano_port("Casio Privia PX-S1100")
    assert is_hardware_piano_port("Nord Stage 3")
    assert is_hardware_piano_port("Arturia KeyLab 88")
    assert is_hardware_piano_port("UM-ONE")
    assert is_hardware_piano_port("WIDI Master")
    assert is_hardware_piano_port("USB MIDI Interface")
    assert is_hardware_piano_port("Digital Piano Port 1")


def test_is_hardware_piano_port_rejects_virtual_buses_and_software_synths() -> None:
    assert not is_hardware_piano_port("IAC Driver Bus 1")
    assert not is_hardware_piano_port("Midi Through Port-0")
    assert not is_hardware_piano_port("Pianoteq 8")
    assert not is_hardware_piano_port("Keyscape Piano")
    assert not is_hardware_piano_port("Ableton Live Piano Bus")
    assert not is_hardware_piano_port("MainStage Piano Output")
    assert not is_hardware_piano_port("Kontakt 7 Piano Out")
    assert not is_hardware_piano_port("loopMIDI Piano")
    assert not is_hardware_piano_port("")


def test_select_preferred_port_empty_returns_empty_string() -> None:
    assert select_preferred_port(()) == ""


def test_select_preferred_port_prioritizes_user_explicit_preference_even_virtual() -> None:
    ports = ("IAC Driver Bus 1", "Clavinova", "Yamaha CLP-795GP")
    # If user explicitly selected IAC Driver, it should be honored
    assert select_preferred_port(ports, preferred_port="IAC Driver Bus 1") == "IAC Driver Bus 1"
    assert select_preferred_port(ports, preferred_port="Yamaha CLP-795GP") == "Yamaha CLP-795GP"


def test_select_preferred_port_promotes_reconnected_hardware_over_virtual_bus() -> None:
    # When Yamaha was unplugged, selection fell back to IAC Driver Bus 1.
    # When Yamaha (Clavinova) is reconnected, it should promote Clavinova if no explicit preference.
    ports = ("IAC Driver Bus 1", "Clavinova")
    assert select_preferred_port(ports, current_selection="IAC Driver Bus 1") == "Clavinova"


def test_select_preferred_port_retains_valid_hardware_selection() -> None:
    ports = ("IAC Driver Bus 1", "Clavinova")
    assert select_preferred_port(ports, current_selection="Clavinova") == "Clavinova"


def test_select_preferred_port_falls_back_to_first_port_when_no_hardware() -> None:
    ports = ("Virtual Bus A", "Virtual Bus B")
    assert select_preferred_port(ports) == "Virtual Bus A"


def test_list_midi_ports_reports_fresh_subprocess_enumeration() -> None:
    # Enumeration runs out-of-process so a hot-plugged device is seen without a
    # server restart; a fresh interpreter reports current ports as JSON.
    completed = subprocess.CompletedProcess(
        args=(),
        returncode=0,
        stdout=json.dumps({"ok": True, "inputs": ["Clavinova"], "outputs": ["Clavinova"]}),
    )
    with patch("aimusic.accompaniment.midi_ports.subprocess.run", return_value=completed):
        res = list_midi_ports()
    assert res.backend_available
    assert res.inputs == ("Clavinova",)
    assert res.outputs == ("Clavinova",)


def test_list_midi_ports_reports_missing_backend() -> None:
    # The subprocess signals a missing/failed MIDI backend with ok=false, which
    # must present as backend_available=False rather than an empty-but-fine list.
    completed = subprocess.CompletedProcess(
        args=(),
        returncode=0,
        stdout=json.dumps({"ok": False, "inputs": [], "outputs": []}),
    )
    with patch("aimusic.accompaniment.midi_ports.subprocess.run", return_value=completed):
        res = list_midi_ports()
    assert not res.backend_available
    assert res.inputs == ()
    assert res.outputs == ()


def test_list_midi_ports_survives_subprocess_failure() -> None:
    with patch(
        "aimusic.accompaniment.midi_ports.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="python", timeout=5),
    ):
        res = list_midi_ports()
    assert not res.backend_available
    assert res.inputs == ()
    assert res.outputs == ()

