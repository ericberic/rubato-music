"""Optional software-instrument rendering and audio-zone output."""

from aimusic.audio.plugin_host import (
    DEFAULT_BBCSO_VST3_PATH,
    audio_output_devices,
    capture_plugin_state,
    play_audio_file_default,
)

__all__ = [
    "DEFAULT_BBCSO_VST3_PATH",
    "audio_output_devices",
    "capture_plugin_state",
    "play_audio_file_default",
]
