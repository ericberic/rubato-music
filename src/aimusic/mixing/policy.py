"""Compile an authored mix document with room-zone readiness."""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field

from aimusic.mixing.models import (
    Curve,
    EnvelopePoint,
    MixProgram,
    MixRoute,
    ZoneConfig,
    ZoneHealth,
)


class CompiledRoute(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    requested_zone_id: str
    active_zone_id: str | None
    stem_ids: tuple[str, ...]
    level: float = Field(ge=0, le=100)
    envelope: tuple[EnvelopePoint, ...] = ()
    readiness: str


class CompiledRegion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    region_id: str
    start_tick: int
    end_tick: int
    gesture: str
    routes: tuple[CompiledRoute, ...]
    enabled: bool


class MixPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    program_id: str
    program_revision: int
    level_mapping_revision: str
    calibration_revisions: dict[str, str | None]
    default_routes: tuple[CompiledRoute, ...]
    regions: tuple[CompiledRegion, ...]

    def level_at(self, zone_id: str, stem_id: str, score_tick: int) -> float:
        candidates = self._routes_at(zone_id, stem_id, score_tick)
        if not candidates:
            return 0.0
        # Multiple routes inside one region can converge on a fallback zone.
        # Combining by maximum is deterministic and preserves the audible route
        # instead of making persisted list order an undocumented mix operator.
        return max(_route_level(route, score_tick) for route in candidates)

    def level_for_part_at(self, zone_id: str, part_id: str, score_tick: int) -> float:
        """Resolve a score part, falling back to the whole-orchestra stem."""

        exact = self._routes_at(zone_id, part_id, score_tick)
        candidates = exact or self._routes_at(zone_id, "orchestra", score_tick)
        if not candidates:
            return 0.0
        return max(_route_level(route, score_tick) for route in candidates)

    def _routes_at(
        self, zone_id: str, stem_id: str, score_tick: int
    ) -> list[CompiledRoute]:
        defaults = [
            route
            for route in self.default_routes
            if route.active_zone_id == zone_id and stem_id in route.stem_ids
        ]
        regional: list[CompiledRoute] = []
        for region in self.regions:
            if not region.enabled or not region.start_tick <= score_tick < region.end_tick:
                continue
            regional.extend(
                route
                for route in region.routes
                if route.active_zone_id == zone_id and stem_id in route.stem_ids
            )
        return regional or defaults

    def audio_gain_at(self, zone_id: str, stem_id: str, score_tick: int) -> float:
        """Map authored volume to deterministic linear amplitude.

        The mapping is intentionally pinned by ``level_mapping_revision``.
        A future perceptual taper can be introduced as a new revision without
        silently changing existing programs.
        """

        if self.level_mapping_revision != "volume-linear-v1":
            raise ValueError(f"unsupported level mapping: {self.level_mapping_revision}")
        return self.level_at(zone_id, stem_id, score_tick) / 100.0

    def audio_gain_for_part_at(
        self, zone_id: str, part_id: str, score_tick: int
    ) -> float:
        if self.level_mapping_revision != "volume-linear-v1":
            raise ValueError(f"unsupported level mapping: {self.level_mapping_revision}")
        return self.level_for_part_at(zone_id, part_id, score_tick) / 100.0

    def audible_gain_for_part_at(
        self, zone_id: str, part_id: str, score_tick: int, *, floor: float
    ) -> float:
        """Resolved gain that a real audible output may never silence by accident.

        A routing gap returns 0 (``level_for_part_at``), and an uncalibrated zone
        falls back to another, so a reverb-shaping envelope authored for one zone
        can ride onto the actual speakers and mute them -- exactly what dropped
        the Clavinova orchestra on the m.44 trigger beats. The mix decides
        balance, not existence: a floor keeps the audible orchestra present. A
        performer who truly wants silence uses the global orchestra mute, which is
        a separate control applied after this one, so the floor never traps them.
        """

        if not 0.0 <= floor <= 1.0:
            raise ValueError("floor must be within [0, 1]")
        return max(floor, self.audio_gain_for_part_at(zone_id, part_id, score_tick))

    def midi_cc7_at(self, zone_id: str, stem_id: str, score_tick: int) -> int:
        return round(self.level_at(zone_id, stem_id, score_tick) * 127 / 100)


def compile_mix_policy(
    program: MixProgram,
    zones: tuple[ZoneConfig, ...],
    *,
    dispatch_horizon_ms: float = 100,
    planning_horizon_ms: float = 500,
) -> MixPolicy:
    by_id = {zone.zone_id: zone for zone in zones}
    if len(by_id) != len(zones):
        raise ValueError("zone ids must be unique")
    compiled_defaults = tuple(
        _compile_route(route, by_id, dispatch_horizon_ms, planning_horizon_ms)
        for route in program.default_routes
    )
    compiled_regions = tuple(
        CompiledRegion(
            region_id=region.region_id,
            start_tick=region.start_tick,
            end_tick=region.end_tick,
            gesture=region.gesture.value,
            routes=tuple(
                _compile_route(route, by_id, dispatch_horizon_ms, planning_horizon_ms)
                for route in region.routes
            ),
            enabled=region.enabled,
        )
        for region in program.regions
    )
    return MixPolicy(
        program_id=program.program_id,
        program_revision=program.revision,
        level_mapping_revision=program.level_mapping_revision,
        calibration_revisions={zone.zone_id: zone.calibration_revision for zone in zones},
        default_routes=compiled_defaults,
        regions=compiled_regions,
    )


def _compile_route(
    route: MixRoute,
    zones: dict[str, ZoneConfig],
    dispatch_horizon_ms: float,
    planning_horizon_ms: float,
) -> CompiledRoute:
    requested = zones.get(route.zone_id)
    if requested is None:
        raise ValueError(f"unknown mix zone: {route.zone_id}")
    active = requested if _zone_ready(requested, dispatch_horizon_ms, planning_horizon_ms) else None
    readiness = "ready"
    if active is None:
        readiness = requested.health.value
        fallback_id = route.fallback_zone_id or requested.fallback_zone_id
        if fallback_id is not None:
            fallback = zones.get(fallback_id)
            if fallback is None:
                raise ValueError(f"unknown fallback mix zone: {fallback_id}")
            if _zone_ready(fallback, dispatch_horizon_ms, planning_horizon_ms):
                active = fallback
                readiness = f"fallback:{fallback.zone_id}"
    return CompiledRoute(
        requested_zone_id=route.zone_id,
        active_zone_id=active.zone_id if active else None,
        stem_ids=route.stem_ids,
        level=route.level,
        envelope=route.envelope,
        readiness=readiness,
    )


def _zone_ready(zone: ZoneConfig, dispatch_ms: float, planning_ms: float) -> bool:
    return (
        zone.health == ZoneHealth.READY
        and zone.configured_output_advance_ms <= dispatch_ms
        and zone.configured_output_advance_ms <= planning_ms
        and (
            zone.residual_error_p95_ms is None
            or zone.residual_error_p95_ms <= zone.timing_tolerance_ms
        )
    )


def _route_level(route, score_tick: int) -> float:
    points = route.envelope
    if not points:
        return route.level
    if score_tick <= points[0].score_tick:
        return points[0].level
    if score_tick >= points[-1].score_tick:
        return points[-1].level
    for left, right in zip(points, points[1:], strict=False):
        if left.score_tick <= score_tick <= right.score_tick:
            if left.curve == Curve.HOLD:
                return left.level
            ratio = (score_tick - left.score_tick) / (right.score_tick - left.score_tick)
            if left.curve == Curve.EQUAL_POWER:
                # A smooth, monotonic perceptual starting point. Endpoints stay exact.
                ratio = math.sin(ratio * math.pi / 2) ** 2
            return left.level + (right.level - left.level) * ratio
    return route.level


__all__ = ["CompiledRegion", "CompiledRoute", "MixPolicy", "compile_mix_policy"]
