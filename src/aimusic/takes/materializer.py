"""Durable write-side profile and coverage materialization jobs."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aimusic.core import paths
from aimusic.core.event_journal import journal_exception
from aimusic.core.events import events
from aimusic.server.schemas import CoverageMaterialized
from aimusic.takes import coverage, profile, store
from aimusic.takes.lifecycle import JobKind, JobRecord, JobState, LifecycleFailure
from aimusic.takes.models import CoverageDoc


class MaterializationWorker:
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="take-materialize")

    def enqueue(self, piece_id: str, movement: int, take_id: str) -> JobRecord:
        input_revision = profile.active_take_input_revision(piece_id, movement)
        job = store.create_job(
            piece_id,
            movement,
            take_id,
            JobKind.REBUILD_PROFILE,
            input_revision=input_revision,
            deduplicate=True,
        )
        if job.state == JobState.QUEUED:
            self._executor.submit(self._run_profile, piece_id, movement, job.job_id)
        return job

    def _run_profile(self, piece_id: str, movement: int, job_id: str) -> None:
        job = store.get_job(piece_id, movement, job_id)
        if job.state == JobState.SUCCEEDED:
            return
        try:
            if job.state == JobState.QUEUED:
                store.transition_stored_job(piece_id, movement, job_id, JobState.RUNNING)
            current_revision = profile.active_take_input_revision(piece_id, movement)
            if current_revision != job.input_revision:
                # The take set changed while this job waited. Complete it as
                # superseded without writing stale output, then enqueue the
                # current revision through the normal deduplication path.
                store.transition_stored_job(piece_id, movement, job_id, JobState.SUCCEEDED)
                if job.take_id is not None:
                    self.enqueue(piece_id, movement, job.take_id)
                return
            profile_doc = coverage.materialize_profile(piece_id, movement)
            store.transition_stored_job(piece_id, movement, job_id, JobState.SUCCEEDED)
            if job.take_id is not None:
                coverage_revision = _coverage_input_revision(profile_doc)
                coverage_job = store.create_job(
                    piece_id,
                    movement,
                    job.take_id,
                    JobKind.REBUILD_COVERAGE,
                    input_revision=coverage_revision,
                    deduplicate=True,
                )
                if coverage_job.state == JobState.QUEUED:
                    self._executor.submit(
                        self._run_coverage, piece_id, movement, coverage_job.job_id
                    )
        except Exception as exc:
            self._fail_running(piece_id, movement, job_id, "profile_rebuild_failed", exc)

    def _run_coverage(self, piece_id: str, movement: int, job_id: str) -> None:
        job = store.get_job(piece_id, movement, job_id)
        if job.state == JobState.SUCCEEDED:
            return
        try:
            if job.state == JobState.QUEUED:
                store.transition_stored_job(piece_id, movement, job_id, JobState.RUNNING)
            profile_path = (
                paths.data_root() / "profiles" / piece_id / str(movement) / "profile.json"
            )
            if profile_path.exists():
                current = profile.Interpretation.model_validate_json(
                    profile_path.read_text(encoding="utf-8")
                )
                current_revision = _coverage_input_revision(current)
                if job.input_revision != current_revision:
                    store.transition_stored_job(piece_id, movement, job_id, JobState.SUCCEEDED)
                    if job.take_id is not None:
                        replacement = store.create_job(
                            piece_id,
                            movement,
                            job.take_id,
                            JobKind.REBUILD_COVERAGE,
                            input_revision=current_revision,
                            deduplicate=True,
                        )
                        if replacement.state == JobState.QUEUED:
                            self._executor.submit(
                                self._run_coverage,
                                piece_id,
                                movement,
                                replacement.job_id,
                            )
                    return
            coverage_doc = coverage.materialize_coverage(piece_id, movement)
            store.transition_stored_job(piece_id, movement, job_id, JobState.SUCCEEDED)
            events.publish(
                CoverageMaterialized(
                    type="coverage:materialized",
                    piece_id=piece_id,
                    movement=movement,
                    revision=coverage_doc.revision,
                )
            )
        except Exception as exc:
            self._fail_running(piece_id, movement, job_id, "coverage_rebuild_failed", exc)

    def _fail_running(
        self, piece_id: str, movement: int, job_id: str, code: str, exc: Exception
    ) -> None:
        logging.getLogger(__name__).exception("Materialization job %s failed", job_id)
        job = store.get_job(piece_id, movement, job_id)
        journal_exception(
            f"take:{code}",
            exc,
            piece_id=piece_id,
            movement=movement,
            take_id=job.take_id,
            job_id=job_id,
        )
        if job.state == JobState.RUNNING:
            store.transition_stored_job(
                piece_id,
                movement,
                job_id,
                JobState.FAILED,
                failure=LifecycleFailure(code=code, message=str(exc), retryable=True),
            )

    def recover(self, piece_id: str, movement: int) -> int:
        queued: list[JobRecord] = []
        for job in store.list_jobs(piece_id, movement):
            if job.kind not in {JobKind.REBUILD_PROFILE, JobKind.REBUILD_COVERAGE}:
                continue
            if job.take_id is not None:
                take_dir = paths.take_dir(piece_id, movement, job.take_id, create=False)
                if not (take_dir / "take.v2.json").exists() and not (
                    take_dir / "take.json"
                ).exists():
                    if job.state in {JobState.QUEUED, JobState.RUNNING}:
                        store.transition_stored_job(
                            piece_id,
                            movement,
                            job.job_id,
                            JobState.CANCELLED,
                        )
                    logging.getLogger(__name__).info(
                        "Cancelled orphaned materialization job %s for missing take %s",
                        job.job_id,
                        job.take_id,
                    )
                    continue
            if job.state == JobState.RUNNING:
                job = store.transition_stored_job(
                    piece_id,
                    movement,
                    job.job_id,
                    JobState.FAILED,
                    failure=LifecycleFailure(
                        code="worker_restarted",
                        message="worker restarted while materializing",
                        retryable=True,
                    ),
                )
                job = store.transition_stored_job(piece_id, movement, job.job_id, JobState.QUEUED)
            if job.state != JobState.QUEUED:
                continue
            queued.append(job)

        current_profile_revision = profile.active_take_input_revision(piece_id, movement)
        profile_path = _profile_path(piece_id, movement)
        try:
            cached_profile = (
                profile.Interpretation.model_validate_json(
                    profile_path.read_text(encoding="utf-8")
                )
                if profile_path.is_file()
                else None
            )
        except (OSError, ValueError) as exc:
            logging.getLogger(__name__).warning(
                "Ignoring invalid cached profile %s during recovery: %s",
                profile_path,
                exc,
            )
            cached_profile = None
        profile_stale = (
            current_profile_revision != "empty"
            and (
                cached_profile is None
                or cached_profile.input_revision != current_profile_revision
            )
        )

        if profile_stale:
            # Coverage derived from an obsolete profile is necessarily stale,
            # even when its own algorithm/profile_revision fields look current.
            # Cancel every queued coverage fold so none can run before the
            # single-threaded profile rebuild; _run_profile will enqueue the
            # correct coverage revision after the new profile is durable.
            for job in queued:
                if job.kind == JobKind.REBUILD_COVERAGE or (
                    job.kind == JobKind.REBUILD_PROFILE
                    and job.input_revision != current_profile_revision
                ):
                    store.transition_stored_job(
                        piece_id, movement, job.job_id, JobState.CANCELLED
                    )

            active_take_ids = profile.active_take_ids(piece_id, movement)
            if not active_take_ids:  # pragma: no cover - guarded by non-empty revision
                return 0
            # Artifact evidence overrides terminal job history: an older
            # SUCCEEDED job with this same lifecycle input cannot make a stale
            # or missing profile current.  Reuse only live matching repair
            # work; otherwise create a fresh job without terminal dedup.
            profile_job = next(
                (
                    job
                    for job in queued
                    if job.kind == JobKind.REBUILD_PROFILE
                    and job.input_revision == current_profile_revision
                    and job.state == JobState.QUEUED
                ),
                None,
            )
            if profile_job is None:
                profile_job = store.create_job(
                    piece_id,
                    movement,
                    active_take_ids[-1],
                    JobKind.REBUILD_PROFILE,
                    input_revision=current_profile_revision,
                    deduplicate=False,
                )
            if profile_job.state != JobState.QUEUED:
                return 0
            self._executor.submit(
                self._run_profile, piece_id, movement, profile_job.job_id
            )
            return 1

        recovered = 0
        for job in queued:
            handler = (
                self._run_profile if job.kind == JobKind.REBUILD_PROFILE else self._run_coverage
            )
            self._executor.submit(handler, piece_id, movement, job.job_id)
            recovered += 1
        recovered += self._refresh_stale_coverage(piece_id, movement)
        return recovered

    def _refresh_stale_coverage(self, piece_id: str, movement: int) -> int:
        """Queue a new-algorithm coverage fold for an existing cached profile.

        This startup audit is what upgrades a completed rehearsal immediately:
        a user must not record another take merely to invalidate coverage.json.
        The algorithm revision is part of the durable job input identity, so an
        old succeeded coverage job cannot deduplicate the repair away.
        """

        profile_path = _profile_path(piece_id, movement)
        if not profile_path.is_file():
            return 0
        try:
            profile_doc = profile.Interpretation.model_validate_json(
                profile_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            logging.getLogger(__name__).warning(
                "Ignoring invalid cached profile %s during coverage recovery: %s",
                profile_path,
                exc,
            )
            return 0
        coverage_path = profile_path.with_name("coverage.json")
        if coverage_path.is_file():
            try:
                cached = CoverageDoc.model_validate_json(
                    coverage_path.read_text(encoding="utf-8")
                )
                if (
                    cached.algorithm_revision == coverage.COVERAGE_ALGORITHM_REVISION
                    and cached.profile_revision == profile_doc.revision
                ):
                    return 0
            except (OSError, ValueError) as exc:
                logging.getLogger(__name__).warning(
                    "Ignoring invalid cached coverage %s during recovery: %s",
                    coverage_path,
                    exc,
                )

        if profile_doc.included_take_ids:
            take_id = profile_doc.included_take_ids[-1]
        else:
            existing_takes = store.list_takes(piece_id, movement)
            if not existing_takes:
                return 0
            take_id = existing_takes[-1].take_id
        input_revision = _coverage_input_revision(profile_doc)
        live_job = next(
            (
                job
                for job in store.list_jobs(piece_id, movement)
                if job.kind == JobKind.REBUILD_COVERAGE
                and job.input_revision == input_revision
                and job.state in {JobState.QUEUED, JobState.RUNNING}
            ),
            None,
        )
        if live_job is not None:
            return 0
        # Artifact evidence wins over terminal job history: a corrupt or
        # missing coverage file must be repairable even if an older job with
        # the same profile revision previously succeeded.
        job = store.create_job(
            piece_id,
            movement,
            take_id,
            JobKind.REBUILD_COVERAGE,
            input_revision=input_revision,
            deduplicate=False,
        )
        if job.state != JobState.QUEUED:
            return 0
        self._executor.submit(self._run_coverage, piece_id, movement, job.job_id)
        return 1

    def recover_all(self) -> int:
        recovered = 0
        for piece_dir in paths.takes_root().iterdir():
            if not piece_dir.is_dir():
                continue
            for movement_dir in piece_dir.iterdir():
                if not movement_dir.is_dir():
                    continue
                try:
                    movement = int(movement_dir.name)
                except ValueError:
                    continue
                recovered += self.recover(piece_dir.name, movement)
        return recovered


materialization_worker = MaterializationWorker()


def _profile_path(piece_id: str, movement: int) -> Path:
    return paths.data_root() / "profiles" / piece_id / str(movement) / "profile.json"


def _coverage_input_revision(profile_doc: profile.Interpretation) -> str:
    return (
        f"coverage:{coverage.COVERAGE_ALGORITHM_REVISION}:"
        f"profile:{profile_doc.revision}"
    )

__all__ = ["MaterializationWorker", "materialization_worker"]
