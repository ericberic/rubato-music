"""Opt-in helpers for configuring the live VST host.

These helpers enumerate devices and capture reusable BBCSO state. They never
render a score or create an audio file; musical playback belongs exclusively to
the live zone worker.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

DEFAULT_BBCSO_VST3_PATH = Path("/Library/Audio/Plug-Ins/VST3/BBC Symphony Orchestra.vst3")


class AudioDependencyError(RuntimeError):
    pass


def _pedalboard_api():
    try:
        import pedalboard
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise AudioDependencyError(
            "Pedalboard is not installed; run `uv sync --extra audio`."
        ) from exc
    return pedalboard


def capture_plugin_state(
    plugin_path: Path,
    output_path: Path,
    *,
    initial_state_path: Path | None = None,
) -> Path:
    """Open the editor from an optional prior state and persist it on close."""

    if not plugin_path.exists():
        raise FileNotFoundError(f"VST3 plug-in is missing: {plugin_path}")
    if initial_state_path is not None and not initial_state_path.is_file():
        raise FileNotFoundError(f"initial plug-in state is missing: {initial_state_path}")
    pedalboard = _pedalboard_api()
    plugin = pedalboard.load_plugin(str(plugin_path), initialization_timeout=60.0)
    if initial_state_path is not None:
        plugin.raw_state = initial_state_path.read_bytes()
    plugin.show_editor()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(plugin.raw_state)
    return output_path


def audio_output_devices() -> tuple[str, str | None, tuple[str, ...]]:
    """Return backend name, default output, and available output names."""

    _pedalboard_api()
    from pedalboard.io import AudioStream

    return (
        "Pedalboard AudioStream",
        AudioStream.default_output_device_name,
        tuple(AudioStream.output_device_names),
    )


def play_audio_file_default(path: Path) -> None:
    """Play a rendered file through the normal macOS default-output route."""

    if sys.platform != "darwin":
        raise RuntimeError("default file playback is currently supported only on macOS")
    if not path.is_file():
        raise FileNotFoundError(f"audio file is missing: {path}")
    subprocess.run(["/usr/bin/afplay", str(path)], check=True)


__all__ = [
    "AudioDependencyError",
    "DEFAULT_BBCSO_VST3_PATH",
    "audio_output_devices",
    "capture_plugin_state",
    "play_audio_file_default",
]
