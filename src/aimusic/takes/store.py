"""Durable rehearsal-take store with lifecycle-v2 authority.

``take.v2.json`` and ``aligned.v2.json`` are authoritative.  Legacy sidecars
remain dual-written/read during the UI migration; callers that still consume
``TakeRecord.status`` receive an explicit compatibility projection.
"""

from __future__ import annotations

import logging
import secrets
from contextlib import nullcontext
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from collections.abc import Callable
from threading import Lock, RLock
from typing import Any
from uuid import uuid4

from mido import MidiFile

from aimusic.accompaniment.offline_alignment import extract_note_events
from aimusic.core import paths
from aimusic.core.time import utc_now
from aimusic.takes.lifecycle import (
    AlignedResultV2,
    AlignmentCandidateV2,
    AnalysisState,
    JobKind,
    JobRecord,
    JobState,
    LifecycleFailure,
    ProfileMembership,
    TakeDocV2,
    TimingMapPointV2,
    UserDisposition,
    set_disposition,
    set_profile_membership,
    stable_candidate_id,
    transition_analysis,
    transition_job,
)
from aimusic.takes.migration import migrate_alignment, migrate_take_directory
from aimusic.takes.models import (
    AlignedResult,
    Anchor,
    AnchorSet,
    TakeCue,
    TakePlacementHint,
    TakeRecord,
    TakeStatus,
)

CAPTURED = "captured"
ALIGNING = "aligning"
ALIGNED = "aligned"
AMBIGUOUS = "ambiguous"
UNALIGNABLE = "unalignable"
FAILED = "failed"
DISCARDED = "discarded"
TAKE_MIDI_FILENAME = "take.mid"
CANONICAL_PPQ = 960
Take = TakeRecord
_JOB_LOCKS_GUARD = Lock()
_JOB_LOCKS: dict[tuple[str, int], RLock] = {}


_FINALIZE_GUARD = Lock()
_FINALIZE_LOCKS: dict[tuple[str, int, str], RLock] = {}


def _finalize_lock(piece_id: str, movement: int, take_id: str) -> RLock:
    key = (piece_id, movement, take_id)
    with _FINALIZE_GUARD:
        lock = _FINALIZE_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _FINALIZE_LOCKS[key] = lock
        return lock


def finalize_take(
    piece_id: str,
    movement: int,
    take_id: str,
    source_midi_path: Path | str,
    *,
    cue_provider: "Callable[[], tuple[Any, Any]] | None" = None,
    input_name: str | None = None,
    aborted: bool = False,
) -> tuple[Take, bool]:
    """Register a finished recording exactly once, whatever triggered the stop.

    A take can be completed by the normal stop endpoint *or* by an abort path
    (Silence/panic/stop), and those can race: both would check "is it saved
    yet?", both see no, and both save. The second write previously clobbered the
    first -- stripping the cue metadata the first had already consumed -- and
    published a duplicate event plus a second alignment job.

    Finalization is therefore owned here, behind a per-take lock, and is
    idempotent: the returned flag says whether *this* call performed it. The cue
    is fetched through ``cue_provider`` **inside** the lock so it is consumed by
    whichever caller actually wins, never popped by a loser.

    Returns ``(take, finalized_now)``.
    """

    source = Path(source_midi_path)
    with _finalize_lock(piece_id, movement, take_id):
        try:
            return get_take(piece_id, movement, take_id), False
        except TakeNotFoundError:
            pass
        cue, placement_hint = cue_provider() if cue_provider is not None else (None, None)
        take = save_take(
            piece_id,
            movement,
            source,
            take_id=take_id,
            input_name=input_name,
            cue=cue,
            placement_hint=placement_hint,
        )
        if aborted:
            # An abort is not a clean read of the performer's intent: the take
            # may be truncated or a ruined attempt. Keep it (never lose playing)
            # but hold it out of the fitted model until reviewed, so a botched
            # stop cannot quietly move the live accompanist's tempo prior.
            set_take_profile_membership(
                piece_id, movement, take_id, ProfileMembership.EXCLUDED
            )
        return take, True


class TakeNotFoundError(LookupError):
    pass


class JobNotFoundError(LookupError):
    pass


class AnchorNotFoundError(ValueError):
    pass


class AnchorConflictError(ValueError):
    pass


def generate_take_id(*, now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return f"t{moment.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(2)}"


def save_take(
    piece_id: str,
    movement: int,
    source_midi_path: Path | str,
    *,
    take_id: str | None = None,
    input_name: str | None = None,
    cue: TakeCue | None = None,
    placement_hint: TakePlacementHint | None = None,
    recorded_at: datetime | str | None = None,
    status: TakeStatus = CAPTURED,
) -> Take:
    source = Path(source_midi_path)
    if not source.exists():
        raise FileNotFoundError(f"Take source MIDI not found: {source}")
    resolved_id = take_id or generate_take_id()
    directory = paths.take_dir(piece_id, movement, resolved_id, create=True)
    midi_path = directory / TAKE_MIDI_FILENAME
    midi_path.write_bytes(source.read_bytes())
    legacy = Take(
        take_id=resolved_id,
        piece_id=piece_id,
        movement=movement,
        recorded_at=recorded_at or utc_now(),
        duration_seconds=_midi_duration_seconds(midi_path),
        note_on_count=len(extract_note_events(midi_path)),
        input_name=input_name,
        cue=cue,
        placement_hint=placement_hint,
        status=status,
        midi_path=TAKE_MIDI_FILENAME,
    )
    _atomic_model(directory / "take.json", legacy)
    migrated = migrate_take_directory(directory, apply=True)
    if migrated.take is None:
        raise RuntimeError(f"failed to create lifecycle artifact for {resolved_id}")
    _append_manifest(piece_id, movement, legacy)
    return legacy


def get_take_v2(piece_id: str, movement: int, take_id: str) -> TakeDocV2:
    directory = paths.take_dir(piece_id, movement, take_id, create=False)
    path = directory / "take.v2.json"
    if not path.exists():
        if not (directory / "take.json").exists():
            raise TakeNotFoundError(f"Take not found: {piece_id}/{movement}/{take_id}")
        result = migrate_take_directory(directory, apply=True)
        if result.take is None:
            raise TakeNotFoundError(f"Take not found: {piece_id}/{movement}/{take_id}")
        return result.take
    return TakeDocV2.model_validate_json(path.read_text(encoding="utf-8"))


def get_take(piece_id: str, movement: int, take_id: str) -> Take:
    v2 = get_take_v2(piece_id, movement, take_id)
    return _legacy_projection(piece_id, movement, v2)


def list_takes_v2(piece_id: str, movement: int) -> list[TakeDocV2]:
    directory = paths.take_movement_dir(piece_id, movement, create=False)
    if not directory.exists():
        return []
    takes = []
    for item in directory.iterdir():
        if item.is_dir() and item.name.startswith("t"):
            try:
                takes.append(get_take_v2(piece_id, movement, item.name))
            except TakeNotFoundError:
                pass
    return sorted(takes, key=lambda take: take.recorded_at)


def list_takes(piece_id: str, movement: int) -> list[Take]:
    return [
        _legacy_projection(piece_id, movement, take) for take in list_takes_v2(piece_id, movement)
    ]


def transition_take_analysis(
    piece_id: str,
    movement: int,
    take_id: str,
    target: AnalysisState,
    *,
    alignment_artifact: str | None = None,
    failure: LifecycleFailure | None = None,
) -> TakeDocV2:
    current = get_take_v2(piece_id, movement, take_id)
    updated = transition_analysis(
        current, target, alignment_artifact=alignment_artifact, failure=failure
    )
    if target == AnalysisState.ALIGNED and updated.disposition == UserDisposition.KEPT:
        updated = set_profile_membership(updated, ProfileMembership.INCLUDED)
    elif target in {AnalysisState.AMBIGUOUS, AnalysisState.UNALIGNABLE, AnalysisState.FAILED}:
        updated = set_profile_membership(updated, ProfileMembership.EXCLUDED)
    _write_take_v2_and_legacy(piece_id, movement, updated)
    return updated


def update_take_status(piece_id: str, movement: int, take_id: str, status: TakeStatus) -> Take:
    """Compatibility command for old callers; new code uses explicit transitions."""
    current = get_take_v2(piece_id, movement, take_id)
    if status == DISCARDED:
        updated = set_disposition(current, UserDisposition.DISCARDED)
    else:
        target = {
            CAPTURED: AnalysisState.CAPTURED,
            ALIGNING: AnalysisState.RUNNING,
            ALIGNED: AnalysisState.ALIGNED,
            AMBIGUOUS: AnalysisState.AMBIGUOUS,
            UNALIGNABLE: AnalysisState.UNALIGNABLE,
            FAILED: AnalysisState.FAILED,
        }[status]
        # Preserve compatibility for tests/tools that historically jumped
        # directly between states, while production uses transition service.
        updated = current.model_copy(
            update={
                "analysis_state": target,
                "updated_at": utc_now(),
                "lifecycle_revision": current.lifecycle_revision + 1,
                "alignment_artifact": "aligned.v2.json"
                if target in {AnalysisState.ALIGNED, AnalysisState.AMBIGUOUS}
                else current.alignment_artifact,
                "failure": LifecycleFailure(code="legacy_failure", message="legacy failure")
                if target == AnalysisState.FAILED
                else None,
            }
        )
        updated = TakeDocV2.model_validate(updated.model_dump())
        if target == AnalysisState.ALIGNED and updated.disposition == UserDisposition.KEPT:
            updated = set_profile_membership(updated, ProfileMembership.INCLUDED)
    _write_take_v2_and_legacy(piece_id, movement, updated)
    return _legacy_projection(piece_id, movement, updated)


def discard_take(piece_id: str, movement: int, take_id: str) -> Take:
    updated = set_disposition(get_take_v2(piece_id, movement, take_id), UserDisposition.DISCARDED)
    _write_take_v2_and_legacy(piece_id, movement, updated)
    return _legacy_projection(piece_id, movement, updated)


def restore_take(piece_id: str, movement: int, take_id: str) -> Take:
    updated = set_disposition(get_take_v2(piece_id, movement, take_id), UserDisposition.KEPT)
    _write_take_v2_and_legacy(piece_id, movement, updated)
    return _legacy_projection(piece_id, movement, updated)


def set_take_profile_membership(
    piece_id: str, movement: int, take_id: str, membership: ProfileMembership
) -> TakeDocV2:
    updated = set_profile_membership(get_take_v2(piece_id, movement, take_id), membership)
    _write_take_v2_and_legacy(piece_id, movement, updated)
    return updated


def write_aligned_result(
    piece_id: str, movement: int, take_id: str, aligned: AlignedResult
) -> None:
    directory = paths.take_dir(piece_id, movement, take_id, create=True)
    _atomic_model(directory / "aligned.json", aligned)
    bundle = get_take_v2(piece_id, movement, take_id).bundle
    migrated = migrate_alignment(aligned, bundle=bundle, canonical_ppq=CANONICAL_PPQ)
    if (piece_id, movement) == ("chopin_op11", 2):
        migrated = _canonicalize_movement2_alignment(migrated)
    else:
        migrated = _canonicalize_identity_alignment(migrated)
    migrated = _resample_performance_cells(piece_id, movement, take_id, migrated)
    _atomic_model(directory / "aligned.v2.json", migrated)


def _canonicalize_identity_alignment(aligned: AlignedResultV2) -> AlignedResultV2:
    """Promote a score-synchronous reference into the canonical coordinate."""

    return aligned.model_copy(
        update={
            "coordinate_system": "canonical_score",
            "start_reference_tick": aligned.start_reference_tick or aligned.start_score_tick,
            "end_reference_tick": aligned.end_reference_tick or aligned.end_score_tick,
            "mapping_id": aligned.mapping_id or aligned.bundle.timeline_id,
            "timing_map": tuple(
                point.model_copy(
                    update={"reference_tick": point.reference_tick or point.score_tick}
                )
                for point in aligned.timing_map
            ),
        }
    )


def _canonicalize_movement2_alignment(aligned: AlignedResultV2) -> AlignedResultV2:
    """Promote reference-MIDI compatibility ticks through the fused beat map."""

    from aimusic.accompaniment.rehearsal_position import score_projection

    projection = score_projection("chopin_op11", 2)
    if projection.anchors and aligned.end_score_tick < projection.anchors[0][0]:
        # Synthetic fixtures and pre-roll-only captures can sit entirely
        # before the mapping's first audible source anchor.  Preserve their
        # explicit reference coordinate rather than falsely collapsing every
        # point onto canonical tick zero.
        return _canonicalize_identity_alignment(aligned)

    def canonical(reference_tick: int) -> int:
        return projection.position_at_source_tick(reference_tick).score_tick

    candidates = tuple(
        AlignmentCandidateV2(
            candidate_id=stable_candidate_id(
                aligned.take_id,
                start_score_tick=canonical(candidate.start_score_tick),
                start_position=candidate.start_position,
            ),
            start_score_tick=canonical(candidate.start_score_tick),
            reference_start_tick=candidate.start_score_tick,
            score=candidate.score,
            start_position=candidate.start_position,
        )
        for candidate in aligned.candidates
    )
    return aligned.model_copy(
        update={
            "coordinate_system": "canonical_score",
            "start_reference_tick": aligned.start_score_tick,
            "end_reference_tick": aligned.end_score_tick,
            "start_score_tick": canonical(aligned.start_score_tick),
            "end_score_tick": canonical(aligned.end_score_tick),
            "mapping_id": projection.mapping_id,
            "timing_map": tuple(
                TimingMapPointV2(
                    score_tick=canonical(point.score_tick),
                    reference_tick=point.score_tick,
                    take_seconds=point.take_seconds,
                )
                for point in aligned.timing_map
            ),
            "candidates": candidates,
            "cell_samples": (),
            "base_seconds_per_quarter": None,
        }
    )


def _resample_performance_cells(
    piece_id: str,
    movement: int,
    take_id: str,
    aligned: AlignedResultV2,
) -> AlignedResultV2:
    """Derive profile cells from the canonical timing map, never source time."""

    from aimusic.takes.profile import compute_canonical_cell_samples

    take = get_take_v2(piece_id, movement, take_id)
    midi_path = paths.take_dir(piece_id, movement, take_id, create=False) / take.midi_path
    samples, base = compute_canonical_cell_samples(
        midi_path,
        start_score_tick=aligned.start_score_tick,
        end_score_tick=aligned.end_score_tick,
        match_rate=aligned.match_rate,
        timing_map=aligned.timing_map,
    )
    return aligned.model_copy(update={"cell_samples": samples, "base_seconds_per_quarter": base})


def get_aligned_result_v2(piece_id: str, movement: int, take_id: str) -> AlignedResultV2 | None:
    directory = paths.take_dir(piece_id, movement, take_id, create=False)
    path = directory / "aligned.v2.json"
    if path.exists():
        # During dual-write, validate both representations so corruption of a
        # compatibility artifact is visible rather than silently masked.
        legacy_path = directory / "aligned.json"
        if legacy_path.exists():
            AlignedResult.model_validate_json(legacy_path.read_text(encoding="utf-8"))
        return AlignedResultV2.model_validate_json(path.read_text(encoding="utf-8"))
    legacy_path = directory / "aligned.json"
    if not legacy_path.exists():
        return None
    legacy = AlignedResult.model_validate_json(legacy_path.read_text(encoding="utf-8"))
    result = migrate_alignment(legacy, bundle=get_take_v2(piece_id, movement, take_id).bundle)
    if (piece_id, movement) == ("chopin_op11", 2):
        result = _canonicalize_movement2_alignment(result)
    else:
        result = _canonicalize_identity_alignment(result)
    result = _resample_performance_cells(piece_id, movement, take_id, result)
    _atomic_model(path, result)
    return result


def get_aligned_result(piece_id: str, movement: int, take_id: str) -> AlignedResult | None:
    path = paths.take_dir(piece_id, movement, take_id, create=False) / "aligned.json"
    if path.exists():
        return AlignedResult.model_validate_json(path.read_text(encoding="utf-8"))
    return None


def create_job(
    piece_id: str,
    movement: int,
    take_id: str,
    kind: JobKind,
    *,
    candidate_id: str | None = None,
    input_revision: str | None = None,
    deduplicate: bool = False,
) -> JobRecord:
    take = get_take_v2(piece_id, movement, take_id)
    selected_revision = input_revision or str(take.lifecycle_revision)
    lock = _movement_job_lock(piece_id, movement) if deduplicate else nullcontext()
    with lock:
        if deduplicate:
            _initialize_job_dedup_index(piece_id, movement)
            existing = _get_indexed_job(
                piece_id,
                movement,
                kind=kind,
                take_id=take_id,
                input_revision=selected_revision,
            )
            if existing is not None:
                return existing
        job = JobRecord(
            job_id=f"job-{uuid4().hex}",
            kind=kind,
            take_id=take_id,
            candidate_id=candidate_id,
            bundle=take.bundle,
            input_revision=selected_revision,
            requested_at=utc_now(),
        )
        _write_job(piece_id, movement, job)
        return job


def get_job(piece_id: str, movement: int, job_id: str) -> JobRecord:
    path = _jobs_dir(piece_id, movement) / f"{job_id}.json"
    if not path.exists():
        raise JobNotFoundError(job_id)
    return JobRecord.model_validate_json(path.read_text(encoding="utf-8"))


def list_jobs(piece_id: str, movement: int) -> list[JobRecord]:
    return [
        JobRecord.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(_jobs_dir(piece_id, movement).glob("job-*.json"))
    ]


def transition_stored_job(
    piece_id: str,
    movement: int,
    job_id: str,
    target: JobState,
    *,
    failure: LifecycleFailure | None = None,
) -> JobRecord:
    updated = transition_job(get_job(piece_id, movement, job_id), target, failure=failure)
    _write_job(piece_id, movement, updated)
    return updated


def _jobs_dir(piece_id: str, movement: int) -> Path:
    path = paths.take_movement_dir(piece_id, movement) / "jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_job(piece_id: str, movement: int, job: JobRecord) -> None:
    with _movement_job_lock(piece_id, movement):
        _atomic_model(_jobs_dir(piece_id, movement) / f"{job.job_id}.json", job)
        _update_job_dedup_index(piece_id, movement, job)


def _movement_job_lock(piece_id: str, movement: int) -> RLock:
    key = (piece_id, movement)
    with _JOB_LOCKS_GUARD:
        return _JOB_LOCKS.setdefault(key, RLock())


def _job_dedup_key(kind: JobKind, take_id: str | None, input_revision: str) -> str:
    scope = "movement" if kind in {JobKind.REBUILD_PROFILE, JobKind.REBUILD_COVERAGE} else take_id
    return f"{kind.value}\0{scope}\0{input_revision}"


def _job_dedup_pointer(
    piece_id: str,
    movement: int,
    *,
    kind: JobKind,
    take_id: str | None,
    input_revision: str,
) -> Path:
    digest = sha256(_job_dedup_key(kind, take_id, input_revision).encode()).hexdigest()
    return _jobs_dir(piece_id, movement) / ".dedup-v1" / f"{digest}.job"


def _initialize_job_dedup_index(piece_id: str, movement: int) -> None:
    """Build the lightweight dedup index once for stores created before it existed."""

    index_dir = _jobs_dir(piece_id, movement) / ".dedup-v1"
    ready = index_dir / ".ready"
    if ready.exists():
        return
    index_dir.mkdir(parents=True, exist_ok=True)
    for stale in index_dir.glob("*.job"):
        stale.unlink()
    indexed_keys: set[str] = set()
    for job in list_jobs(piece_id, movement):
        if job.state not in {JobState.QUEUED, JobState.RUNNING, JobState.SUCCEEDED}:
            continue
        key = _job_dedup_key(job.kind, job.take_id, job.input_revision)
        if key in indexed_keys:
            continue
        indexed_keys.add(key)
        pointer = _job_dedup_pointer(
            piece_id,
            movement,
            kind=job.kind,
            take_id=job.take_id,
            input_revision=job.input_revision,
        )
        _atomic_text(pointer, job.job_id + "\n")
    _atomic_text(ready, "1\n")


def _get_indexed_job(
    piece_id: str,
    movement: int,
    *,
    kind: JobKind,
    take_id: str,
    input_revision: str,
) -> JobRecord | None:
    pointer = _job_dedup_pointer(
        piece_id,
        movement,
        kind=kind,
        take_id=take_id,
        input_revision=input_revision,
    )
    if not pointer.exists():
        return None
    try:
        job = get_job(piece_id, movement, pointer.read_text(encoding="utf-8").strip())
    except (JobNotFoundError, ValueError):
        pointer.unlink(missing_ok=True)
        return None
    if job.state not in {JobState.QUEUED, JobState.RUNNING, JobState.SUCCEEDED}:
        pointer.unlink(missing_ok=True)
        return None
    return job


def _update_job_dedup_index(piece_id: str, movement: int, job: JobRecord) -> None:
    pointer = _job_dedup_pointer(
        piece_id,
        movement,
        kind=job.kind,
        take_id=job.take_id,
        input_revision=job.input_revision,
    )
    if job.state in {JobState.QUEUED, JobState.RUNNING, JobState.SUCCEEDED}:
        # Keep the first eligible record as the canonical match, mirroring
        # list_jobs()'s historical ordering if a non-deduplicated caller ever
        # creates a second job for the same key.
        if not pointer.exists():
            _atomic_text(pointer, job.job_id + "\n")
    elif pointer.exists() and pointer.read_text(encoding="utf-8").strip() == job.job_id:
        pointer.unlink()


def _legacy_projection(piece_id: str, movement: int, take: TakeDocV2) -> Take:
    status = (
        DISCARDED
        if take.disposition == UserDisposition.DISCARDED
        else {
            AnalysisState.CAPTURED: CAPTURED,
            AnalysisState.QUEUED: ALIGNING,
            AnalysisState.RUNNING: ALIGNING,
            AnalysisState.ALIGNED: ALIGNED,
            AnalysisState.AMBIGUOUS: AMBIGUOUS,
            AnalysisState.UNALIGNABLE: UNALIGNABLE,
            AnalysisState.FAILED: UNALIGNABLE,
        }[take.analysis_state]
    )
    cue = None
    if take.cue is not None:
        cue = TakeCue(
            kind=take.cue.kind,
            target_beat=take.cue.target_score_tick / CANONICAL_PPQ,
            cue_start_beat=(
                None
                if take.cue.cue_start_score_tick is None
                else take.cue.cue_start_score_tick / CANONICAL_PPQ
            ),
            cue_seconds=take.cue.cue_seconds,
            output_name=take.cue.output_name,
        )
    placement_hint = None
    if take.placement_hint is not None:
        placement_hint = TakePlacementHint(
            target_beat=take.placement_hint.target_score_tick / CANONICAL_PPQ,
            source=take.placement_hint.source,
        )
    return Take(
        take_id=take.take_id,
        piece_id=piece_id,
        movement=movement,
        recorded_at=take.recorded_at,
        duration_seconds=take.duration_seconds,
        note_on_count=take.note_on_count,
        input_name=take.input_name,
        cue=cue,
        placement_hint=placement_hint,
        status=status,
        midi_path=take.midi_path,
    )


def _write_take_v2_and_legacy(piece_id: str, movement: int, take: TakeDocV2) -> None:
    directory = paths.take_dir(piece_id, movement, take.take_id, create=True)
    _atomic_model(directory / "take.v2.json", take)
    _atomic_model(directory / "take.json", _legacy_projection(piece_id, movement, take))


def _anchor_path(piece_id: str, movement: int) -> Path:
    return paths.data_root() / "profiles" / piece_id / str(movement) / "anchors.json"


def load_anchor_set(piece_id: str, movement: int) -> AnchorSet:
    """Read the piece/movement's structural beat anchors (empty if none yet)."""

    path = _anchor_path(piece_id, movement)
    if not path.exists():
        return AnchorSet(piece_id=piece_id, movement=movement, updated=utc_now())
    return AnchorSet.model_validate_json(path.read_text(encoding="utf-8"))


def save_anchor_set(anchors: AnchorSet) -> None:
    _atomic_model(_anchor_path(anchors.piece_id, anchors.movement), anchors)


def add_anchor(
    piece_id: str, movement: int, *, score_tick: int, measure: int, label: str = ""
) -> AnchorSet:
    """Add (or replace) the anchor at ``score_tick``; idempotent on the tick key."""

    with _movement_job_lock(piece_id, movement):
        current = load_anchor_set(piece_id, movement)
        kept = tuple(anchor for anchor in current.anchors if anchor.score_tick != score_tick)
        updated = AnchorSet(
            piece_id=piece_id,
            movement=movement,
            updated=utc_now(),
            anchors=tuple(
                sorted(
                    (*kept, Anchor(score_tick=score_tick, measure=measure, label=label)),
                    key=lambda anchor: anchor.score_tick,
                )
            ),
        )
        save_anchor_set(updated)
        return updated


def remove_anchor(piece_id: str, movement: int, *, score_tick: int) -> AnchorSet:
    """Delete the anchor at ``score_tick`` (no-op if absent)."""

    with _movement_job_lock(piece_id, movement):
        current = load_anchor_set(piece_id, movement)
        updated = AnchorSet(
            piece_id=piece_id,
            movement=movement,
            updated=utc_now(),
            anchors=tuple(a for a in current.anchors if a.score_tick != score_tick),
        )
        save_anchor_set(updated)
        return updated


def move_anchor(
    piece_id: str,
    movement: int,
    *,
    score_tick: int,
    new_score_tick: int,
    measure: int,
    label: str = "",
) -> AnchorSet:
    """Atomically relocate an existing anchor and retain tick-key uniqueness."""

    with _movement_job_lock(piece_id, movement):
        current = load_anchor_set(piece_id, movement)
        original = next(
            (anchor for anchor in current.anchors if anchor.score_tick == score_tick),
            None,
        )
        if original is None:
            raise AnchorNotFoundError(f"anchor at score tick {score_tick} does not exist")
        if new_score_tick != score_tick and any(
            anchor.score_tick == new_score_tick for anchor in current.anchors
        ):
            raise AnchorConflictError(f"anchor already exists at score tick {new_score_tick}")
        kept = tuple(anchor for anchor in current.anchors if anchor.score_tick != score_tick)
        updated = AnchorSet(
            piece_id=piece_id,
            movement=movement,
            updated=utc_now(),
            anchors=tuple(
                sorted(
                    (
                        *kept,
                        Anchor(
                            score_tick=new_score_tick,
                            measure=measure,
                            label=label or original.label,
                        ),
                    ),
                    key=lambda anchor: anchor.score_tick,
                )
            ),
        )
        save_anchor_set(updated)
        return updated


def remove_anchors_in_measure(piece_id: str, movement: int, *, measure: int) -> AnchorSet:
    """Atomically clear the anchors displayed in one printed measure."""

    with _movement_job_lock(piece_id, movement):
        current = load_anchor_set(piece_id, movement)
        updated = AnchorSet(
            piece_id=piece_id,
            movement=movement,
            updated=utc_now(),
            anchors=tuple(anchor for anchor in current.anchors if anchor.measure != measure),
        )
        save_anchor_set(updated)
        return updated


def restore_anchors(
    piece_id: str,
    movement: int,
    *,
    anchors: tuple[Anchor, ...],
) -> AnchorSet:
    """Atomically merge anchors back into the durable set for undo."""

    with _movement_job_lock(piece_id, movement):
        current = load_anchor_set(piece_id, movement)
        restored_ticks = {anchor.score_tick for anchor in anchors}
        updated = AnchorSet(
            piece_id=piece_id,
            movement=movement,
            updated=utc_now(),
            anchors=tuple(
                sorted(
                    (
                        *(
                            anchor
                            for anchor in current.anchors
                            if anchor.score_tick not in restored_ticks
                        ),
                        *anchors,
                    ),
                    key=lambda anchor: anchor.score_tick,
                )
            ),
        )
        save_anchor_set(updated)
        return updated


def _atomic_model(path: Path, model: object) -> None:
    content = model.model_dump_json(indent=2) + "\n"  # type: ignore[attr-defined]
    _atomic_text(path, content)


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def _append_manifest(piece_id: str, movement: int, take: Take) -> None:
    with paths.take_manifest_path(piece_id, movement, create=True).open(
        "a", encoding="utf-8"
    ) as handle:
        handle.write(take.model_dump_json() + "\n")


def _midi_duration_seconds(path: Path) -> float:
    try:
        return float(MidiFile(path, clip=True).length)
    except (OSError, EOFError, ValueError) as exc:
        logging.warning("Could not compute MIDI duration for %s: %s", path, exc)
        return 0.0


__all__ = [
    "CAPTURED",
    "ALIGNING",
    "ALIGNED",
    "AMBIGUOUS",
    "UNALIGNABLE",
    "FAILED",
    "DISCARDED",
    "TAKE_MIDI_FILENAME",
    "Take",
    "TakeNotFoundError",
    "JobNotFoundError",
    "AnchorNotFoundError",
    "AnchorConflictError",
    "generate_take_id",
    "save_take",
    "get_take",
    "get_take_v2",
    "list_takes",
    "list_takes_v2",
    "transition_take_analysis",
    "update_take_status",
    "load_anchor_set",
    "save_anchor_set",
    "add_anchor",
    "move_anchor",
    "remove_anchor",
    "remove_anchors_in_measure",
    "restore_anchors",
    "discard_take",
    "restore_take",
    "set_take_profile_membership",
    "write_aligned_result",
    "get_aligned_result",
    "get_aligned_result_v2",
    "create_job",
    "get_job",
    "list_jobs",
    "transition_stored_job",
]
