from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient
from mido import Message, MidiFile, MidiTrack

from aimusic.core import paths
from aimusic.server.app import create_app
from aimusic.takes import aligner, coverage, profile, store
from aimusic.takes.lifecycle import (
    AnalysisState,
    JobKind,
    JobState,
    LifecycleFailure,
    ProfileMembership,
    UserDisposition,
)
from aimusic.takes.materializer import MaterializationWorker
from aimusic.takes.models import AlignedResult, CellSample, TimingMapPoint


from tests.oguri_guard import requires_oguri_derived

@pytest.fixture(autouse=True)
def isolated_data_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))


def _midi(path: Path) -> None:
    midi = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    track.append(Message("note_on", note=60, velocity=64, time=0))
    track.append(Message("note_off", note=60, velocity=0, time=240))
    midi.tracks.append(track)
    midi.save(path)


def _take(tmp_path: Path, movement: int = 2):
    source = tmp_path / "take.mid"
    _midi(source)
    return store.save_take("chopin_op11", movement, source)


def test_discard_during_alignment_preserves_analysis_and_restore_is_explicit(
    tmp_path: Path,
) -> None:
    take = _take(tmp_path)
    store.transition_take_analysis("chopin_op11", 2, take.take_id, AnalysisState.QUEUED)
    store.transition_take_analysis("chopin_op11", 2, take.take_id, AnalysisState.RUNNING)
    store.discard_take("chopin_op11", 2, take.take_id)
    store.transition_take_analysis(
        "chopin_op11",
        2,
        take.take_id,
        AnalysisState.ALIGNED,
        alignment_artifact="aligned.v2.json",
    )

    discarded = store.get_take_v2("chopin_op11", 2, take.take_id)
    assert discarded.analysis_state == AnalysisState.ALIGNED
    assert discarded.disposition == UserDisposition.DISCARDED
    assert discarded.profile_membership == ProfileMembership.EXCLUDED

    store.restore_take("chopin_op11", 2, take.take_id)
    restored = store.get_take_v2("chopin_op11", 2, take.take_id)
    assert restored.analysis_state == AnalysisState.ALIGNED
    assert restored.disposition == UserDisposition.KEPT
    assert restored.profile_membership == ProfileMembership.EXCLUDED


def test_stale_running_job_is_requeued_on_recovery(tmp_path: Path) -> None:
    take = _take(tmp_path)
    store.transition_take_analysis("chopin_op11", 2, take.take_id, AnalysisState.QUEUED)
    job = store.create_job("chopin_op11", 2, take.take_id, JobKind.ALIGN)
    store.transition_stored_job("chopin_op11", 2, job.job_id, JobState.RUNNING)

    worker = aligner.AlignmentWorker()
    submitted: list[tuple[object, ...]] = []
    worker._executor.shutdown(wait=True)
    worker._executor = type(
        "FakeExecutor", (), {"submit": lambda self, *args: submitted.append(args)}
    )()

    assert worker.recover("chopin_op11", 2) == 1
    recovered = store.get_job("chopin_op11", 2, job.job_id)
    assert recovered.state == JobState.QUEUED
    assert recovered.attempt == 1
    assert submitted and submitted[0][-1] == job.job_id


def test_coverage_get_is_read_only_when_cache_is_missing(tmp_path: Path) -> None:
    client = TestClient(create_app())
    profile_dir = tmp_path / "data" / "profiles" / "chopin_op11" / "1"
    response = client.get("/api/coverage/1?piece_id=chopin_op11")
    assert response.status_code == 200
    assert response.json()["measures"]
    assert not profile_dir.exists()


def test_materialization_revisions_advance(tmp_path: Path) -> None:
    first = coverage.compute_coverage("chopin_op11", 1)
    second = coverage.compute_coverage("chopin_op11", 1)
    assert first.revision == 1
    assert first.profile_revision == 1
    assert second.revision == 1
    assert second.profile_revision == 1


def test_recovery_rebuilds_stale_coverage_algorithm_without_new_take(
    tmp_path: Path,
) -> None:
    _take(tmp_path, movement=1)
    current = coverage.compute_coverage("chopin_op11", 1)
    coverage_path = (
        paths.data_root() / "profiles" / "chopin_op11" / "1" / "coverage.json"
    )
    stale = current.model_copy(update={"algorithm_revision": "coverage-v1"})
    coverage_path.write_text(stale.model_dump_json(indent=2) + "\n", encoding="utf-8")

    worker = MaterializationWorker()
    submitted: list[tuple[object, ...]] = []
    worker._executor.shutdown(wait=True)
    worker._executor = type(
        "FakeExecutor", (), {"submit": lambda self, *args: submitted.append(args)}
    )()

    assert worker.recover("chopin_op11", 1) == 1
    assert len(submitted) == 1
    _handler, piece_id, movement, job_id = submitted[0]
    worker._run_coverage(piece_id, movement, job_id)

    refreshed = coverage.get_cached_coverage("chopin_op11", 1)
    assert refreshed.algorithm_revision == coverage.COVERAGE_ALGORITHM_REVISION


def test_recovery_treats_corrupt_profile_cache_as_stale(tmp_path: Path) -> None:
    take = _take(tmp_path, movement=1)
    store.update_take_status("chopin_op11", 1, take.take_id, store.ALIGNED)
    profile_path = (
        paths.data_root() / "profiles" / "chopin_op11" / "1" / "profile.json"
    )
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text("{not valid json", encoding="utf-8")

    worker = MaterializationWorker()
    submitted: list[tuple[object, ...]] = []
    worker._executor.shutdown(wait=True)
    worker._executor = type(
        "FakeExecutor", (), {"submit": lambda self, *args: submitted.append(args)}
    )()

    assert worker.recover("chopin_op11", 1) == 1
    assert len(submitted) == 1
    handler, piece_id, movement, _job_id = submitted[0]
    assert handler == worker._run_profile
    assert (piece_id, movement) == ("chopin_op11", 1)


def test_recovery_rebuilds_corrupt_coverage_cache(tmp_path: Path) -> None:
    _take(tmp_path, movement=1)
    current = coverage.compute_coverage("chopin_op11", 1)
    coverage_path = (
        paths.data_root() / "profiles" / "chopin_op11" / "1" / "coverage.json"
    )
    coverage_path.write_text("{not valid json", encoding="utf-8")

    worker = MaterializationWorker()
    submitted: list[tuple[object, ...]] = []
    worker._executor.shutdown(wait=True)
    worker._executor = type(
        "FakeExecutor", (), {"submit": lambda self, *args: submitted.append(args)}
    )()

    assert worker.recover("chopin_op11", 1) == 1
    assert len(submitted) == 1
    handler, piece_id, movement, job_id = submitted[0]
    assert handler == worker._run_coverage
    assert (piece_id, movement) == ("chopin_op11", 1)
    worker._run_coverage(piece_id, movement, job_id)
    refreshed = coverage.get_cached_coverage("chopin_op11", 1)
    assert refreshed.algorithm_revision == current.algorithm_revision


@requires_oguri_derived
def test_recovery_rebuilds_stale_empty_profile_before_movement2_coverage(
    tmp_path: Path,
) -> None:
    """Regression for the real take upgrade: lifecycle truth outranks cache.

    The take is already aligned/kept/included, while profile.json still says
    ``input_revision=empty``.  Even a coverage.json written by the newest
    algorithm is stale in that state and must not suppress the profile fold.
    """

    take = _take(tmp_path, movement=2)
    samples = tuple(
        CellSample(
            beat=index / 2,
            period_s=0.5,
            velocity=72,
            pedal=0.0,
            quality=0.86,
        )
        for index in range(round(144.5 * 2), round(232.0 * 2) + 1)
    )
    store.write_aligned_result(
        "chopin_op11",
        2,
        take.take_id,
        AlignedResult(
            take_id=take.take_id,
            aligner="real-smoke-fixture",
            score_start_beat=144.245,
            score_end_beat=232.387,
            match_rate=0.86,
            ambiguous=False,
            matched_notes=97,
            extra_notes=10,
            missing_notes=6,
            timing_map=(
                TimingMapPoint(score_beat=144.245, take_seconds=8.0),
                TimingMapPoint(score_beat=232.387, take_seconds=32.0),
            ),
            candidates=(),
            cell_samples=samples,
            edge_trim_beats=(1.0, 0.5),
        ),
    )
    store.update_take_status("chopin_op11", 2, take.take_id, store.ALIGNED)
    expected_input = profile.active_take_input_revision("chopin_op11", 2)
    assert expected_input != "empty"
    historical_profile_job = store.create_job(
        "chopin_op11",
        2,
        take.take_id,
        JobKind.REBUILD_PROFILE,
        input_revision=expected_input,
    )
    store.transition_stored_job(
        "chopin_op11", 2, historical_profile_job.job_id, JobState.RUNNING
    )
    store.transition_stored_job(
        "chopin_op11", 2, historical_profile_job.job_id, JobState.SUCCEEDED
    )

    profile_path = (
        paths.data_root() / "profiles" / "chopin_op11" / "2" / "profile.json"
    )
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    stale_profile = profile.Interpretation(
        piece_id="chopin_op11",
        movement=2,
        take_count=0,
        updated=datetime.now(timezone.utc),
        cells=(),
        revision=3,
        input_revision="empty",
        included_take_ids=(),
    )
    profile_path.write_text(
        stale_profile.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    stale_coverage = coverage.materialize_coverage("chopin_op11", 2)
    assert stale_coverage.algorithm_revision == coverage.COVERAGE_ALGORITHM_REVISION
    assert stale_coverage.summary.percent_observed == 0.0
    stale_coverage_job = store.create_job(
        "chopin_op11",
        2,
        take.take_id,
        JobKind.REBUILD_COVERAGE,
        input_revision=(
            f"coverage:{coverage.COVERAGE_ALGORITHM_REVISION}:profile:3"
        ),
    )

    worker = MaterializationWorker()
    assert worker.recover("chopin_op11", 2) == 1
    assert (
        store.get_job("chopin_op11", 2, stale_coverage_job.job_id).state
        == JobState.CANCELLED
    )
    repair_jobs = [
        job
        for job in store.list_jobs("chopin_op11", 2)
        if job.kind == JobKind.REBUILD_PROFILE
        and job.input_revision == expected_input
    ]
    assert len(repair_jobs) == 2
    repair_job_ids = {job.job_id for job in repair_jobs}
    assert historical_profile_job.job_id in repair_job_ids
    assert repair_job_ids - {historical_profile_job.job_id}

    deadline = monotonic() + 5.0
    while monotonic() < deadline:
        refreshed_profile = profile.Interpretation.model_validate_json(
            profile_path.read_text(encoding="utf-8")
        )
        refreshed_coverage = coverage.get_cached_coverage("chopin_op11", 2)
        if (
            refreshed_profile.input_revision == expected_input
            and refreshed_profile.included_take_ids == (take.take_id,)
            and refreshed_coverage.profile_revision == refreshed_profile.revision
            and refreshed_coverage.summary.percent_observed > 0
        ):
            break
        sleep(0.01)
    else:
        raise AssertionError("startup recovery did not rebuild profile then coverage")

    observed = [measure for measure in refreshed_coverage.measures if measure.observed]
    assert observed[0].measure == 12
    assert observed[-1].measure == 22


def test_profile_and_coverage_jobs_are_persisted_and_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    take = _take(tmp_path, movement=1)
    worker = MaterializationWorker()
    submitted: list[tuple[object, ...]] = []
    worker._executor.shutdown(wait=True)
    worker._executor = type(
        "FakeExecutor", (), {"submit": lambda self, *args: submitted.append(args)}
    )()
    published: list[object] = []
    monkeypatch.setattr("aimusic.takes.materializer.events.publish", published.append)

    profile_job = worker.enqueue("chopin_op11", 1, take.take_id)
    assert profile_job.kind == JobKind.REBUILD_PROFILE
    worker._run_profile("chopin_op11", 1, profile_job.job_id)
    assert store.get_job("chopin_op11", 1, profile_job.job_id).state == JobState.SUCCEEDED
    coverage_jobs = [
        job for job in store.list_jobs("chopin_op11", 1) if job.kind == JobKind.REBUILD_COVERAGE
    ]
    assert len(coverage_jobs) == 1
    worker._run_coverage("chopin_op11", 1, coverage_jobs[0].job_id)
    assert store.get_job("chopin_op11", 1, coverage_jobs[0].job_id).state == JobState.SUCCEEDED
    assert coverage.get_cached_coverage("chopin_op11", 1).profile_revision == 1
    assert [event.model_dump(mode="json") for event in published] == [
        {
            "type": "coverage:materialized",
            "piece_id": "chopin_op11",
            "movement": 1,
            "revision": 1,
        }
    ]

    repeated = worker.enqueue("chopin_op11", 1, take.take_id)
    assert repeated.job_id == profile_job.job_id


def test_materialization_enqueue_does_not_fold_alignment_payloads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    take = _take(tmp_path, movement=1)
    worker = MaterializationWorker()
    submitted: list[tuple[object, ...]] = []
    worker._executor.shutdown(wait=True)
    worker._executor = type(
        "FakeExecutor", (), {"submit": lambda self, *args: submitted.append(args)}
    )()
    monkeypatch.setattr(
        profile,
        "fit_interpretation_for",
        lambda *_args: pytest.fail("enqueue must not perform the profile fold"),
    )

    job = worker.enqueue("chopin_op11", 1, take.take_id)

    assert job.input_revision == "empty"
    assert submitted and submitted[0][-1] == job.job_id


def test_job_dedup_index_scans_history_once_and_preserves_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    take = _take(tmp_path)
    historical = store.create_job(
        "chopin_op11",
        2,
        take.take_id,
        JobKind.REBUILD_PROFILE,
        input_revision="takes:r1",
    )
    original_list_jobs = store.list_jobs
    history_scans = 0

    def counted_list_jobs(piece_id: str, movement: int):
        nonlocal history_scans
        history_scans += 1
        return original_list_jobs(piece_id, movement)

    monkeypatch.setattr(store, "list_jobs", counted_list_jobs)
    first = store.create_job(
        "chopin_op11",
        2,
        take.take_id,
        JobKind.REBUILD_PROFILE,
        input_revision="takes:r1",
        deduplicate=True,
    )
    repeated = store.create_job(
        "chopin_op11",
        2,
        take.take_id,
        JobKind.REBUILD_PROFILE,
        input_revision="takes:r1",
        deduplicate=True,
    )
    second = store.create_job(
        "chopin_op11",
        2,
        take.take_id,
        JobKind.REBUILD_PROFILE,
        input_revision="takes:r2",
        deduplicate=True,
    )

    assert first.job_id == historical.job_id
    assert repeated.job_id == historical.job_id
    assert second.job_id != first.job_id
    assert history_scans == 1
    assert {job.job_id for job in original_list_jobs("chopin_op11", 2)} == {
        historical.job_id,
        second.job_id,
    }


def test_failed_indexed_job_can_be_replaced_without_scanning_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    take = _take(tmp_path)
    failed = store.create_job(
        "chopin_op11",
        2,
        take.take_id,
        JobKind.REBUILD_PROFILE,
        input_revision="takes:r1",
        deduplicate=True,
    )
    store.transition_stored_job("chopin_op11", 2, failed.job_id, JobState.RUNNING)
    store.transition_stored_job(
        "chopin_op11",
        2,
        failed.job_id,
        JobState.FAILED,
        failure=LifecycleFailure(code="test", message="retry me", retryable=True),
    )
    monkeypatch.setattr(
        store,
        "list_jobs",
        lambda *_args: pytest.fail("initialized index must avoid full history scans"),
    )

    replacement = store.create_job(
        "chopin_op11",
        2,
        take.take_id,
        JobKind.REBUILD_PROFILE,
        input_revision="takes:r1",
        deduplicate=True,
    )

    assert replacement.job_id != failed.job_id


def test_concurrent_job_creation_deduplicates_one_movement_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    take = _take(tmp_path)
    workers = 8
    start = Barrier(workers)
    original_get_indexed_job = store._get_indexed_job

    def delayed_lookup(*args: object, **kwargs: object):
        existing = original_get_indexed_job(*args, **kwargs)
        if existing is None:
            sleep(0.02)
        return existing

    monkeypatch.setattr(store, "_get_indexed_job", delayed_lookup)

    def create() -> str:
        start.wait(timeout=2)
        return store.create_job(
            "chopin_op11",
            2,
            take.take_id,
            JobKind.REBUILD_PROFILE,
            input_revision="takes:r1",
            deduplicate=True,
        ).job_id

    with ThreadPoolExecutor(max_workers=workers) as executor:
        job_ids = list(executor.map(lambda _index: create(), range(workers)))

    assert len(set(job_ids)) == 1
    assert len(store.list_jobs("chopin_op11", 2)) == 1


def test_materialization_worker_recovers_stale_running_job(tmp_path: Path) -> None:
    take = _take(tmp_path)
    job = store.create_job(
        "chopin_op11", 2, take.take_id, JobKind.REBUILD_PROFILE, input_revision="empty"
    )
    store.transition_stored_job("chopin_op11", 2, job.job_id, JobState.RUNNING)
    worker = MaterializationWorker()
    submitted: list[tuple[object, ...]] = []
    worker._executor.shutdown(wait=True)
    worker._executor = type(
        "FakeExecutor", (), {"submit": lambda self, *args: submitted.append(args)}
    )()

    assert worker.recover("chopin_op11", 2) == 1
    recovered = store.get_job("chopin_op11", 2, job.job_id)
    assert recovered.state == JobState.QUEUED
    assert recovered.attempt == 1
    assert submitted and submitted[0][-1] == job.job_id


@pytest.mark.parametrize("initial_state", [JobState.QUEUED, JobState.RUNNING])
def test_materialization_recovery_cancels_job_for_deleted_take(
    tmp_path: Path, initial_state: JobState
) -> None:
    take = _take(tmp_path)
    job = store.create_job(
        "chopin_op11", 2, take.take_id, JobKind.REBUILD_PROFILE, input_revision="orphan"
    )
    if initial_state == JobState.RUNNING:
        store.transition_stored_job("chopin_op11", 2, job.job_id, JobState.RUNNING)
    take_dir = paths.take_dir("chopin_op11", 2, take.take_id, create=False)
    for artifact in take_dir.iterdir():
        artifact.unlink()
    take_dir.rmdir()

    worker = MaterializationWorker()
    submitted: list[tuple[object, ...]] = []
    worker._executor.shutdown(wait=True)
    worker._executor = type(
        "FakeExecutor", (), {"submit": lambda self, *args: submitted.append(args)}
    )()

    assert worker.recover("chopin_op11", 2) == 0
    assert store.get_job("chopin_op11", 2, job.job_id).state == JobState.CANCELLED
    assert submitted == []
