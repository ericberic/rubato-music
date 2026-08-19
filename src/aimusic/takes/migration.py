"""Non-destructive migration from take artifacts v1 to lifecycle v2.

The current application still reads ``take.json`` and ``aligned.json``.  Until
all consumers switch atomically, applying this migration writes sidecars
(``take.v2.json`` and ``aligned.v2.json``) and leaves the v1 runtime files
untouched.  Re-running an applied migration is idempotent.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field

from aimusic.accompaniment.bundle_v2 import BundleManifest
from aimusic.core import paths
from aimusic.takes.lifecycle import (
    AlignedResultV2,
    AlignmentCandidateV2,
    AnalysisState,
    BundleRef,
    CellSampleV2,
    ProfileMembership,
    TakeCueV2,
    TakeDocV2,
    TakePlacementHintV2,
    TimingMapPointV2,
    UserDisposition,
    stable_candidate_id,
)
from aimusic.takes.models import AlignedResult, TakeCue, TakePlacementHint, TakeRecord


class V1Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TakeDocV1(V1Model):
    """Explicit adapter model for the pre-v2 ``take.json`` contract."""

    take_id: str
    piece_id: str
    movement: int
    recorded_at: datetime
    duration_seconds: float = Field(ge=0.0)
    note_on_count: int = Field(ge=0)
    input_name: str | None = None
    cue: dict[str, object] | None = None
    placement_hint: dict[str, object] | None = None
    status: Literal["captured", "aligning", "aligned", "ambiguous", "unalignable", "discarded"]
    midi_path: str


@dataclass(frozen=True)
class MigrationResult:
    take_directory: Path
    action: Literal["would_write", "written", "unchanged", "skipped"]
    take: TakeDocV2 | None
    aligned: AlignedResultV2 | None
    note: str = ""


@dataclass(frozen=True)
class MigrationInventory:
    take_directories: int = 0
    v1_only: int = 0
    v2_only: int = 0
    dual: int = 0
    missing_take_metadata: int = 0
    aligned_v1_only: int = 0
    aligned_v2_only: int = 0
    aligned_dual: int = 0

    @property
    def v1_fallback_required(self) -> int:
        return self.v1_only

    @property
    def ready_to_retire_v1_reads(self) -> bool:
        return self.v1_only == 0 and self.missing_take_metadata == 0


def migrate_take_directory(
    take_directory: Path,
    *,
    apply: bool = False,
    bundle: BundleRef | None = None,
    canonical_ppq: int = 960,
) -> MigrationResult:
    """Plan or apply one v1-to-v2 migration using sidecar artifacts."""

    v1_path = take_directory / "take.json"
    if not v1_path.exists():
        return MigrationResult(take_directory, "skipped", None, None, "missing take.json")
    v1 = TakeDocV1.model_validate_json(v1_path.read_text(encoding="utf-8"))
    selected_bundle = bundle or registered_bundle_ref(v1.piece_id, v1.movement)
    aligned_v1_path = take_directory / "aligned.json"
    aligned_v1 = (
        AlignedResult.model_validate_json(aligned_v1_path.read_text(encoding="utf-8"))
        if aligned_v1_path.exists()
        else None
    )
    take_v2 = migrate_take(
        v1,
        bundle=selected_bundle,
        has_alignment=aligned_v1 is not None,
        alignment_was_ambiguous=aligned_v1.ambiguous if aligned_v1 is not None else False,
        canonical_ppq=canonical_ppq,
    )
    aligned_v2 = (
        migrate_alignment(aligned_v1, bundle=selected_bundle, canonical_ppq=canonical_ppq)
        if aligned_v1 is not None
        else None
    )

    desired: dict[Path, str] = {
        take_directory / "take.v2.json": take_v2.model_dump_json(indent=2) + "\n"
    }
    if aligned_v2 is not None:
        desired[take_directory / "aligned.v2.json"] = aligned_v2.model_dump_json(indent=2) + "\n"
    changed = [path for path, content in desired.items() if not _same_json(path, content)]
    if not changed:
        return MigrationResult(take_directory, "unchanged", take_v2, aligned_v2)
    if not apply:
        return MigrationResult(take_directory, "would_write", take_v2, aligned_v2)
    for path, content in desired.items():
        if path in changed:
            _atomic_write(path, content)
    return MigrationResult(take_directory, "written", take_v2, aligned_v2)


def migrate_tree(
    root: Path,
    *,
    apply: bool = False,
    canonical_ppq: int = 960,
) -> list[MigrationResult]:
    return [
        migrate_take_directory(path.parent, apply=apply, canonical_ppq=canonical_ppq)
        for path in sorted(root.glob("*/*/*/take.json"))
    ]


def inventory_tree(root: Path) -> MigrationInventory:
    """Inventory compatibility usage without reading or writing artifacts."""

    directories = sorted(
        {
            path.parent
            for pattern in ("take.json", "take.v2.json", "aligned.json", "aligned.v2.json")
            for path in root.glob(f"*/*/*/{pattern}")
        }
    )
    counts = {
        "v1_only": 0,
        "v2_only": 0,
        "dual": 0,
        "missing_take_metadata": 0,
        "aligned_v1_only": 0,
        "aligned_v2_only": 0,
        "aligned_dual": 0,
    }
    for directory in directories:
        v1 = (directory / "take.json").exists()
        v2 = (directory / "take.v2.json").exists()
        key = (
            "dual"
            if v1 and v2
            else "v1_only"
            if v1
            else "v2_only"
            if v2
            else "missing_take_metadata"
        )
        counts[key] += 1
        aligned_v1 = (directory / "aligned.json").exists()
        aligned_v2 = (directory / "aligned.v2.json").exists()
        if aligned_v1 and aligned_v2:
            counts["aligned_dual"] += 1
        elif aligned_v1:
            counts["aligned_v1_only"] += 1
        elif aligned_v2:
            counts["aligned_v2_only"] += 1
    return MigrationInventory(take_directories=len(directories), **counts)


def migrate_take(
    take: TakeDocV1 | TakeRecord,
    *,
    bundle: BundleRef,
    has_alignment: bool,
    alignment_was_ambiguous: bool = False,
    canonical_ppq: int = 960,
) -> TakeDocV2:
    status = take.status
    if status == "discarded":
        # V1 destroyed the prior analysis state.  The alignment sidecar is the
        # best durable evidence available; preserve this inference explicitly.
        analysis = (
            AnalysisState.AMBIGUOUS
            if alignment_was_ambiguous
            else AnalysisState.ALIGNED
            if has_alignment
            else AnalysisState.CAPTURED
        )
        disposition = UserDisposition.DISCARDED
        membership = ProfileMembership.EXCLUDED
    else:
        analysis = {
            "captured": AnalysisState.CAPTURED,
            "aligning": AnalysisState.RUNNING,
            "aligned": AnalysisState.ALIGNED,
            "ambiguous": AnalysisState.AMBIGUOUS,
            "unalignable": AnalysisState.UNALIGNABLE,
        }[status]
        disposition = UserDisposition.KEPT
        membership = (
            ProfileMembership.INCLUDED
            if analysis == AnalysisState.ALIGNED
            else ProfileMembership.PENDING
        )
    alignment_artifact = (
        "aligned.v2.json" if analysis in {AnalysisState.ALIGNED, AnalysisState.AMBIGUOUS} else None
    )
    cue = _migrate_cue(take.cue, canonical_ppq=canonical_ppq)
    placement_hint = _migrate_placement_hint(take.placement_hint, canonical_ppq=canonical_ppq)
    return TakeDocV2(
        take_id=take.take_id,
        bundle=bundle,
        recorded_at=take.recorded_at,
        updated_at=take.recorded_at,
        duration_seconds=take.duration_seconds,
        note_on_count=take.note_on_count,
        input_name=take.input_name,
        cue=cue,
        placement_hint=placement_hint,
        midi_path=take.midi_path,
        alignment_artifact=alignment_artifact,
        analysis_state=analysis,
        disposition=disposition,
        profile_membership=membership,
    )


def migrate_alignment(
    aligned: AlignedResult,
    *,
    bundle: BundleRef,
    canonical_ppq: int = 960,
) -> AlignedResultV2:
    # Early v1 aligners could retain a negative n-gram diagonal as a weak
    # alternate.  It is not a valid score start and v2 correctly rejects it;
    # drop it here so one diagnostic alternate cannot invalidate an otherwise
    # successful alignment artifact during sidecar migration.
    candidates = tuple(
        AlignmentCandidateV2(
            candidate_id=stable_candidate_id(
                aligned.take_id,
                start_score_tick=round(candidate.start_beat * canonical_ppq),
                start_position=candidate.start_position,
            ),
            start_score_tick=round(candidate.start_beat * canonical_ppq),
            score=candidate.score,
            start_position=candidate.start_position,
        )
        for candidate in aligned.candidates
        if candidate.start_position is None or candidate.start_position >= 0
    )
    base_seconds_per_quarter = (
        statistics.median(sample.period_s for sample in aligned.cell_samples)
        if aligned.cell_samples
        else None
    )
    return AlignedResultV2(
        take_id=aligned.take_id,
        bundle=bundle,
        aligner=aligned.aligner,
        start_score_tick=round(aligned.score_start_beat * canonical_ppq),
        end_score_tick=round(aligned.score_end_beat * canonical_ppq),
        match_rate=aligned.match_rate,
        ambiguous=aligned.ambiguous,
        matched_notes=aligned.matched_notes,
        extra_notes=aligned.extra_notes,
        missing_notes=aligned.missing_notes,
        timing_map=tuple(
            TimingMapPointV2(
                score_tick=round(point.score_beat * canonical_ppq),
                take_seconds=point.take_seconds,
            )
            for point in aligned.timing_map
        ),
        candidates=candidates,
        cell_samples=tuple(
            CellSampleV2(
                score_tick=round(sample.beat * canonical_ppq),
                seconds_per_quarter=sample.period_s,
                rubato_ratio=(
                    sample.period_s / base_seconds_per_quarter
                    if base_seconds_per_quarter is not None
                    else 1.0
                ),
                velocity=sample.velocity,
                pedal=sample.pedal,
                quality=sample.quality,
            )
            for sample in aligned.cell_samples
        ),
        edge_trim_ticks=(
            round(aligned.edge_trim_beats[0] * canonical_ppq),
            round(aligned.edge_trim_beats[1] * canonical_ppq),
        ),
        base_seconds_per_quarter=base_seconds_per_quarter,
    )


def legacy_bundle_ref(piece_id: str, movement: int) -> BundleRef:
    return BundleRef(
        bundle_id=f"{piece_id}_movement_{movement}",
        revision="legacy-unversioned",
        timeline_id="legacy-score-beat-v1",
    )


def registered_bundle_ref(piece_id: str, movement: int) -> BundleRef:
    """Resolve an explicit Bundle v2 identity, falling back only for v1 assets."""

    try:
        manifest_path = paths.score_bundle_dir(piece_id, movement) / "bundle.yaml"
    except ValueError:
        return legacy_bundle_ref(piece_id, movement)
    if not manifest_path.exists():
        return legacy_bundle_ref(piece_id, movement)
    manifest = BundleManifest.model_validate(
        yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    )
    return manifest.ref


def _migrate_cue(
    cue: dict[str, object] | TakeCue | None, *, canonical_ppq: int
) -> TakeCueV2 | None:
    if cue is None:
        return None
    data = cue.model_dump() if isinstance(cue, TakeCue) else cue
    return TakeCueV2(
        kind=str(data.get("kind", "from_top")),
        target_score_tick=round(float(data["target_beat"]) * canonical_ppq),
        cue_start_score_tick=(
            None
            if data.get("cue_start_beat") is None
            else round(float(data["cue_start_beat"]) * canonical_ppq)
        ),
        cue_seconds=float(data["cue_seconds"]),
        output_name=str(data["output_name"]),
    )


def _migrate_placement_hint(
    hint: dict[str, object] | TakePlacementHint | None, *, canonical_ppq: int
) -> TakePlacementHintV2 | None:
    if hint is None:
        return None
    data = hint.model_dump() if isinstance(hint, TakePlacementHint) else hint
    return TakePlacementHintV2(
        target_score_tick=round(float(data["target_beat"]) * canonical_ppq),
        source="selected_passage",
    )


def _same_json(path: Path, content: str) -> bool:
    if not path.exists():
        return False
    try:
        return json.loads(path.read_text(encoding="utf-8")) == json.loads(content)
    except json.JSONDecodeError:
        return False


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "MigrationResult",
    "MigrationInventory",
    "TakeDocV1",
    "legacy_bundle_ref",
    "registered_bundle_ref",
    "migrate_alignment",
    "migrate_take",
    "migrate_take_directory",
    "migrate_tree",
    "inventory_tree",
]
