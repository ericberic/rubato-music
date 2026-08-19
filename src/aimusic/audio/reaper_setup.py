"""One-time machine setup for the external REAPER orchestra host."""

from __future__ import annotations

import shutil
from pathlib import Path

from aimusic.audio.live_config import LiveVstZoneConfig
from aimusic.core import paths

DEFAULT_REAPER_APP = Path("/Applications/REAPER.app")
DEFAULT_REAPER_MIDI_PORT = "Rubato Orchestra"
RUBATO_STARTUP_MARKER = "-- Rubato managed REAPER startup bridge."


def reaper_state_dir() -> Path:
    return paths.state_root() / "reaper"


def default_reaper_project_path() -> Path:
    return reaper_state_dir() / "Rubato Orchestra.RPP"


def default_reaper_heartbeat_path() -> Path:
    return reaper_state_dir() / "renderer-status.json"


def default_reaper_bridge_path() -> Path:
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "REAPER"
        / "Scripts"
        / "Rubato"
        / "rubato_reaper_bridge.lua"
    )


def default_reaper_startup_path() -> Path:
    return default_reaper_bridge_path().parents[1] / "__startup.lua"


def install_reaper_bridge(source: Path, destination: Path | None = None) -> Path:
    """Install the bundled bridge where REAPER's ReaScript chooser starts."""

    if not source.is_file():
        raise FileNotFoundError(f"REAPER bridge is missing: {source}")
    installed = destination or default_reaper_bridge_path()
    installed.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, installed)
    return installed


def install_reaper_startup(source: Path, destination: Path | None = None) -> Path:
    """Install Rubato's REAPER startup hook without replacing user automation."""

    if not source.is_file():
        raise FileNotFoundError(f"REAPER startup hook is missing: {source}")
    installed = destination or default_reaper_startup_path()
    if installed.exists() and RUBATO_STARTUP_MARKER not in installed.read_text(
        encoding="utf-8"
    ):
        raise FileExistsError(
            f"Refusing to replace an existing REAPER startup script: {installed}"
        )
    installed.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, installed)
    return installed


def create_reaper_project(path: Path | None = None) -> Path:
    """Create a minimal project that the bridge can populate without data loss."""

    project = path or default_reaper_project_path()
    project.parent.mkdir(parents=True, exist_ok=True)
    if project.exists():
        return project
    project.write_text(
        '<REAPER_PROJECT 0.1 "7.78/OSX-arm64" 1786716000\n'
        "  RIPPLE 0\n"
        "  GROUPOVERRIDE 0 0 0\n"
        "  AUTOXFADE 1\n"
        "  SAMPLERATE 48000 1 0\n"
        "  TEMPO 120 4 4\n"
        ">\n",
        encoding="utf-8",
    )
    return project


def migrate_zone_to_reaper(
    zone: LiveVstZoneConfig,
    *,
    app_path: Path = DEFAULT_REAPER_APP,
    project_path: Path | None = None,
    heartbeat_path: Path | None = None,
    midi_port_name: str = DEFAULT_REAPER_MIDI_PORT,
) -> LiveVstZoneConfig:
    """Preserve four-group musical routing while replacing the audio host."""

    project = project_path or default_reaper_project_path()
    heartbeat = heartbeat_path or default_reaper_heartbeat_path()
    return zone.model_copy(
        update={
            "renderer": "reaper",
            "label": "Living room (LG) — REAPER/BBCSO 4-group",
            # Start honest. The prior 59 ms was an HDMI planning assumption,
            # not a measurement of this new DAW path.
            "configured_output_advance_ms": 0.0,
            "residual_error_p95_ms": 0.0,
            "calibration_revision": "reaper-experiment-unmeasured",
            "midi_port_name": midi_port_name,
            "reaper_app_path": app_path,
            "reaper_project_path": project,
            "reaper_heartbeat_path": heartbeat,
            "sample_rate": 48_000.0,
            "block_size": 128,
        }
    )


__all__ = [
    "DEFAULT_REAPER_APP",
    "DEFAULT_REAPER_MIDI_PORT",
    "RUBATO_STARTUP_MARKER",
    "create_reaper_project",
    "default_reaper_bridge_path",
    "default_reaper_heartbeat_path",
    "default_reaper_project_path",
    "default_reaper_startup_path",
    "install_reaper_bridge",
    "install_reaper_startup",
    "migrate_zone_to_reaper",
    "reaper_state_dir",
]
