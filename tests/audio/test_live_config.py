from __future__ import annotations

from pathlib import Path

from aimusic.audio.live_config import (
    LiveAudioConfig,
    LiveVstZoneConfig,
    VstInstrumentBinding,
    load_live_audio_config,
    save_live_audio_config,
    upsert_live_vst_zone,
)
from aimusic.audio.reaper_setup import (
    create_reaper_project,
    install_reaper_bridge,
    install_reaper_startup,
    migrate_zone_to_reaper,
)
from aimusic.mixing.models import ZoneHealth
from aimusic.mixing.zones import room_zones


def zone(tmp_path: Path, *, instrument_id: str = "violins") -> LiveVstZoneConfig:
    plugin = tmp_path / "BBCSO.vst3"
    state = tmp_path / f"{instrument_id}.state"
    plugin.mkdir(exist_ok=True)
    state.write_bytes(b"state")
    return LiveVstZoneConfig(
        zone_id="room_center",
        label="BBCSO center",
        output_device_name="LG S95A",
        acoustic_position="front wall",
        configured_output_advance_ms=59,
        residual_error_p95_ms=7,
        calibration_revision="bench-1",
        instruments=(
            VstInstrumentBinding(
                instrument_id=instrument_id,
                stem_ids=("orchestra",),
                plugin_path=plugin,
                plugin_state_path=state,
            ),
        ),
    )


def test_live_audio_config_round_trips_machine_bindings(tmp_path: Path) -> None:
    path = tmp_path / "live-audio-zones.json"
    expected = LiveAudioConfig(zones=(zone(tmp_path),))

    save_live_audio_config(expected, path)

    assert load_live_audio_config(path) == expected
    assert "LG S95A" in path.read_text(encoding="utf-8")


def test_live_zone_defaults_use_the_measured_fast_bbcso_startup_path(tmp_path: Path) -> None:
    configured = zone(tmp_path)

    assert configured.plugin_initialization_timeout_seconds == 1.0
    assert configured.warmup_blocks == 1


def test_upsert_replaces_one_zone_atomically(tmp_path: Path) -> None:
    path = tmp_path / "live-audio-zones.json"
    save_live_audio_config(LiveAudioConfig(zones=(zone(tmp_path),)), path)
    replacement = zone(tmp_path, instrument_id="oboe")

    updated = upsert_live_vst_zone(replacement, path)

    assert updated.zones == (replacement,)
    assert load_live_audio_config(path) == updated


def test_room_zone_is_ready_only_with_calibrated_existing_live_files(tmp_path: Path) -> None:
    configured = LiveAudioConfig(zones=(zone(tmp_path),))

    resolved = {item.zone_id: item for item in room_zones(configured)}

    assert resolved["room_center"].health is ZoneHealth.READY
    assert resolved["room_center"].device_id == "LG S95A"
    assert resolved["room_center"].configured_output_advance_ms == 59


def test_missing_patch_state_fails_zone_closed(tmp_path: Path) -> None:
    configured_zone = zone(tmp_path)
    configured_zone.instruments[0].plugin_state_path.unlink()

    resolved = {
        item.zone_id: item for item in room_zones(LiveAudioConfig(zones=(configured_zone,)))
    }

    assert resolved["room_center"].health is ZoneHealth.UNAVAILABLE


def test_reaper_migration_preserves_group_routing_and_requires_project(tmp_path: Path) -> None:
    app = tmp_path / "REAPER.app"
    app.mkdir()
    project = create_reaper_project(tmp_path / "Rubato Orchestra.RPP")
    migrated = migrate_zone_to_reaper(
        zone(tmp_path),
        app_path=app,
        project_path=project,
        heartbeat_path=tmp_path / "renderer-status.json",
    )

    assert migrated.renderer == "reaper"
    assert migrated.midi_port_name == "Rubato Orchestra"
    assert migrated.instruments[0].stem_ids == ("orchestra",)
    assert migrated.configured_output_advance_ms == 0
    assert migrated.sample_rate == 48_000
    assert migrated.block_size == 128
    assert "SAMPLERATE 48000 1 0" in project.read_text(encoding="utf-8")
    assert room_zones(LiveAudioConfig(zones=(migrated,)))[1].renderer_id == "reaper_midi"


def test_reaper_bridge_is_installed_in_a_stable_scripts_location(tmp_path: Path) -> None:
    source = tmp_path / "source.lua"
    source.write_text("reaper.defer(loop)\n", encoding="utf-8")
    destination = tmp_path / "Scripts" / "Rubato" / "rubato_reaper_bridge.lua"

    installed = install_reaper_bridge(source, destination)

    assert installed == destination
    assert installed.read_text(encoding="utf-8") == "reaper.defer(loop)\n"


def test_reaper_startup_install_refuses_to_replace_user_script(tmp_path: Path) -> None:
    source = tmp_path / "rubato-startup.lua"
    source.write_text("-- Rubato managed REAPER startup bridge.\n", encoding="utf-8")
    destination = tmp_path / "Scripts" / "__startup.lua"

    installed = install_reaper_startup(source, destination)

    assert installed.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    destination.write_text("-- My existing setup\n", encoding="utf-8")
    try:
        install_reaper_startup(source, destination)
    except FileExistsError:
        pass
    else:
        raise AssertionError("user startup script should not be replaced")
