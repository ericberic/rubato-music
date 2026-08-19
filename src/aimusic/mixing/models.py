"""Validated contracts for spatial mix authoring.

Mix programs are performer-authored artifacts.  They deliberately contain no
tracking confidence, rehearsal coverage, transport latency, or hardware volume
state: those concerns have different owners and lifecycles.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MixArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Curve(StrEnum):
    LINEAR = "linear"
    EQUAL_POWER = "equal_power"
    HOLD = "hold"


class Gesture(StrEnum):
    SWELL = "swell"
    FADE = "fade"
    BED = "bed"
    FEATURE = "feature"
    CUSTOM = "custom"


class ZoneHealth(StrEnum):
    READY = "ready"
    UNAVAILABLE = "unavailable"
    NEEDS_CALIBRATION = "needs_calibration"


class ScoreIdentityStatus(StrEnum):
    CURRENT = "current"
    STALE = "stale"


class EnvelopePoint(MixArtifact):
    score_tick: int = Field(ge=0)
    level: float = Field(ge=0, le=100)
    curve: Curve = Curve.EQUAL_POWER


class MixRoute(MixArtifact):
    zone_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    stem_ids: tuple[str, ...] = Field(min_length=1)
    level: float = Field(default=100, ge=0, le=100)
    envelope: tuple[EnvelopePoint, ...] = ()
    fallback_zone_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_-]+$"
    )

    @field_validator("stem_ids")
    @classmethod
    def validate_stems(cls, stems: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(stem.strip() for stem in stems)
        if any(not stem for stem in cleaned):
            raise ValueError("stem ids cannot be blank")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("stem ids must be unique within a route")
        return cleaned

    @model_validator(mode="after")
    def validate_route(self) -> "MixRoute":
        ticks = [point.score_tick for point in self.envelope]
        if ticks != sorted(ticks) or len(ticks) != len(set(ticks)):
            raise ValueError("envelope points must have unique ascending score ticks")
        if self.fallback_zone_id == self.zone_id:
            raise ValueError("a route cannot fall back to itself")
        return self


class MixRegion(MixArtifact):
    region_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    start_tick: int = Field(ge=0)
    end_tick: int = Field(gt=0)
    gesture: Gesture = Gesture.CUSTOM
    routes: tuple[MixRoute, ...] = Field(min_length=1)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_region(self) -> "MixRegion":
        if self.end_tick <= self.start_tick:
            raise ValueError("region end_tick must be greater than start_tick")
        identities: set[tuple[str, tuple[str, ...]]] = set()
        for route in self.routes:
            identity = (route.zone_id, route.stem_ids)
            if identity in identities:
                raise ValueError("a region cannot duplicate the same zone/stem route")
            identities.add(identity)
            if any(
                point.score_tick < self.start_tick or point.score_tick > self.end_tick
                for point in route.envelope
            ):
                raise ValueError("envelope points must lie inside their region")
        return self


class MixProgram(MixArtifact):
    schema_version: int = Field(default=1, ge=1)
    program_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=120)
    piece_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    movement: int = Field(ge=1)
    score_bundle_id: str
    score_bundle_revision: str
    timeline_digest: str
    level_mapping_revision: str = "volume-linear-v1"
    revision: int = Field(default=1, ge=1)
    undo_parent_revision: int | None = Field(default=None, ge=1)
    score_identity_status: ScoreIdentityStatus = ScoreIdentityStatus.CURRENT
    updated_at: datetime
    default_routes: tuple[MixRoute, ...] = Field(min_length=1)
    regions: tuple[MixRegion, ...] = ()

    @model_validator(mode="after")
    def validate_program(self) -> "MixProgram":
        if (
            self.undo_parent_revision is not None
            and self.undo_parent_revision >= self.revision
        ):
            raise ValueError("undo parent revision must precede the current revision")
        region_ids = [region.region_id for region in self.regions]
        if len(region_ids) != len(set(region_ids)):
            raise ValueError("region ids must be unique")
        default_identities = [
            (route.zone_id, route.stem_ids) for route in self.default_routes
        ]
        if len(default_identities) != len(set(default_identities)):
            raise ValueError("default routes must have unique zone/stem identities")
        enabled = [region for region in self.regions if region.enabled]
        for index, left in enumerate(enabled):
            left_routes = {
                (route.zone_id, stem_id)
                for route in left.routes
                for stem_id in route.stem_ids
            }
            for right in enabled[index + 1 :]:
                # Regions are half-open [start_tick, end_tick), so adjacent
                # gestures can meet at one canonical boundary without overlap.
                if left.start_tick >= right.end_tick or right.start_tick >= left.end_tick:
                    continue
                right_routes = {
                    (route.zone_id, stem_id)
                    for route in right.routes
                    for stem_id in route.stem_ids
                }
                collision = sorted(left_routes & right_routes)
                if collision:
                    zone_id, stem_id = collision[0]
                    raise ValueError(
                        "enabled mix regions cannot overlap for the same zone/stem: "
                        f"{zone_id}/{stem_id}"
                    )
        return self


class ZoneConfig(MixArtifact):
    zone_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    label: str
    renderer_id: str
    device_id: str | None = None
    channel_map: tuple[int, ...] = ()
    acoustic_position: str
    configured_output_advance_ms: float = Field(ge=0)
    residual_error_p95_ms: float | None = Field(default=None, ge=0)
    timing_tolerance_ms: float = Field(default=40, gt=0)
    calibration_revision: str | None = None
    health: ZoneHealth
    fallback_zone_id: str | None = None


class MixProgramCreate(MixArtifact):
    program_id: str = Field(default="main", min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(default="Main spatial mix", min_length=1, max_length=120)
    piece_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    movement: int = Field(ge=1)


class MixRoutesUpdate(MixArtifact):
    expected_revision: int = Field(ge=1)
    default_routes: tuple[MixRoute, ...] = Field(min_length=1)


class MixRegionCreate(MixArtifact):
    expected_revision: int = Field(ge=1)
    region: MixRegion


class MixRegionUpdate(MixArtifact):
    expected_revision: int = Field(ge=1)
    region: MixRegion


class MixRevisionRequest(MixArtifact):
    expected_revision: int = Field(ge=1)


class MixProgramList(MixArtifact):
    programs: tuple[MixProgram, ...]


class ZoneConfigList(MixArtifact):
    zones: tuple[ZoneConfig, ...]


__all__ = [
    "Curve",
    "EnvelopePoint",
    "Gesture",
    "MixProgram",
    "MixProgramCreate",
    "MixProgramList",
    "MixRegion",
    "MixRegionCreate",
    "MixRegionUpdate",
    "MixRevisionRequest",
    "MixRoute",
    "MixRoutesUpdate",
    "ScoreIdentityStatus",
    "ZoneConfig",
    "ZoneConfigList",
    "ZoneHealth",
]
