from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from aimusic.mixing.models import (
    EnvelopePoint,
    MixProgram,
    MixRegion,
    MixRoute,
    ZoneConfig,
    ZoneHealth,
)
from aimusic.mixing.policy import compile_mix_policy


def sample_program() -> MixProgram:
    return MixProgram(
        program_id="main",
        name="Main mix",
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
                region_id="m45_swell",
                start_tick=4_000,
                end_tick=8_000,
                gesture="swell",
                routes=(
                    MixRoute(
                        zone_id="room_center",
                        stem_ids=("orchestra",),
                        envelope=(
                            EnvelopePoint(score_tick=4_000, level=15),
                            EnvelopePoint(score_tick=8_000, level=100),
                        ),
                    ),
                ),
            ),
        ),
    )


def zones(*, room_health: ZoneHealth = ZoneHealth.READY) -> tuple[ZoneConfig, ...]:
    return (
        ZoneConfig(
            zone_id="yamaha_anchor",
            label="Yamaha",
            renderer_id="midi",
            acoustic_position="piano",
            configured_output_advance_ms=0,
            residual_error_p95_ms=2,
            health=ZoneHealth.READY,
        ),
        ZoneConfig(
            zone_id="room_center",
            label="Room",
            renderer_id="bbcso",
            acoustic_position="center",
            configured_output_advance_ms=59,
            residual_error_p95_ms=10,
            health=room_health,
            fallback_zone_id="yamaha_anchor",
        ),
    )


def test_program_round_trips_and_rejects_envelope_outside_region() -> None:
    program = sample_program()
    assert MixProgram.model_validate_json(program.model_dump_json()) == program

    with pytest.raises(ValidationError, match="inside their region"):
        MixRegion(
            region_id="bad",
            start_tick=100,
            end_tick=200,
            routes=(
                MixRoute(
                    zone_id="room_center",
                    stem_ids=("orchestra",),
                    envelope=(EnvelopePoint(score_tick=201, level=100),),
                ),
            ),
        )


def test_policy_interpolates_swell_and_maps_100_scale_to_audio_and_cc7() -> None:
    policy = compile_mix_policy(sample_program(), zones())

    assert policy.level_at("yamaha_anchor", "orchestra", 3_000) == 25
    assert policy.level_at("room_center", "orchestra", 4_000) == 15
    assert policy.level_at("room_center", "orchestra", 8_000) == 100
    assert 15 < policy.level_at("room_center", "orchestra", 6_000) < 100
    assert policy.audio_gain_at("yamaha_anchor", "orchestra", 3_000) == 0.25
    assert policy.midi_cc7_at("yamaha_anchor", "orchestra", 3_000) == 32


def test_unready_room_route_falls_back_without_changing_authored_program() -> None:
    policy = compile_mix_policy(
        sample_program(), zones(room_health=ZoneHealth.NEEDS_CALIBRATION)
    )

    room_default = policy.default_routes[1]
    assert room_default.requested_zone_id == "room_center"
    assert room_default.active_zone_id == "yamaha_anchor"
    assert room_default.readiness == "fallback:yamaha_anchor"
    assert sample_program().default_routes[1].zone_id == "room_center"


def test_audible_gain_floors_accidental_silence_but_not_intentional_balance() -> None:
    # A room-zone envelope that dips to 0 and falls back to the audible Yamaha:
    # the m.44 ducking shape. On the real audible output it must never fully
    # silence, but the floor must not touch louder, intentional balance levels.
    program = sample_program().model_copy(
        update={
            "regions": (
                MixRegion(
                    region_id="duck",
                    start_tick=0,
                    end_tick=1_000,
                    gesture="custom",
                    routes=(
                        MixRoute(
                            zone_id="room_center",
                            stem_ids=("orchestra",),
                            envelope=(
                                EnvelopePoint(score_tick=0, level=0),
                                EnvelopePoint(score_tick=999, level=0),
                            ),
                            fallback_zone_id="yamaha_anchor",
                        ),
                    ),
                ),
            ),
        }
    )
    policy = compile_mix_policy(program, zones(room_health=ZoneHealth.NEEDS_CALIBRATION))

    # Without the floor the ducked, fallen-back envelope mutes the audible Yamaha.
    assert policy.audio_gain_for_part_at("yamaha_anchor", "orchestra", 500) == 0.0
    assert policy.audible_gain_for_part_at("yamaha_anchor", "orchestra", 500, floor=0.2) == 0.2

    # A routing gap (unknown zone) is likewise floored, never silent.
    assert policy.audio_gain_for_part_at("nonexistent_zone", "orchestra", 500) == 0.0
    assert (
        policy.audible_gain_for_part_at("nonexistent_zone", "orchestra", 500, floor=0.2) == 0.2
    )

    # The floor never lowers an already-audible, intentionally-authored level.
    loud = policy.audio_gain_for_part_at("yamaha_anchor", "orchestra", 2_000)
    assert loud > 0.2
    assert policy.audible_gain_for_part_at("yamaha_anchor", "orchestra", 2_000, floor=0.2) == loud

    with pytest.raises(ValueError):
        policy.audible_gain_for_part_at("yamaha_anchor", "orchestra", 2_000, floor=1.5)


def test_part_specific_route_overrides_orchestra_route_and_region_end_is_half_open() -> None:
    program = sample_program().model_copy(
        update={
            "default_routes": (
                MixRoute(zone_id="yamaha_anchor", stem_ids=("orchestra",), level=25),
                MixRoute(zone_id="yamaha_anchor", stem_ids=("oboe",), level=60),
                MixRoute(zone_id="room_center", stem_ids=("orchestra",), level=100),
            )
        }
    )
    policy = compile_mix_policy(program, zones())

    assert policy.level_for_part_at("yamaha_anchor", "oboe", 3_000) == 60
    assert policy.level_for_part_at("yamaha_anchor", "strings", 3_000) == 25
    assert policy.level_at("room_center", "orchestra", 7_999) < 100
    assert policy.level_at("room_center", "orchestra", 8_000) == 100


def test_program_rejects_order_dependent_region_overlap() -> None:
    first = sample_program().regions[0]
    with pytest.raises(ValidationError, match="cannot overlap"):
        MixProgram.model_validate(
            sample_program().model_dump()
            | {
                "regions": (
                    first,
                    first.model_copy(
                        update={
                            "region_id": "overlap",
                            "start_tick": 7_000,
                            "end_tick": 9_000,
                            "routes": (
                                MixRoute(
                                    zone_id="room_center",
                                    stem_ids=("orchestra",),
                                    level=50,
                                ),
                            ),
                        }
                    ),
                )
            }
        )
