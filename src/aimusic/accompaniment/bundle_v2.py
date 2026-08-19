"""Score Bundle v2 sourcing contracts and canonical score timeline.

Bundle v2 describes how independent score sources map to one exact symbolic
timeline.  It intentionally does not replace the legacy runtime ``ScoreBundle``
event view yet; :mod:`aimusic.accompaniment.score_bundle` remains the adapter
used by the current follower and scheduler while callers migrate.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

import yaml
from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


def _relative_artifact_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
        raise ValueError("artifact paths must be non-empty, bundle-relative paths")
    return value


RelativeArtifactPath = Annotated[str, AfterValidator(_relative_artifact_path)]


class ContractModel(BaseModel):
    """Strict, immutable base for current-version bundle artifacts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceRole(StrEnum):
    """A source's declared contribution; filenames never imply a role."""

    SEMANTIC_SCORE = "semantic_score"
    DISPLAY_SCORE = "display_score"
    PLAYBACK_PERFORMANCE = "playback_performance"
    LAYOUT_EVIDENCE = "layout_evidence"
    ANNOTATIONS = "annotations"


class DerivedArtifactRole(StrEnum):
    """Rebuildable artifacts consumed by rehearsal and performance."""

    FOLLOWER_REFERENCE = "follower_reference"
    ACCOMPANIMENT = "accompaniment"
    DISPLAY_MAP = "display_map"
    DISPLAY_LAYOUT = "display_layout"
    PERFORMANCE_MAP = "performance_map"
    SECTIONS = "sections"
    INSTRUMENT_MAP = "instrument_map"
    NGRAM_INDEX = "ngram_index"
    PERFORMANCE_BEAT_MAP = "performance_beat_map"


class MappingKind(StrEnum):
    PERFORMANCE = "performance"
    DISPLAY = "display"
    SEMANTIC = "semantic"


class MappingReviewState(StrEnum):
    MACHINE = "machine"
    REVIEWED = "reviewed"
    REJECTED = "rejected"


class BundleRef(ContractModel):
    """Stable identity for one immutable bundle revision and its timeline."""

    bundle_id: str = Field(min_length=1)
    revision: str = Field(
        min_length=1,
        validation_alias=AliasChoices("revision", "bundle_revision"),
    )
    timeline_id: str = Field(
        min_length=1,
        validation_alias=AliasChoices("timeline_id", "timeline_revision"),
    )

    @property
    def bundle_revision(self) -> str:
        """Lifecycle-facing compatibility name for the immutable revision."""

        return self.revision

    @property
    def timeline_revision(self) -> str:
        """Lifecycle-facing compatibility name for the timeline identity."""

        return self.timeline_id


class WorkIdentity(ContractModel):
    work_id: str = Field(min_length=1)
    movement_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    composer: str = Field(min_length=1)


class TimelineRef(ContractModel):
    timeline_id: str = Field(min_length=1)
    canonical_ppq: int = Field(gt=0)
    artifact: RelativeArtifactPath
    review_state: MappingReviewState = MappingReviewState.REVIEWED


class SourceArtifact(ContractModel):
    source_id: str = Field(min_length=1)
    role: SourceRole
    path: RelativeArtifactPath
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    derived_from: tuple[str, ...] = ()
    provenance: str | None = None
    page_scope: tuple[int, int] | None = None

    @field_validator("page_scope")
    @classmethod
    def _valid_page_scope(cls, value: tuple[int, int] | None) -> tuple[int, int] | None:
        if value is not None and (value[0] < 1 or value[1] < value[0]):
            raise ValueError("page_scope must be a one-based inclusive range")
        return value


class DerivedArtifact(ContractModel):
    artifact_id: str = Field(min_length=1)
    role: DerivedArtifactRole
    path: RelativeArtifactPath
    derived_from: tuple[str, ...] = ()
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class SourceMappingRef(ContractModel):
    mapping_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    kind: MappingKind
    artifact: RelativeArtifactPath
    review_state: MappingReviewState = MappingReviewState.MACHINE


class BundleManifest(ContractModel):
    """The root ``bundle.yaml`` contract for Score Bundle v2."""

    schema_version: Literal[2]
    bundle_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    work: WorkIdentity
    timeline: TimelineRef
    sources: tuple[SourceArtifact, ...]
    derived: tuple[DerivedArtifact, ...] = ()
    mappings: tuple[SourceMappingRef, ...] = ()

    @model_validator(mode="after")
    def _unique_and_connected_ids(self) -> "BundleManifest":
        source_ids = [source.source_id for source in self.sources]
        artifact_ids = [artifact.artifact_id for artifact in self.derived]
        mapping_ids = [mapping.mapping_id for mapping in self.mappings]
        for label, values in (
            ("source", source_ids),
            ("derived artifact", artifact_ids),
            ("mapping", mapping_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label} id")
        known_inputs = set(source_ids) | set(artifact_ids)
        for source in self.sources:
            unknown = set(source.derived_from) - known_inputs
            if unknown:
                raise ValueError(f"source {source.source_id} has unknown inputs: {sorted(unknown)}")
        for artifact in self.derived:
            unknown = set(artifact.derived_from) - known_inputs
            if unknown:
                raise ValueError(
                    f"derived artifact {artifact.artifact_id} has unknown inputs: {sorted(unknown)}"
                )
        for mapping in self.mappings:
            if mapping.source_id not in set(source_ids):
                raise ValueError(
                    f"mapping {mapping.mapping_id} references unknown source {mapping.source_id}"
                )
        return self

    @property
    def ref(self) -> BundleRef:
        return BundleRef(
            bundle_id=self.bundle_id,
            revision=self.revision,
            timeline_id=self.timeline.timeline_id,
        )


class TimeSignature(ContractModel):
    beats: int = Field(gt=0)
    beat_type: int = Field(gt=0)

    @field_validator("beat_type")
    @classmethod
    def _power_of_two(cls, value: int) -> int:
        if value & (value - 1):
            raise ValueError("beat_type must be a power of two")
        return value


class MeasureSpan(ContractModel):
    measure_index: int = Field(ge=0)
    measure_label: str = Field(min_length=1)
    start_tick: int = Field(ge=0)
    end_tick: int = Field(gt=0)
    time_signature: TimeSignature
    implicit: bool = False

    @model_validator(mode="after")
    def _positive_span(self) -> "MeasureSpan":
        if self.end_tick <= self.start_tick:
            raise ValueError("measure end_tick must be greater than start_tick")
        return self


class ScorePosition(ContractModel):
    """Exact machine coordinate plus its human-readable measure projection."""

    movement_id: str = Field(min_length=1)
    score_tick: int = Field(ge=0)
    measure_index: int = Field(ge=0)
    measure_label: str = Field(min_length=1)
    offset_ticks: int = Field(ge=0)
    beat_in_measure: float = Field(ge=0)


class ScoreSpan(ContractModel):
    movement_id: str = Field(min_length=1)
    start_tick: int = Field(ge=0)
    end_tick: int = Field(gt=0)

    @model_validator(mode="after")
    def _positive_span(self) -> "ScoreSpan":
        if self.end_tick <= self.start_tick:
            raise ValueError("score span must be non-empty and half-open")
        return self


class TimelineDocument(ContractModel):
    schema_version: Literal[2]
    timeline_id: str = Field(min_length=1)
    movement_id: str = Field(min_length=1)
    canonical_ppq: int = Field(gt=0)
    measures: tuple[MeasureSpan, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _continuous_measures(self) -> "TimelineDocument":
        for expected_index, measure in enumerate(self.measures):
            if measure.measure_index != expected_index:
                raise ValueError("measure_index values must be contiguous and zero-based")
            if expected_index and measure.start_tick != self.measures[expected_index - 1].end_tick:
                raise ValueError("canonical measure spans must be continuous")
        return self


class MappingSegment(ContractModel):
    """One monotonic affine correspondence segment.

    Source coordinates are deliberately unit-labelled; a MIDI source tick is a
    performance coordinate and is not assumed to equal a canonical score tick.
    """

    source_start: float
    source_end: float
    score_start_tick: int = Field(ge=0)
    score_end_tick: int = Field(gt=0)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _positive_segment(self) -> "MappingSegment":
        if self.source_end <= self.source_start:
            raise ValueError("mapping source span must be positive")
        if self.score_end_tick <= self.score_start_tick:
            raise ValueError("mapping score span must be positive")
        return self


class SourceMappingDocument(ContractModel):
    """Piecewise source-to-score correspondence with explicit unmapped gaps."""

    schema_version: Literal[2]
    mapping_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    timeline_id: str = Field(min_length=1)
    kind: MappingKind
    source_unit: Literal["midi_tick", "seconds", "musicxml_division", "page_coordinate"]
    segments: tuple[MappingSegment, ...] = ()
    unmapped_score_spans: tuple[ScoreSpan, ...] = ()

    @model_validator(mode="after")
    def _monotonic_segments(self) -> "SourceMappingDocument":
        for previous, current in zip(self.segments, self.segments[1:]):
            if current.source_start < previous.source_end:
                raise ValueError("mapping source segments must not overlap")
            if current.score_start_tick < previous.score_end_tick:
                raise ValueError("mapping score segments must not overlap or reverse")
        ordered_gaps = sorted(self.unmapped_score_spans, key=lambda span: span.start_tick)
        for previous, current in zip(ordered_gaps, ordered_gaps[1:]):
            if current.start_tick < previous.end_tick:
                raise ValueError("unmapped score spans must not overlap")
        return self


class DisplayMeasureBox(ContractModel):
    """One PDF rectangle explicitly joined to a canonical measure span."""

    page: int = Field(ge=1)
    system: int = Field(ge=1)
    x0: float = Field(ge=0, le=1)
    x1: float = Field(ge=0, le=1)
    y0: float = Field(ge=0, le=1)
    y1: float = Field(ge=0, le=1)
    measure_index: int = Field(ge=0)
    measure_label: str = Field(min_length=1)
    score_start_tick: int = Field(ge=0)
    score_end_tick: int = Field(gt=0)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _valid_box_and_span(self) -> "DisplayMeasureBox":
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValueError("display box must have positive normalized area")
        if self.score_end_tick <= self.score_start_tick:
            raise ValueError("display box score span must be positive")
        return self


class DisplayMappingDocument(ContractModel):
    """Two-dimensional PDF geometry joined to the exact score timeline."""

    schema_version: Literal[2]
    mapping_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    timeline_id: str = Field(min_length=1)
    kind: Literal[MappingKind.DISPLAY] = MappingKind.DISPLAY
    coordinate_system: Literal["normalized_pdf_page"] = "normalized_pdf_page"
    page_count: int = Field(gt=0)
    boxes: tuple[DisplayMeasureBox, ...]

    @model_validator(mode="after")
    def _unique_monotonic_boxes(self) -> "DisplayMappingDocument":
        indexes = [box.measure_index for box in self.boxes]
        if len(indexes) != len(set(indexes)):
            raise ValueError("display mapping contains duplicate measure indexes")
        for previous, current in zip(self.boxes, self.boxes[1:]):
            if current.score_start_tick < previous.score_end_tick:
                raise ValueError("display mapping score spans must not overlap or reverse")
        if any(box.page > self.page_count for box in self.boxes):
            raise ValueError("display box page exceeds page_count")
        return self


MappingDocument = SourceMappingDocument | DisplayMappingDocument


class CanonicalTimeline:
    """Validated conversions between exact ticks and printed measure positions."""

    def __init__(self, document: TimelineDocument) -> None:
        self.document = document
        self._starts = tuple(measure.start_tick for measure in document.measures)

    @property
    def end_tick(self) -> int:
        return self.document.measures[-1].end_tick

    def position_at(self, score_tick: int) -> ScorePosition:
        if score_tick < 0 or score_tick >= self.end_tick:
            raise ValueError(f"score_tick must be in [0, {self.end_tick})")
        index = bisect_right(self._starts, score_tick) - 1
        measure = self.document.measures[index]
        offset = score_tick - measure.start_tick
        return ScorePosition(
            movement_id=self.document.movement_id,
            score_tick=score_tick,
            measure_index=measure.measure_index,
            measure_label=measure.measure_label,
            offset_ticks=offset,
            beat_in_measure=offset / self.document.canonical_ppq,
        )

    def tick_at(self, measure_index: int, offset_ticks: int = 0) -> int:
        try:
            measure = self.document.measures[measure_index]
        except IndexError as exc:
            raise ValueError(f"unknown measure_index {measure_index}") from exc
        if offset_ticks < 0 or offset_ticks >= measure.end_tick - measure.start_tick:
            raise ValueError("offset_ticks must fall inside the half-open measure span")
        return measure.start_tick + offset_ticks


class ReadinessIssue(ContractModel):
    severity: Literal["error", "warning"]
    code: str
    message: str
    artifact_id: str | None = None


class BundleReadiness(ContractModel):
    sourcing: bool
    rehearsal: bool
    performance: bool
    issues: tuple[ReadinessIssue, ...]


class LoadedBundleV2:
    """Loaded v2 manifest, timeline, mappings, and validation report."""

    def __init__(
        self,
        *,
        root: Path,
        manifest: BundleManifest,
        timeline: CanonicalTimeline,
        mappings: tuple[MappingDocument, ...],
        readiness: BundleReadiness,
    ) -> None:
        self.root = root
        self.manifest = manifest
        self.timeline = timeline
        self.mappings = mappings
        self.readiness = readiness


class BundleLoaderV2:
    """Parse and validate a Bundle v2 directory at its filesystem boundary."""

    @classmethod
    def load(cls, root: Path | str, *, require_ready: bool = False) -> LoadedBundleV2:
        bundle_root = Path(root).resolve()
        manifest = BundleManifest.model_validate(_read_yaml(bundle_root / "bundle.yaml"))
        timeline_path = bundle_root / manifest.timeline.artifact
        timeline = TimelineDocument.model_validate_json(timeline_path.read_text(encoding="utf-8"))
        if timeline.timeline_id != manifest.timeline.timeline_id:
            raise ValueError("manifest and timeline artifact timeline_id differ")
        if timeline.canonical_ppq != manifest.timeline.canonical_ppq:
            raise ValueError("manifest and timeline artifact canonical_ppq differ")
        if timeline.movement_id != manifest.work.movement_id:
            raise ValueError("manifest and timeline artifact movement_id differ")

        mappings: list[MappingDocument] = []
        for mapping_ref in manifest.mappings:
            mapping_payload = (bundle_root / mapping_ref.artifact).read_text(encoding="utf-8")
            mapping_object = json.loads(mapping_payload)
            if mapping_ref.kind == MappingKind.DISPLAY and "boxes" in mapping_object:
                document = DisplayMappingDocument.model_validate(mapping_object)
            else:
                document = SourceMappingDocument.model_validate(mapping_object)
            if document.mapping_id != mapping_ref.mapping_id:
                raise ValueError(f"mapping id mismatch for {mapping_ref.mapping_id}")
            if document.source_id != mapping_ref.source_id or document.kind != mapping_ref.kind:
                raise ValueError(f"mapping metadata mismatch for {mapping_ref.mapping_id}")
            if document.timeline_id != timeline.timeline_id:
                raise ValueError(f"mapping {mapping_ref.mapping_id} targets another timeline")
            if isinstance(document, SourceMappingDocument):
                for gap in document.unmapped_score_spans:
                    wrong_movement = gap.movement_id != timeline.movement_id
                    past_timeline = gap.end_tick > timeline.measures[-1].end_tick
                    if wrong_movement or past_timeline:
                        raise ValueError(
                            f"mapping {mapping_ref.mapping_id} has an out-of-range gap"
                        )
                if any(
                    segment.score_end_tick > timeline.measures[-1].end_tick
                    for segment in document.segments
                ):
                    raise ValueError(
                        f"mapping {mapping_ref.mapping_id} has an out-of-range segment"
                    )
            elif any(
                box.score_end_tick > timeline.measures[-1].end_tick
                for box in document.boxes
            ):
                raise ValueError(f"mapping {mapping_ref.mapping_id} has an out-of-range box")
            mappings.append(document)

        readiness = cls.validate_readiness(bundle_root, manifest, tuple(mappings))
        if require_ready and not readiness.performance:
            errors = "; ".join(
                issue.message for issue in readiness.issues if issue.severity == "error"
            )
            raise ValueError(f"bundle is not performance-ready: {errors}")
        return LoadedBundleV2(
            root=bundle_root,
            manifest=manifest,
            timeline=CanonicalTimeline(timeline),
            mappings=tuple(mappings),
            readiness=readiness,
        )

    @classmethod
    def validate_readiness(
        cls,
        root: Path,
        manifest: BundleManifest,
        mappings: tuple[MappingDocument, ...],
    ) -> BundleReadiness:
        issues: list[ReadinessIssue] = []
        for artifact_id, relative_path, digest in (
            *((source.source_id, source.path, source.sha256) for source in manifest.sources),
            *(
                (artifact.artifact_id, artifact.path, artifact.sha256)
                for artifact in manifest.derived
            ),
            *((mapping.mapping_id, mapping.artifact, None) for mapping in manifest.mappings),
        ):
            path = root / relative_path
            if not path.is_file():
                issues.append(
                    ReadinessIssue(
                        severity="error",
                        code="missing_artifact",
                        message=f"declared artifact does not exist: {relative_path}",
                        artifact_id=artifact_id,
                    )
                )
            elif digest and _sha256(path) != digest:
                issues.append(
                    ReadinessIssue(
                        severity="error",
                        code="hash_mismatch",
                        message=f"sha256 does not match: {relative_path}",
                        artifact_id=artifact_id,
                    )
                )

        source_roles = {source.role for source in manifest.sources}
        derived_roles = {artifact.role for artifact in manifest.derived}
        mapping_kinds = {mapping.kind for mapping in mappings}

        def missing(code: str, message: str) -> None:
            issues.append(ReadinessIssue(severity="warning", code=code, message=message))

        timeline_reviewed = manifest.timeline.review_state == MappingReviewState.REVIEWED
        sourcing = SourceRole.SEMANTIC_SCORE in source_roles and timeline_reviewed
        if not sourcing:
            if SourceRole.SEMANTIC_SCORE not in source_roles:
                missing(
                    "no_semantic_score",
                    "a semantic_score source is required for sourcing readiness",
                )
            if not timeline_reviewed:
                missing(
                    "timeline_not_reviewed",
                    "the canonical timeline must be reviewed for sourcing readiness",
                )
        rehearsal = sourcing and {
            SourceRole.DISPLAY_SCORE,
        }.issubset(source_roles) and {
            DerivedArtifactRole.FOLLOWER_REFERENCE,
            DerivedArtifactRole.DISPLAY_MAP,
        }.issubset(derived_roles) and MappingKind.DISPLAY in mapping_kinds
        if sourcing and not rehearsal:
            missing(
                "rehearsal_inputs_incomplete",
                "rehearsal requires a display score/map and follower reference",
            )
        performance = rehearsal and {
            DerivedArtifactRole.ACCOMPANIMENT,
            DerivedArtifactRole.SECTIONS,
            DerivedArtifactRole.INSTRUMENT_MAP,
        }.issubset(derived_roles)
        if rehearsal and not performance:
            missing(
                "performance_inputs_incomplete",
                "performance requires accompaniment, sections, and instrument map artifacts",
            )
        has_errors = any(issue.severity == "error" for issue in issues)
        return BundleReadiness(
            sourcing=sourcing and not has_errors,
            rehearsal=rehearsal and not has_errors,
            performance=performance and not has_errors,
            issues=tuple(issues),
        )


class BundleRegistry:
    """Explicit registry; it never guesses bundle paths from movement numbers."""

    def __init__(self) -> None:
        self._roots: dict[tuple[str, str], Path] = {}

    def register(self, root: Path | str) -> BundleRef:
        bundle_root = Path(root).resolve()
        manifest = BundleManifest.model_validate(_read_yaml(bundle_root / "bundle.yaml"))
        key = (manifest.bundle_id, manifest.revision)
        existing = self._roots.get(key)
        if existing is not None and existing != bundle_root:
            raise ValueError(f"bundle revision {key!r} is already registered at {existing}")
        self._roots[key] = bundle_root
        return manifest.ref

    def resolve(self, bundle_id: str, revision: str | None = None) -> Path:
        matches = [(key, root) for key, root in self._roots.items() if key[0] == bundle_id]
        if revision is not None:
            try:
                return self._roots[(bundle_id, revision)]
            except KeyError as exc:
                raise KeyError(f"unknown bundle revision: {bundle_id}@{revision}") from exc
        if not matches:
            raise KeyError(f"unknown bundle: {bundle_id}")
        if len(matches) > 1:
            revisions = sorted(key[1] for key, _ in matches)
            raise ValueError(f"revision required for {bundle_id}; registered: {revisions}")
        return matches[0][1]

    def load(self, bundle_id: str, revision: str | None = None) -> LoadedBundleV2:
        return BundleLoaderV2.load(self.resolve(bundle_id, revision))


def _read_yaml(path: Path) -> object:
    if not path.is_file():
        raise FileNotFoundError(path)
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "BundleLoaderV2",
    "BundleManifest",
    "BundleReadiness",
    "BundleRef",
    "BundleRegistry",
    "CanonicalTimeline",
    "DerivedArtifact",
    "DerivedArtifactRole",
    "DisplayMappingDocument",
    "DisplayMeasureBox",
    "LoadedBundleV2",
    "MappingKind",
    "MappingReviewState",
    "MappingSegment",
    "MeasureSpan",
    "ReadinessIssue",
    "ScorePosition",
    "ScoreSpan",
    "SourceArtifact",
    "SourceMappingDocument",
    "SourceMappingRef",
    "SourceRole",
    "TimeSignature",
    "TimelineDocument",
    "TimelineRef",
    "WorkIdentity",
]
