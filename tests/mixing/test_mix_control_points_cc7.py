"""The mix program's authored control points must drive real per-zone CC7.

These exercise the seam the live performance actually uses: a compiled MixPolicy
resolving a route envelope, fed through ``MidoAccompanimentOutput`` (the adapter
that emits channel-volume CC7). They lock in that

  * control points interpolate between anchors and reproduce them exactly,
  * two zones (Clavinova floor + living-room BBCSO) carry independent curves, and
  * the global master volume scales every zone proportionally.
"""

from __future__ import annotations

from datetime import datetime, timezone

import mido

from aimusic.accompaniment.midi_output import MidoAccompanimentOutput
from aimusic.accompaniment.score_bundle import InstrumentMapEntry
from aimusic.mixing.models import (
    Curve,
    EnvelopePoint,
    Gesture,
    MixProgram,
    MixRegion,
    MixRoute,
    ZoneConfig,
    ZoneHealth,
)
from aimusic.mixing.policy import MixPolicy, compile_mix_policy

M76, M77, M78, M79 = 288_000, 291_840, 295_680, 299_520


class _FakePort:
    def __init__(self, _name: str) -> None:
        self.messages: list[mido.Message] = []

    def send(self, message: mido.Message) -> None:
        self.messages.append(message.copy())

    def close(self) -> None:
        pass


def _ready_zones() -> tuple[ZoneConfig, ...]:
    common = dict(
        configured_output_advance_ms=0,
        residual_error_p95_ms=2,
        health=ZoneHealth.READY,
        calibration_revision="v1",
    )
    return (
        ZoneConfig(
            zone_id="yamaha_anchor",
            label="Clavinova",
            renderer_id="yamaha_midi",
            acoustic_position="piano",
            **common,
        ),
        ZoneConfig(
            zone_id="room_center",
            label="Living room",
            renderer_id="reaper_midi",
            device_id="PHL 328E1",
            acoustic_position="room",
            fallback_zone_id="yamaha_anchor",
            **common,
        ),
    )


def _swell_program() -> MixProgram:
    """25:100 base; swell Clavinova 25->50->25 while the room relaxes 100->50."""

    return MixProgram(
        program_id="swell",
        name="Swell",
        piece_id="chopin_op11",
        movement=2,
        score_bundle_id="bundle",
        score_bundle_revision="r1",
        timeline_digest="digest",
        updated_at=datetime.now(timezone.utc),
        default_routes=(
            MixRoute(zone_id="yamaha_anchor", stem_ids=("orchestra",), level=25),
            MixRoute(zone_id="room_center", stem_ids=("orchestra",), level=100),
        ),
        regions=(
            MixRegion(
                region_id="m76_swell",
                start_tick=M76,
                end_tick=M79,
                gesture=Gesture.SWELL,
                routes=(
                    MixRoute(
                        zone_id="yamaha_anchor",
                        stem_ids=("orchestra",),
                        level=25,
                        envelope=(
                            EnvelopePoint(score_tick=M76, level=25, curve=Curve.EQUAL_POWER),
                            EnvelopePoint(score_tick=M77, level=50, curve=Curve.EQUAL_POWER),
                            EnvelopePoint(score_tick=M78, level=25, curve=Curve.EQUAL_POWER),
                        ),
                    ),
                    MixRoute(
                        zone_id="room_center",
                        stem_ids=("orchestra",),
                        level=100,
                        envelope=(
                            EnvelopePoint(score_tick=M76, level=100, curve=Curve.EQUAL_POWER),
                            EnvelopePoint(score_tick=M77, level=100, curve=Curve.EQUAL_POWER),
                            EnvelopePoint(score_tick=M78, level=50, curve=Curve.EQUAL_POWER),
                        ),
                    ),
                ),
            ),
        ),
    )


def _cc7_at(policy: MixPolicy, zone_id: str, score_tick: int, master: float = 1.0) -> int:
    """Effective CC7 the adapter would hold for the zone's channel at score_tick."""

    port = _FakePort("sim")
    adapter = MidoAccompanimentOutput(
        "sim",
        (InstrumentMapEntry(part_id="orchestra", channel=0, program=0, name="o", volume=100),),
        port_factory=lambda _name: port,
        master_volume=master,
        mix_level_resolver=lambda part_id, tick: policy.audio_gain_for_part_at(
            zone_id, part_id, tick
        ),
        autostart=False,
    )
    adapter.apply_mix_automation(score_tick, sent_at=0.0)
    value = adapter._channel_last_volume_value[0]
    adapter.close()
    return value


def test_both_zones_compile_active_not_fallback() -> None:
    policy = compile_mix_policy(_swell_program(), _ready_zones())
    active = {route.requested_zone_id: route.active_zone_id for route in policy.default_routes}
    assert active == {"yamaha_anchor": "yamaha_anchor", "room_center": "room_center"}


def test_control_points_reproduced_exactly_at_anchors() -> None:
    policy = compile_mix_policy(_swell_program(), _ready_zones())
    # base_volume 100 and master 1.0 make CC7 == authored level.
    assert _cc7_at(policy, "yamaha_anchor", M76) == 25
    assert _cc7_at(policy, "yamaha_anchor", M77) == 50
    assert _cc7_at(policy, "yamaha_anchor", M78) == 25
    assert _cc7_at(policy, "room_center", M76) == 100
    assert _cc7_at(policy, "room_center", M77) == 100
    assert _cc7_at(policy, "room_center", M78) == 50


def test_envelope_interpolates_monotonically_between_anchors() -> None:
    policy = compile_mix_policy(_swell_program(), _ready_zones())
    mid_up = _cc7_at(policy, "yamaha_anchor", (M76 + M77) // 2)
    assert 25 < mid_up < 50  # swelling up
    mid_down = _cc7_at(policy, "room_center", (M77 + M78) // 2)
    assert 50 < mid_down < 100  # relaxing down


def test_master_volume_scales_every_zone_proportionally() -> None:
    policy = compile_mix_policy(_swell_program(), _ready_zones())
    # At m77 the authored levels are Clavinova 50, room 100.
    assert _cc7_at(policy, "yamaha_anchor", M77, master=1.0) == 50
    assert _cc7_at(policy, "room_center", M77, master=1.0) == 100
    assert _cc7_at(policy, "yamaha_anchor", M77, master=0.5) == 25
    assert _cc7_at(policy, "room_center", M77, master=0.5) == 50


def test_region_scope_restores_default_after_end_tick() -> None:
    # Envelopes apply only inside [start_tick, end_tick); the default resumes after.
    policy = compile_mix_policy(_swell_program(), _ready_zones())
    assert _cc7_at(policy, "room_center", M79) == 100  # back to the 100 default
