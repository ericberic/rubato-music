"""Version 2 rehearsal and performance lifecycle contracts.

The v1 take ``status`` mixed three independent facts: whether analysis had
finished, whether the performer wanted to keep the take, and whether the take
was eligible for the learned profile.  V2 makes those axes explicit and routes
changes through the transition functions in this module.

These models are deliberately independent of the score-bundle implementation.
``BundleRef`` is the narrow compatibility seam: bundle v2 may provide richer
metadata, while lifecycle artifacts only pin immutable identities/revisions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aimusic.accompaniment.bundle_v2 import BundleRef


class LifecycleModel(BaseModel):
    """Strict, immutable base for v2 persisted lifecycle artifacts."""

    model_config = ConfigDict(frozen=True, extra="forbid", use_enum_values=False)


class AnalysisState(StrEnum):
    CAPTURED = "captured"
    QUEUED = "queued"
    RUNNING = "running"
    ALIGNED = "aligned"
    AMBIGUOUS = "ambiguous"
    UNALIGNABLE = "unalignable"
    FAILED = "failed"


class UserDisposition(StrEnum):
    KEPT = "kept"
    DISCARDED = "discarded"


class ProfileMembership(StrEnum):
    PENDING = "pending"
    INCLUDED = "included"
    EXCLUDED = "excluded"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobKind(StrEnum):
    ALIGN = "align"
    RESOLVE_ALIGNMENT = "resolve_alignment"
    RENDER_REVIEW = "render_review"
    REBUILD_PROFILE = "rebuild_profile"
    REBUILD_COVERAGE = "rebuild_coverage"


class RunMode(StrEnum):
    REHEARSAL = "rehearsal"
    PERFORMANCE = "performance"


class RunPhase(StrEnum):
    PREPARING = "preparing"
    LISTENING = "listening"
    ACTIVE = "active"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"


class ProfileRef(LifecycleModel):
    profile_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    bundle: BundleRef


class LifecycleFailure(LifecycleModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool = False


class TakeCueV2(LifecycleModel):
    # Steps 1–3 of Decision 0015 briefly persisted these cues with
    # `tempo_scale` and `handoff`. Read those local artifacts without
    # preserving the retired fixed-clock/authority fields on the next write.
    model_config = ConfigDict(frozen=True, extra="ignore", use_enum_values=False)

    kind: Literal["from_top", "from_position"]
    target_score_tick: int = Field(ge=0)
    cue_start_score_tick: int | None = Field(default=None, ge=0)
    cue_seconds: float = Field(ge=0.0)
    output_name: str = Field(min_length=1)


class TakePlacementHintV2(LifecycleModel):
    """A selected score entry that remains attached to a take without a cue."""

    target_score_tick: int = Field(ge=0)
    source: Literal["selected_passage"] = "selected_passage"


class TakeDocV2(LifecycleModel):
    """Authoritative v2 metadata for a captured rehearsal take."""

    schema_version: Literal[2] = 2
    take_id: str = Field(min_length=1)
    bundle: BundleRef
    recorded_at: datetime
    updated_at: datetime
    duration_seconds: float = Field(ge=0.0)
    note_on_count: int = Field(ge=0)
    input_name: str | None = None
    cue: TakeCueV2 | None = None
    placement_hint: TakePlacementHintV2 | None = None
    midi_path: str = Field(min_length=1)
    alignment_artifact: str | None = None
    analysis_state: AnalysisState = AnalysisState.CAPTURED
    disposition: UserDisposition = UserDisposition.KEPT
    profile_membership: ProfileMembership = ProfileMembership.PENDING
    failure: LifecycleFailure | None = None
    lifecycle_revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_lifecycle_invariants(self) -> Self:
        _require_aware(self.recorded_at, "recorded_at")
        _require_aware(self.updated_at, "updated_at")
        if self.analysis_state in {AnalysisState.ALIGNED, AnalysisState.AMBIGUOUS} and not (
            self.alignment_artifact
        ):
            raise ValueError("aligned and ambiguous takes require alignment_artifact")
        if self.analysis_state == AnalysisState.FAILED and self.failure is None:
            raise ValueError("failed takes require failure details")
        if self.analysis_state != AnalysisState.FAILED and self.failure is not None:
            raise ValueError("failure details are only valid for failed takes")
        if (
            self.disposition == UserDisposition.DISCARDED
            and self.profile_membership == ProfileMembership.INCLUDED
        ):
            raise ValueError("discarded takes cannot be included")
        if (
            self.profile_membership == ProfileMembership.INCLUDED
            and self.analysis_state != AnalysisState.ALIGNED
        ):
            raise ValueError("included takes must be aligned")
        return self


class AlignmentCandidateV2(LifecycleModel):
    candidate_id: str = Field(min_length=1)
    start_score_tick: int = Field(ge=0)
    score: float
    start_position: int | None = Field(default=None, ge=0)
    reference_start_tick: int | None = Field(default=None, ge=0)


class TimingMapPointV2(LifecycleModel):
    score_tick: int = Field(ge=0)
    reference_tick: int | None = Field(default=None, ge=0)
    take_seconds: float = Field(ge=0.0)


class CellSampleV2(LifecycleModel):
    """One take's observation on the canonical half-quarter grid."""

    score_tick: int = Field(ge=0)
    seconds_per_quarter: float = Field(gt=0.0)
    rubato_ratio: float = Field(gt=0.0)
    velocity: float = Field(ge=0.0, le=127.0)
    pedal: float = Field(ge=0.0, le=1.0)
    quality: float = Field(ge=0.0, le=1.0)


class AlignedResultV2(LifecycleModel):
    """Minimal v2 alignment envelope with stable candidate addressing."""

    schema_version: Literal[2] = 2
    take_id: str = Field(min_length=1)
    bundle: BundleRef
    aligner: str = Field(min_length=1)
    coordinate_system: Literal["reference_midi_compat", "canonical_score"] = "reference_midi_compat"
    start_score_tick: int = Field(ge=0)
    end_score_tick: int = Field(ge=0)
    start_reference_tick: int | None = Field(default=None, ge=0)
    end_reference_tick: int | None = Field(default=None, ge=0)
    mapping_id: str | None = None
    match_rate: float = Field(ge=0.0, le=1.0)
    ambiguous: bool
    matched_notes: int = Field(ge=0)
    extra_notes: int = Field(ge=0)
    missing_notes: int = Field(ge=0)
    timing_map: tuple[TimingMapPointV2, ...] = ()
    candidates: tuple[AlignmentCandidateV2, ...] = ()
    performance_model: Literal["canonical-performance-v2"] = "canonical-performance-v2"
    profile_grid_step_ticks: Literal[480] = 480
    base_seconds_per_quarter: float | None = Field(default=None, gt=0.0)
    cell_samples: tuple[CellSampleV2, ...] = ()
    edge_trim_ticks: tuple[int, int] = (0, 0)

    @model_validator(mode="after")
    def validate_span_and_candidates(self) -> Self:
        if self.end_score_tick < self.start_score_tick:
            raise ValueError("end_score_tick must not precede start_score_tick")
        ids = [candidate.candidate_id for candidate in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate_id values must be unique")
        if self.coordinate_system == "canonical_score":
            if self.start_reference_tick is None or self.end_reference_tick is None:
                raise ValueError("canonical alignments require explicit reference endpoints")
            if any(point.reference_tick is None for point in self.timing_map):
                raise ValueError("canonical timing points require reference_tick")
            if not self.mapping_id:
                raise ValueError("canonical alignments require mapping_id")
        if any(sample.score_tick % self.profile_grid_step_ticks for sample in self.cell_samples):
            raise ValueError("cell samples must lie on the canonical profile grid")
        if self.cell_samples and self.base_seconds_per_quarter is None:
            raise ValueError("cell samples require base_seconds_per_quarter")
        return self


class JobRecord(LifecycleModel):
    schema_version: Literal[2] = 2
    job_id: str = Field(min_length=1)
    kind: JobKind
    state: JobState = JobState.QUEUED
    take_id: str | None = None
    candidate_id: str | None = None
    bundle: BundleRef
    input_revision: str = Field(min_length=1)
    attempt: int = Field(default=0, ge=0)
    requested_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    failure: LifecycleFailure | None = None

    @model_validator(mode="after")
    def validate_job_invariants(self) -> Self:
        for name in ("requested_at", "started_at", "finished_at"):
            value = getattr(self, name)
            if value is not None:
                _require_aware(value, name)
        if self.state == JobState.RUNNING and self.started_at is None:
            raise ValueError("running jobs require started_at")
        if self.state in {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}:
            if self.finished_at is None:
                raise ValueError("terminal jobs require finished_at")
        elif self.finished_at is not None:
            raise ValueError("non-terminal jobs cannot have finished_at")
        if self.state == JobState.FAILED and self.failure is None:
            raise ValueError("failed jobs require failure details")
        if self.state != JobState.FAILED and self.failure is not None:
            raise ValueError("failure details are only valid for failed jobs")
        if self.kind == JobKind.RESOLVE_ALIGNMENT and not self.candidate_id:
            raise ValueError("resolve_alignment jobs require candidate_id")
        if self.kind != JobKind.RESOLVE_ALIGNMENT and self.candidate_id is not None:
            raise ValueError("candidate_id is only valid for resolve_alignment jobs")
        return self


class RunRecord(LifecycleModel):
    schema_version: Literal[2] = 2
    run_id: str = Field(min_length=1)
    mode: RunMode
    phase: RunPhase = RunPhase.PREPARING
    bundle: BundleRef
    profile: ProfileRef | None = None
    profile_frozen: bool = False
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    failure: LifecycleFailure | None = None

    @model_validator(mode="after")
    def validate_run_invariants(self) -> Self:
        for name in ("created_at", "started_at", "finished_at"):
            value = getattr(self, name)
            if value is not None:
                _require_aware(value, name)
        terminal = self.phase in {RunPhase.COMPLETED, RunPhase.FAILED}
        if terminal != (self.finished_at is not None):
            raise ValueError("terminal run phases and finished_at must agree")
        if self.phase == RunPhase.FAILED and self.failure is None:
            raise ValueError("failed runs require failure details")
        if self.phase != RunPhase.FAILED and self.failure is not None:
            raise ValueError("failure details are only valid for failed runs")
        if self.mode == RunMode.PERFORMANCE and self.phase in {
            RunPhase.ACTIVE,
            RunPhase.STOPPING,
            RunPhase.COMPLETED,
        }:
            if self.profile is None or not self.profile_frozen:
                raise ValueError("active performance runs require a frozen profile revision")
        if self.profile_frozen and self.profile is None:
            raise ValueError("profile_frozen requires a profile")
        if self.profile is not None and self.profile.bundle != self.bundle:
            raise ValueError("run profile and run bundle revisions must match")
        return self


ANALYSIS_TRANSITIONS: dict[AnalysisState, frozenset[AnalysisState]] = {
    AnalysisState.CAPTURED: frozenset({AnalysisState.QUEUED}),
    AnalysisState.QUEUED: frozenset({AnalysisState.RUNNING, AnalysisState.CAPTURED}),
    AnalysisState.RUNNING: frozenset(
        {
            AnalysisState.ALIGNED,
            AnalysisState.AMBIGUOUS,
            AnalysisState.UNALIGNABLE,
            AnalysisState.FAILED,
        }
    ),
    AnalysisState.ALIGNED: frozenset({AnalysisState.QUEUED}),
    AnalysisState.AMBIGUOUS: frozenset({AnalysisState.ALIGNED, AnalysisState.QUEUED}),
    AnalysisState.UNALIGNABLE: frozenset({AnalysisState.QUEUED}),
    AnalysisState.FAILED: frozenset({AnalysisState.QUEUED}),
}

JOB_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.CANCELLED}),
    JobState.RUNNING: frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}),
    JobState.SUCCEEDED: frozenset(),
    JobState.FAILED: frozenset({JobState.QUEUED}),
    JobState.CANCELLED: frozenset({JobState.QUEUED}),
}

RUN_TRANSITIONS: dict[RunPhase, frozenset[RunPhase]] = {
    RunPhase.PREPARING: frozenset({RunPhase.LISTENING, RunPhase.FAILED}),
    RunPhase.LISTENING: frozenset({RunPhase.ACTIVE, RunPhase.STOPPING, RunPhase.FAILED}),
    RunPhase.ACTIVE: frozenset({RunPhase.STOPPING, RunPhase.FAILED}),
    RunPhase.STOPPING: frozenset({RunPhase.COMPLETED, RunPhase.FAILED}),
    RunPhase.COMPLETED: frozenset(),
    RunPhase.FAILED: frozenset(),
}


class IllegalTransitionError(ValueError):
    """Raised when a lifecycle command violates the documented state machine."""


def transition_analysis(
    take: TakeDocV2,
    target: AnalysisState,
    *,
    now: datetime | None = None,
    alignment_artifact: str | None = None,
    failure: LifecycleFailure | None = None,
) -> TakeDocV2:
    _assert_transition("analysis", take.analysis_state, target, ANALYSIS_TRANSITIONS)
    updates: dict[str, object] = {
        "analysis_state": target,
        "updated_at": now or _utc_now(),
        "lifecycle_revision": take.lifecycle_revision + 1,
        "failure": failure if target == AnalysisState.FAILED else None,
    }
    if target in {AnalysisState.ALIGNED, AnalysisState.AMBIGUOUS}:
        updates["alignment_artifact"] = alignment_artifact or take.alignment_artifact
    elif target in {AnalysisState.QUEUED, AnalysisState.RUNNING}:
        updates["profile_membership"] = ProfileMembership.PENDING
    updated = take.model_copy(update=updates, deep=True)
    return TakeDocV2.model_validate(updated.model_dump())


def set_disposition(
    take: TakeDocV2,
    disposition: UserDisposition,
    *,
    now: datetime | None = None,
) -> TakeDocV2:
    if take.disposition == disposition:
        return take
    membership = take.profile_membership
    if disposition == UserDisposition.DISCARDED:
        membership = ProfileMembership.EXCLUDED
    updated = take.model_copy(
        update={
            "disposition": disposition,
            "profile_membership": membership,
            "updated_at": now or _utc_now(),
            "lifecycle_revision": take.lifecycle_revision + 1,
        }
    )
    return TakeDocV2.model_validate(updated.model_dump())


def set_profile_membership(
    take: TakeDocV2,
    membership: ProfileMembership,
    *,
    now: datetime | None = None,
) -> TakeDocV2:
    if take.profile_membership == membership:
        return take
    updated = take.model_copy(
        update={
            "profile_membership": membership,
            "updated_at": now or _utc_now(),
            "lifecycle_revision": take.lifecycle_revision + 1,
        }
    )
    return TakeDocV2.model_validate(updated.model_dump())


def transition_job(
    job: JobRecord,
    target: JobState,
    *,
    now: datetime | None = None,
    failure: LifecycleFailure | None = None,
) -> JobRecord:
    _assert_transition("job", job.state, target, JOB_TRANSITIONS)
    moment = now or _utc_now()
    updates: dict[str, object] = {"state": target, "failure": None}
    if target == JobState.RUNNING:
        updates.update(started_at=moment, finished_at=None, attempt=job.attempt + 1)
    elif target in {JobState.SUCCEEDED, JobState.CANCELLED}:
        updates["finished_at"] = moment
    elif target == JobState.FAILED:
        updates.update(finished_at=moment, failure=failure)
    elif target == JobState.QUEUED:
        updates.update(started_at=None, finished_at=None)
    updated = job.model_copy(update=updates)
    return JobRecord.model_validate(updated.model_dump())


def transition_run(
    run: RunRecord,
    target: RunPhase,
    *,
    now: datetime | None = None,
    profile: ProfileRef | None = None,
    failure: LifecycleFailure | None = None,
) -> RunRecord:
    _assert_transition("run", run.phase, target, RUN_TRANSITIONS)
    moment = now or _utc_now()
    if run.profile_frozen and profile is not None and profile != run.profile:
        raise IllegalTransitionError("a frozen run profile revision cannot be changed")
    selected_profile = profile or run.profile
    updates: dict[str, object] = {"phase": target, "failure": None}
    if target == RunPhase.ACTIVE:
        updates["started_at"] = run.started_at or moment
        if run.mode == RunMode.PERFORMANCE:
            if selected_profile is None:
                raise IllegalTransitionError("performance activation requires a profile")
            updates.update(profile=selected_profile, profile_frozen=True)
        elif selected_profile is not None:
            updates["profile"] = selected_profile
    if target in {RunPhase.COMPLETED, RunPhase.FAILED}:
        updates["finished_at"] = moment
    if target == RunPhase.FAILED:
        updates["failure"] = failure
    updated = run.model_copy(update=updates)
    return RunRecord.model_validate(updated.model_dump())


def stable_candidate_id(
    take_id: str,
    *,
    start_score_tick: int,
    start_position: int | None,
) -> str:
    """Return deterministic candidate identity independent of list ordering."""

    source_position = start_position if start_position is not None else ""
    material = f"{take_id}\0{start_score_tick}\0{source_position}"
    return "cand_" + sha256(material.encode("utf-8")).hexdigest()[:16]


def _assert_transition(name: str, source: StrEnum, target: StrEnum, table: dict) -> None:
    if target not in table[source]:
        raise IllegalTransitionError(f"illegal {name} transition: {source.value} -> {target.value}")


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "ANALYSIS_TRANSITIONS",
    "JOB_TRANSITIONS",
    "RUN_TRANSITIONS",
    "AlignedResultV2",
    "AlignmentCandidateV2",
    "AnalysisState",
    "BundleRef",
    "CellSampleV2",
    "IllegalTransitionError",
    "JobKind",
    "JobRecord",
    "JobState",
    "LifecycleFailure",
    "ProfileMembership",
    "ProfileRef",
    "RunMode",
    "RunPhase",
    "RunRecord",
    "TakeCueV2",
    "TakeDocV2",
    "TimingMapPointV2",
    "UserDisposition",
    "set_disposition",
    "set_profile_membership",
    "stable_candidate_id",
    "transition_analysis",
    "transition_job",
    "transition_run",
]
