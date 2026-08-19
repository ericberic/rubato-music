"""Named artistic zones resolved against this machine's live audio bindings."""

from aimusic.audio.live_config import (
    LiveAudioConfig,
    LiveVstZoneConfig,
    load_live_audio_config,
    validate_live_vst_zone_files,
)
from aimusic.mixing.models import ZoneConfig, ZoneHealth


def room_zones(audio_config: LiveAudioConfig | None = None) -> tuple[ZoneConfig, ...]:
    """Return the Yamaha anchor, configured audio zones, and useful placeholders."""

    audio_config = audio_config or load_live_audio_config()
    configured = {zone.zone_id: zone for zone in audio_config.zones}
    zones = [
        ZoneConfig(
            zone_id="yamaha_anchor",
            label="Yamaha · piano side",
            renderer_id="yamaha_midi",
            acoustic_position="piano-side anchor",
            configured_output_advance_ms=0,
            residual_error_p95_ms=2,
            calibration_revision="yamaha-direct-v1",
            health=ZoneHealth.READY,
        )
    ]
    zones.extend(_configured_vst_zone(zone) for zone in audio_config.zones)
    if "room_center" not in configured:
        zones.append(
            ZoneConfig(
                zone_id="room_center",
                label="BBCSO · room center",
                renderer_id="bbcso_vst3",
                acoustic_position="wide center soundbar",
                configured_output_advance_ms=59,
                health=ZoneHealth.NEEDS_CALIBRATION,
                fallback_zone_id="yamaha_anchor",
            )
        )
    if "opposite_solo" not in configured:
        zones.append(
            ZoneConfig(
                zone_id="opposite_solo",
                label="Opposite-side solo",
                renderer_id="bbcso_vst3",
                acoustic_position="opposite side of room",
                configured_output_advance_ms=0,
                health=ZoneHealth.UNAVAILABLE,
                fallback_zone_id="room_center",
            )
        )
    return tuple(zones)


def _configured_vst_zone(zone: LiveVstZoneConfig) -> ZoneConfig:
    problems = validate_live_vst_zone_files(zone)
    return ZoneConfig(
        zone_id=zone.zone_id,
        label=zone.label,
        renderer_id="reaper_midi" if zone.renderer == "reaper" else "bbcso_vst3",
        device_id=zone.output_device_name,
        acoustic_position=zone.acoustic_position,
        configured_output_advance_ms=zone.configured_output_advance_ms,
        residual_error_p95_ms=zone.residual_error_p95_ms,
        timing_tolerance_ms=zone.timing_tolerance_ms,
        calibration_revision=zone.calibration_revision,
        health=ZoneHealth.READY if not problems else ZoneHealth.UNAVAILABLE,
        fallback_zone_id="yamaha_anchor",
    )


__all__ = ["room_zones"]
