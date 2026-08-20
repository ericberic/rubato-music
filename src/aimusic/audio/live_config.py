"""Machine-local bindings from artistic zone names to live VST renderers.

The score-authored :class:`MixProgram` deliberately names stable zones and
stems only.  Plug-in paths, captured patch state, CoreAudio device names, and
buffer sizes belong to this machine and room, so they live in a separate
configuration document under Rubato's application state directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aimusic.core import paths


class LiveAudioConfigModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class VstInstrumentBinding(LiveAudioConfigModel):
    instrument_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    stem_ids: tuple[str, ...] = Field(min_length=1)
    plugin_path: Path
    plugin_state_path: Path
    midi_channel: int = Field(default=0, ge=0, le=15)

    @field_validator("stem_ids")
    @classmethod
    def validate_stems(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        stems = tuple(stem.strip() for stem in value)
        if any(not stem for stem in stems):
            raise ValueError("stem ids cannot be blank")
        if len(stems) != len(set(stems)):
            raise ValueError("stem ids must be unique")
        return stems


class LiveVstZoneConfig(LiveAudioConfigModel):
    # ``pedalboard`` is retained as a rollback path while the external REAPER
    # host is proven on target hardware.  REAPER owns the plug-in and CoreAudio;
    # Rubato owns only a virtual CoreMIDI source and scheduled MIDI messages.
    renderer: Literal["pedalboard", "reaper"] = "pedalboard"
    zone_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    label: str = Field(min_length=1)
    output_device_name: str = Field(min_length=1)
    acoustic_position: str = Field(min_length=1)
    configured_output_advance_ms: float = Field(default=59.0, ge=0, le=500)
    residual_error_p95_ms: float = Field(ge=0)
    timing_tolerance_ms: float = Field(default=40.0, gt=0)
    calibration_revision: str = Field(min_length=1)
    sample_rate: float = Field(default=48_000.0, gt=0)
    block_size: int = Field(default=512, ge=64, le=4096)
    prefill_blocks: int = Field(default=2, ge=1, le=8)
    # BBCSO treats Pedalboard's initialization timeout as a grace period, not
    # merely a ceiling.  A 60-second value therefore costs roughly 60 seconds
    # *per instance* and can outrun the parent worker's readiness deadline.
    # The measured local path returns in ~2 seconds with a one-second grace.
    plugin_initialization_timeout_seconds: float = Field(default=1.0, gt=0, le=300)
    # Prime one silent block before exposing the stream.  This pays BBCSO's
    # first-render setup cost before the first musical event is scheduled.
    warmup_blocks: int = Field(default=1, ge=0, le=8)
    midi_port_name: str | None = None
    reaper_app_path: Path | None = None
    reaper_project_path: Path | None = None
    reaper_heartbeat_path: Path | None = None
    reaper_heartbeat_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    instruments: tuple[VstInstrumentBinding, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_bindings(self) -> "LiveVstZoneConfig":
        ids = [binding.instrument_id for binding in self.instruments]
        if len(ids) != len(set(ids)):
            raise ValueError("instrument ids must be unique within a zone")
        if self.renderer == "reaper":
            required = {
                "midi_port_name": self.midi_port_name,
                "reaper_app_path": self.reaper_app_path,
                "reaper_project_path": self.reaper_project_path,
                "reaper_heartbeat_path": self.reaper_heartbeat_path,
            }
            missing = tuple(
                name for name, value in required.items() if value is None or value == ""
            )
            if missing:
                raise ValueError("REAPER zones require " + ", ".join(missing))
        return self


class LiveAudioConfig(LiveAudioConfigModel):
    schema_version: int = Field(default=1, ge=1)
    zones: tuple[LiveVstZoneConfig, ...] = ()

    @model_validator(mode="after")
    def validate_zones(self) -> "LiveAudioConfig":
        ids = [zone.zone_id for zone in self.zones]
        if len(ids) != len(set(ids)):
            raise ValueError("live audio zone ids must be unique")
        return self


def load_live_audio_config(path: Path | None = None) -> LiveAudioConfig:
    document = path or paths.live_audio_config_path()
    if not document.exists():
        return LiveAudioConfig()
    return LiveAudioConfig.model_validate_json(document.read_text(encoding="utf-8"))


def save_live_audio_config(config: LiveAudioConfig, path: Path | None = None) -> LiveAudioConfig:
    document = path or paths.live_audio_config_path()
    document.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(document.with_suffix(document.suffix + ".lock"), timeout=10)
    with lock:
        temp = document.with_suffix(document.suffix + ".tmp")
        temp.write_text(config.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temp.replace(document)
    return config


def upsert_live_vst_zone(zone: LiveVstZoneConfig, path: Path | None = None) -> LiveAudioConfig:
    current = load_live_audio_config(path)
    zones = tuple(item for item in current.zones if item.zone_id != zone.zone_id) + (zone,)
    return save_live_audio_config(current.model_copy(update={"zones": zones}), path)


def validate_live_vst_zone_files(zone: LiveVstZoneConfig) -> tuple[str, ...]:
    problems: list[str] = []
    if zone.renderer == "reaper":
        assert zone.reaper_app_path is not None
        assert zone.reaper_project_path is not None
        if not zone.reaper_app_path.exists():
            problems.append(f"REAPER app missing: {zone.reaper_app_path}")
        if not zone.reaper_project_path.exists():
            problems.append(f"REAPER project missing: {zone.reaper_project_path}")
        # The heartbeat is intentionally not a static zone-health prerequisite:
        # it exists only while the Rubato bridge is running inside REAPER.  The
        # renderer lifecycle reports that dynamic readiness separately.
        return tuple(problems)
    for instrument in zone.instruments:
        if not instrument.plugin_path.exists():
            problems.append(
                f"{instrument.instrument_id}: plug-in missing: {instrument.plugin_path}"
            )
        if not instrument.plugin_state_path.exists():
            problems.append(
                f"{instrument.instrument_id}: state missing: {instrument.plugin_state_path}"
            )
    return tuple(problems)


__all__ = [
    "LiveAudioConfig",
    "LiveVstZoneConfig",
    "VstInstrumentBinding",
    "load_live_audio_config",
    "save_live_audio_config",
    "upsert_live_vst_zone",
    "validate_live_vst_zone_files",
]
