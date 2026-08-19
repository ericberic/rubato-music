"""One-time migration to canonical performance-profile v2.

This module is intentionally a migration boundary, not a runtime compatibility
layer.  It can read the old ``period_s`` cell payload once, rebuild every cell
from the canonical timing map and raw take MIDI, preserve recoverable backups,
and write only the new strict artifacts consumed by the application.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from aimusic.accompaniment.bundle_v2 import BundleRef
from aimusic.accompaniment.rehearsal_position import score_projection
from aimusic.core import paths
from aimusic.takes import coverage, profile
from aimusic.takes.lifecycle import (
    AlignedResultV2,
    AlignmentCandidateV2,
    TimingMapPointV2,
    stable_candidate_id,
)


class LegacyModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class LegacyTimingPoint(LegacyModel):
    score_tick: int = Field(ge=0)
    reference_tick: int | None = Field(default=None, ge=0)
    take_seconds: float = Field(ge=0)


class LegacyCandidate(LegacyModel):
    candidate_id: str
    start_score_tick: int = Field(ge=0)
    score: float
    start_position: int | None = Field(default=None, ge=0)
    reference_start_tick: int | None = Field(default=None, ge=0)


class LegacyAlignedV2(LegacyModel):
    schema_version: Literal[2] = 2
    take_id: str
    bundle: BundleRef
    aligner: str
    coordinate_system: Literal["reference_midi_compat", "canonical_score"] = (
        "reference_midi_compat"
    )
    start_score_tick: int = Field(ge=0)
    end_score_tick: int = Field(ge=0)
    start_reference_tick: int | None = Field(default=None, ge=0)
    end_reference_tick: int | None = Field(default=None, ge=0)
    mapping_id: str | None = None
    match_rate: float = Field(ge=0, le=1)
    ambiguous: bool
    matched_notes: int = Field(ge=0)
    extra_notes: int = Field(ge=0)
    missing_notes: int = Field(ge=0)
    timing_map: tuple[LegacyTimingPoint, ...] = ()
    candidates: tuple[LegacyCandidate, ...] = ()
    edge_trim_ticks: tuple[int, int] = (0, 0)


@dataclass(frozen=True)
class PerformanceMigrationResult:
    aligned_written: int
    aligned_unchanged: int
    profile_written: bool
    backups: tuple[Path, ...]


def migrate_performance_profile(
    piece_id: str,
    movement: int,
    *,
    data_root: Path | None = None,
    apply: bool = False,
) -> PerformanceMigrationResult:
    """Rebuild a movement's aligned cells and profile on canonical score time."""

    root = data_root or paths.data_root()
    movement_root = root / "takes" / piece_id / str(movement)
    projection = score_projection(piece_id, movement)
    written = 0
    unchanged = 0
    backups: list[Path] = []
    for aligned_path in sorted(movement_root.glob("*/aligned.v2.json")):
        raw = json.loads(aligned_path.read_text(encoding="utf-8"))
        if raw.get("performance_model") == "canonical-performance-v2":
            AlignedResultV2.model_validate(raw)
            unchanged += 1
            continue
        legacy = LegacyAlignedV2.model_validate(raw)
        take_path = aligned_path.with_name("take.mid")
        migrated = migrate_alignment_performance_model(
            legacy,
            take_path=take_path,
            canonicalize_tick=lambda tick: projection.position_at_source_tick(tick).score_tick,
            mapping_id=projection.mapping_id,
        )
        written += 1
        if apply:
            backup = aligned_path.with_name("aligned.pre-performance-v2.json")
            if not backup.exists():
                shutil.copy2(aligned_path, backup)
                backups.append(backup)
            _atomic_write(aligned_path, migrated.model_dump_json(indent=2) + "\n")

    profile_written = False
    if apply:
        profile_path = root / "profiles" / piece_id / str(movement) / "profile.json"
        existing_profile = None
        existing_revision = 0
        if profile_path.exists():
            raw_profile = json.loads(profile_path.read_text(encoding="utf-8"))
            existing_revision = int(raw_profile.get("revision", 0))
            if raw_profile.get("schema_version") == 2:
                existing_profile = profile.Interpretation.model_validate(raw_profile)
            backup = profile_path.with_name("profile.pre-performance-v2.json")
            if not backup.exists():
                shutil.copy2(profile_path, backup)
                backups.append(backup)
        # ``fit_interpretation_for`` uses the configured data root.  A custom root is used
        # only by deterministic tests; production migration uses paths.data_root().
        if data_root is not None and data_root.resolve() != paths.data_root().resolve():
            raise ValueError("apply with a custom data_root requires AIMUSIC_DATA_ROOT")
        if written or existing_profile is None or existing_profile.revision < 1:
            migrated_profile = profile.fit_interpretation_for(piece_id, movement).model_copy(
                update={"revision": max(1, existing_revision + 1)}
            )
            profile_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(profile_path, migrated_profile.model_dump_json(indent=2) + "\n")
            coverage.materialize_coverage(piece_id, movement)
            profile_written = True

    return PerformanceMigrationResult(
        aligned_written=written,
        aligned_unchanged=unchanged,
        profile_written=profile_written,
        backups=tuple(backups),
    )


def migrate_alignment_performance_model(
    legacy: LegacyAlignedV2,
    *,
    take_path: Path,
    canonicalize_tick: Callable[[int], int],
    mapping_id: str,
) -> AlignedResultV2:
    """Convert one old alignment and recompute its observations from MIDI."""

    is_canonical = legacy.coordinate_system == "canonical_score"

    def score_tick(tick: int) -> int:
        return tick if is_canonical else canonicalize_tick(tick)

    def reference_tick(score: int, reference: int | None) -> int:
        return reference if reference is not None else score

    timing_map = tuple(
        TimingMapPointV2(
            score_tick=score_tick(point.score_tick),
            reference_tick=reference_tick(point.score_tick, point.reference_tick),
            take_seconds=point.take_seconds,
        )
        for point in legacy.timing_map
    )
    candidates = tuple(
        AlignmentCandidateV2(
            candidate_id=stable_candidate_id(
                legacy.take_id,
                start_score_tick=score_tick(candidate.start_score_tick),
                start_position=candidate.start_position,
            ),
            start_score_tick=score_tick(candidate.start_score_tick),
            reference_start_tick=reference_tick(
                candidate.start_score_tick, candidate.reference_start_tick
            ),
            score=candidate.score,
            start_position=candidate.start_position,
        )
        for candidate in legacy.candidates
    )
    aligned = AlignedResultV2(
        take_id=legacy.take_id,
        bundle=legacy.bundle,
        aligner=legacy.aligner,
        coordinate_system="canonical_score",
        start_score_tick=score_tick(legacy.start_score_tick),
        end_score_tick=score_tick(legacy.end_score_tick),
        start_reference_tick=reference_tick(
            legacy.start_score_tick, legacy.start_reference_tick
        ),
        end_reference_tick=reference_tick(legacy.end_score_tick, legacy.end_reference_tick),
        mapping_id=legacy.mapping_id if is_canonical and legacy.mapping_id else mapping_id,
        match_rate=legacy.match_rate,
        ambiguous=legacy.ambiguous,
        matched_notes=legacy.matched_notes,
        extra_notes=legacy.extra_notes,
        missing_notes=legacy.missing_notes,
        timing_map=timing_map,
        candidates=candidates,
        cell_samples=(),
        edge_trim_ticks=legacy.edge_trim_ticks,
    )
    samples, base = profile.compute_canonical_cell_samples(
        take_path,
        start_score_tick=aligned.start_score_tick,
        end_score_tick=aligned.end_score_tick,
        match_rate=aligned.match_rate,
        timing_map=aligned.timing_map,
    )
    return aligned.model_copy(
        update={"cell_samples": samples, "base_seconds_per_quarter": base}
    )


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=path.parent, delete=False, encoding="utf-8"
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


__all__ = [
    "LegacyAlignedV2",
    "PerformanceMigrationResult",
    "migrate_alignment_performance_model",
    "migrate_performance_profile",
]
