from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from aimusic.takes.lifecycle import (
    ANALYSIS_TRANSITIONS,
    AnalysisState,
    BundleRef,
    IllegalTransitionError,
    JobKind,
    JobRecord,
    JobState,
    LifecycleFailure,
    ProfileMembership,
    ProfileRef,
    RunMode,
    RunPhase,
    RunRecord,
    TakeCueV2,
    TakeDocV2,
    UserDisposition,
    set_disposition,
    set_profile_membership,
    stable_candidate_id,
    transition_analysis,
    transition_job,
    transition_run,
)

NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
BUNDLE = BundleRef(
    bundle_id="chopin_op11_movement_2",
    revision="bundle-r1",
    timeline_id="timeline-r1",
)


def _take(**updates: object) -> TakeDocV2:
    values: dict[str, object] = {
        "take_id": "take-1",
        "bundle": BUNDLE,
        "recorded_at": NOW,
        "updated_at": NOW,
        "duration_seconds": 10.0,
        "note_on_count": 20,
        "midi_path": "take.mid",
    }
    values.update(updates)
    return TakeDocV2(**values)


def test_take_cue_v2_reads_but_drops_retired_fixed_transport_fields() -> None:
    cue = TakeCueV2.model_validate(
        {
            "kind": "from_position",
            "target_score_tick": 45_120,
            "cue_start_score_tick": 42_240,
            "cue_seconds": 2.8,
            "output_name": "CLP-795GP USB",
            "tempo_scale": 1.2,
            "handoff": "at_entry",
        }
    )

    assert cue.model_dump() == {
        "kind": "from_position",
        "target_score_tick": 45_120,
        "cue_start_score_tick": 42_240,
        "cue_seconds": 2.8,
        "output_name": "CLP-795GP USB",
    }


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (source, target)
        for source, targets in ANALYSIS_TRANSITIONS.items()
        for target in targets
    ],
)
def test_every_declared_analysis_transition_is_executable(
    source: AnalysisState, target: AnalysisState
) -> None:
    kwargs: dict[str, object] = {"analysis_state": source}
    if source == AnalysisState.ALIGNED:
        kwargs["alignment_artifact"] = "aligned.v2.json"
    if source == AnalysisState.AMBIGUOUS:
        kwargs["alignment_artifact"] = "aligned.v2.json"
    if source == AnalysisState.FAILED:
        kwargs["failure"] = LifecycleFailure(code="worker", message="crashed")
    take = _take(**kwargs)
    transition_kwargs: dict[str, object] = {}
    if target in {AnalysisState.ALIGNED, AnalysisState.AMBIGUOUS}:
        transition_kwargs["alignment_artifact"] = "aligned.v2.json"
    if target == AnalysisState.FAILED:
        transition_kwargs["failure"] = LifecycleFailure(code="worker", message="crashed")

    result = transition_analysis(take, target, now=NOW, **transition_kwargs)

    assert result.analysis_state == target
    assert result.lifecycle_revision == 1


def test_undeclared_analysis_transition_is_rejected() -> None:
    with pytest.raises(IllegalTransitionError, match="captured -> aligned"):
        transition_analysis(_take(), AnalysisState.ALIGNED, alignment_artifact="aligned.v2.json")


def test_discard_and_restore_do_not_destroy_analysis_result() -> None:
    aligned = _take(
        analysis_state=AnalysisState.ALIGNED,
        alignment_artifact="aligned.v2.json",
        profile_membership=ProfileMembership.INCLUDED,
    )

    discarded = set_disposition(aligned, UserDisposition.DISCARDED, now=NOW)
    restored = set_disposition(discarded, UserDisposition.KEPT, now=NOW)

    assert discarded.analysis_state == AnalysisState.ALIGNED
    assert discarded.profile_membership == ProfileMembership.EXCLUDED
    assert restored.analysis_state == AnalysisState.ALIGNED
    assert restored.disposition == UserDisposition.KEPT
    # Restoration never silently puts an old take back into the learned prior.
    assert restored.profile_membership == ProfileMembership.EXCLUDED
    assert set_profile_membership(restored, ProfileMembership.INCLUDED).profile_membership == (
        ProfileMembership.INCLUDED
    )


def test_failed_and_unalignable_are_distinct_outcomes() -> None:
    running = _take(analysis_state=AnalysisState.RUNNING)
    unalignable = transition_analysis(running, AnalysisState.UNALIGNABLE)
    failed = transition_analysis(
        running,
        AnalysisState.FAILED,
        failure=LifecycleFailure(code="midi_parse", message="bad file", retryable=False),
    )

    assert unalignable.failure is None
    assert failed.failure is not None
    with pytest.raises(ValidationError, match="failed takes require failure"):
        _take(analysis_state=AnalysisState.FAILED)


def test_discarded_or_unaligned_take_cannot_be_included() -> None:
    with pytest.raises(ValidationError, match="discarded takes"):
        _take(
            disposition=UserDisposition.DISCARDED,
            profile_membership=ProfileMembership.INCLUDED,
            analysis_state=AnalysisState.ALIGNED,
            alignment_artifact="aligned.v2.json",
        )
    with pytest.raises(ValidationError, match="must be aligned"):
        _take(profile_membership=ProfileMembership.INCLUDED)


def test_job_attempts_increment_on_start_and_failure_can_retry() -> None:
    queued = JobRecord(
        job_id="job-1",
        kind=JobKind.ALIGN,
        bundle=BUNDLE,
        input_revision="take-r1",
        requested_at=NOW,
    )
    running = transition_job(queued, target=JobState.RUNNING, now=NOW)
    failed = transition_job(
        running,
        target=JobState.FAILED,
        now=NOW,
        failure=LifecycleFailure(code="worker", message="crashed", retryable=True),
    )
    retried = transition_job(failed, target=JobState.QUEUED, now=NOW)

    assert running.attempt == 1
    assert failed.finished_at == NOW
    assert retried.failure is None
    assert retried.started_at is None


def test_performance_profile_is_frozen_at_activation() -> None:
    profile = ProfileRef(profile_id="soloist", revision="profile-r7", bundle=BUNDLE)
    run = RunRecord(
        run_id="performance-1",
        mode=RunMode.PERFORMANCE,
        bundle=BUNDLE,
        created_at=NOW,
    )
    listening = transition_run(run, RunPhase.LISTENING, now=NOW)
    active = transition_run(listening, RunPhase.ACTIVE, now=NOW, profile=profile)

    assert active.profile == profile
    assert active.profile_frozen is True
    changed = profile.model_copy(update={"revision": "profile-r8"})
    with pytest.raises(IllegalTransitionError, match="cannot be changed"):
        transition_run(active, RunPhase.STOPPING, profile=changed)


def test_performance_cannot_activate_without_profile() -> None:
    run = RunRecord(
        run_id="performance-1",
        mode=RunMode.PERFORMANCE,
        bundle=BUNDLE,
        created_at=NOW,
        phase=RunPhase.LISTENING,
    )
    with pytest.raises(IllegalTransitionError, match="requires a profile"):
        transition_run(run, RunPhase.ACTIVE)


def test_stable_candidate_id_is_order_independent_identity() -> None:
    first = stable_candidate_id("take-1", start_score_tick=960, start_position=12)
    assert first == stable_candidate_id("take-1", start_score_tick=960, start_position=12)
    assert first != stable_candidate_id("take-1", start_score_tick=1920, start_position=12)
