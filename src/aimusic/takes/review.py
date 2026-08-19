"""Durable take-owned accompanied-review rendering.

The rehearsal take is the aggregate: review rendering consumes its authoritative
``take.mid`` and ``aligned.v2.json`` instead of relying on the legacy coincidence
that hardware capture also left a session-shaped copy under ``processed/``.
"""

from __future__ import annotations

import hashlib
import logging
import statistics
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

from mido import MidiFile, tick2second

from aimusic.accompaniment.offline_alignment import (
    PiecewiseLinearTimingMap,
    TimingAnchor,
    retime_midi,
)
from aimusic.accompaniment.offline_render import combine_review_midi
from aimusic.accompaniment.oguri import oguri_movement
from aimusic.core import paths
from aimusic.core.event_journal import journal_exception
from aimusic.core.events import events
from aimusic.server.schemas import TakeReviewStatusEvent, TakeReviewStatusResponse
from aimusic.takes import store
from aimusic.takes.lifecycle import AnalysisState, JobKind, JobRecord, JobState, LifecycleFailure

CANONICAL_PPQ = 960
REVIEW_TEMPO_BPM = 120
REVIEW_RENDER_REVISION = "trim-aligned-preroll-v1"
LOGGER = logging.getLogger(__name__)


def review_output_dir(take_id: str) -> Path:
    return paths.run_output_dir(take_id)


def review_midi_path(take_id: str, variant: str = "ensemble") -> Path:
    if variant not in {"solo", "accompaniment", "ensemble"}:
        raise ValueError(f"unknown review variant: {variant}")
    return review_output_dir(take_id) / f"{variant}.mid"


def latest_review_job(piece_id: str, movement: int, take_id: str) -> JobRecord | None:
    jobs = [
        job
        for job in store.list_jobs(piece_id, movement)
        if job.kind == JobKind.RENDER_REVIEW and job.take_id == take_id
    ]
    return max(jobs, key=lambda job: job.requested_at) if jobs else None


def review_status(
    piece_id: str, movement: int, take_id: str
) -> TakeReviewStatusResponse | None:
    job = latest_review_job(piece_id, movement, take_id)
    if job is None:
        return None
    # A renderer change must not silently reuse an artifact with different
    # transport semantics.  Returning no review lets the UI enqueue a fresh
    # job while leaving the immutable raw take untouched.
    if job.input_revision != review_input_revision(piece_id, movement, take_id):
        return None
    state = _public_state(job)
    ensemble_exists = review_midi_path(take_id).is_file()
    if state == "ready" and not ensemble_exists:
        state = "failed"
    return TakeReviewStatusResponse(
        take_id=take_id,
        state=state,
        job_id=job.job_id,
        error=(
            "Review artifact is missing; prepare it again"
            if job.state == JobState.SUCCEEDED and not ensemble_exists
            else job.failure.message
            if job.failure
            else None
        ),
        midi_url=_review_url(piece_id, movement, take_id) if state == "ready" else None,
    )


def review_input_revision(piece_id: str, movement: int, take_id: str) -> str:
    directory = paths.take_dir(piece_id, movement, take_id, create=False)
    aligned_path = directory / "aligned.v2.json"
    take_path = directory / "take.mid"
    if not aligned_path.exists():
        raise ValueError(f"take {take_id} has no authoritative alignment")
    if not take_path.exists():
        raise FileNotFoundError(f"take MIDI is missing: {take_path}")
    aligned_stat = aligned_path.stat()
    take_stat = take_path.stat()
    return _cached_review_input_revision(
        str(aligned_path),
        aligned_stat.st_mtime_ns,
        aligned_stat.st_size,
        str(take_path),
        take_stat.st_mtime_ns,
        take_stat.st_size,
        REVIEW_RENDER_REVISION,
    )


@lru_cache(maxsize=512)
def _cached_review_input_revision(
    aligned_path: str,
    aligned_mtime_ns: int,
    aligned_size: int,
    take_path: str,
    take_mtime_ns: int,
    take_size: int,
    renderer_revision: str,
) -> str:
    """Hash immutable inputs once per observed file identity.

    The stat values are intentionally part of the cache key: an alignment
    retry can replace the sidecar for the same take ID, and that must create a
    new review revision instead of returning a stale in-memory digest.
    """

    del aligned_mtime_ns, aligned_size, take_mtime_ns, take_size
    digest = hashlib.sha256()
    digest.update(renderer_revision.encode())
    digest.update(Path(aligned_path).read_bytes())
    digest.update(Path(take_path).read_bytes())
    return "review:" + digest.hexdigest()


def _reference_seconds_for_score_ticks(
    reference_path: Path,
    score_ticks: list[int],
) -> dict[int, float]:
    midi = MidiFile(reference_path, clip=True)
    tempo_changes: list[tuple[int, int]] = [(0, 500_000)]
    for track in midi.tracks:
        absolute_tick = 0
        for message in track:
            absolute_tick += message.time
            if message.type == "set_tempo":
                tempo_changes.append((absolute_tick, message.tempo))
    tempo_changes.sort(key=lambda item: item[0])

    resolved: dict[int, float] = {}
    current_source_tick = 0
    current_seconds = 0.0
    current_tempo = 500_000
    change_index = 0
    targets = sorted(
        (round(score_tick * midi.ticks_per_beat / CANONICAL_PPQ), score_tick)
        for score_tick in set(score_ticks)
    )
    for source_tick, score_tick in targets:
        while (
            change_index + 1 < len(tempo_changes)
            and tempo_changes[change_index + 1][0] <= source_tick
        ):
            next_tick, next_tempo = tempo_changes[change_index + 1]
            current_seconds += tick2second(
                next_tick - current_source_tick,
                midi.ticks_per_beat,
                current_tempo,
            )
            current_source_tick = next_tick
            current_tempo = next_tempo
            change_index += 1
        resolved[score_tick] = current_seconds + tick2second(
            source_tick - current_source_tick,
            midi.ticks_per_beat,
            current_tempo,
        )
    return resolved


def _review_timing_map(piece_id: str, movement: int, take_id: str):
    aligned = store.get_aligned_result_v2(piece_id, movement, take_id)
    if aligned is None or len(aligned.timing_map) < 2:
        raise ValueError(f"take {take_id} needs at least two aligned timing anchors")

    movement_source = oguri_movement(movement)
    reference_path = movement_source.solo_reference_path
    if not reference_path.exists():
        raise FileNotFoundError(f"solo reference is missing: {reference_path}")

    by_tick: dict[int, list[float]] = {}
    for point in aligned.timing_map:
        reference_tick = point.reference_tick or point.score_tick
        by_tick.setdefault(reference_tick, []).append(point.take_seconds)
    start_reference_tick = aligned.start_reference_tick or aligned.start_score_tick
    end_reference_tick = aligned.end_reference_tick or aligned.end_score_tick
    score_ticks = [*by_tick, start_reference_tick, end_reference_tick]
    reference_seconds = _reference_seconds_for_score_ticks(reference_path, score_ticks)
    anchors = tuple(
        TimingAnchor(
            reference_time_seconds=reference_seconds[score_tick],
            performance_time_seconds=statistics.median(take_times),
            reference_index=index,
            performance_index=index,
        )
        for index, (score_tick, take_times) in enumerate(sorted(by_tick.items()))
    )
    if len(anchors) < 2:
        raise ValueError(f"take {take_id} needs anchors at two distinct score positions")
    return (
        PiecewiseLinearTimingMap(anchors),
        reference_seconds[start_reference_tick],
        reference_seconds[end_reference_tick],
    )


def review_preroll_seconds(piece_id: str, movement: int, take_id: str) -> float:
    """Silence before the first aligned musical onset in the raw capture."""

    aligned = store.get_aligned_result_v2(piece_id, movement, take_id)
    if aligned is None or not aligned.timing_map:
        return 0.0
    return max(0.0, min(point.take_seconds for point in aligned.timing_map))


def render_take_review(piece_id: str, movement: int, take_id: str) -> tuple[Path, Path, Path]:
    take = store.get_take_v2(piece_id, movement, take_id)
    if take.analysis_state != AnalysisState.ALIGNED:
        raise ValueError(
            f"take {take_id} must be aligned before review (state={take.analysis_state.value})"
        )
    take_path = paths.take_dir(piece_id, movement, take_id, create=False) / take.midi_path
    movement_source = oguri_movement(movement)
    accompaniment_source = movement_source.orchestra_accompaniment_path
    if not accompaniment_source.exists():
        raise FileNotFoundError(f"orchestra accompaniment is missing: {accompaniment_source}")

    timing_map, start_reference_seconds, end_reference_seconds = _review_timing_map(
        piece_id, movement, take_id
    )
    preroll_seconds = review_preroll_seconds(piece_id, movement, take_id)
    shifted_timing_map = PiecewiseLinearTimingMap(
        tuple(
            TimingAnchor(
                reference_time_seconds=anchor.reference_time_seconds,
                performance_time_seconds=max(
                    0.0, anchor.performance_time_seconds - preroll_seconds
                ),
                reference_index=anchor.reference_index,
                performance_index=anchor.performance_index,
            )
            for anchor in timing_map.anchors
        )
    )
    output_dir = review_output_dir(take_id)
    solo_output = output_dir / "solo.mid"
    accompaniment_output = output_dir / "accompaniment.mid"
    ensemble_output = output_dir / "ensemble.mid"

    take_duration = max(MidiFile(take_path, clip=True).length, 0.001)
    trimmed_duration = max(take_duration - preroll_seconds, 0.001)
    solo_trim_map = PiecewiseLinearTimingMap(
        (
            TimingAnchor(preroll_seconds, 0.0, 0, 0),
            TimingAnchor(take_duration, trimmed_duration, 1, 1),
        )
    )
    retime_midi(
        take_path,
        solo_output,
        solo_trim_map,
        start_reference_seconds=preroll_seconds,
        output_tempo_bpm=REVIEW_TEMPO_BPM,
    )
    retime_midi(
        accompaniment_source,
        accompaniment_output,
        shifted_timing_map,
        start_reference_seconds=start_reference_seconds,
        end_reference_seconds=end_reference_seconds,
        output_tempo_bpm=REVIEW_TEMPO_BPM,
    )
    combine_review_midi(
        solo_path=solo_output,
        accompaniment_path=accompaniment_output,
        output_path=ensemble_output,
        tempo_bpm=REVIEW_TEMPO_BPM,
    )
    LOGGER.info(
        "Rendered take review take_id=%s raw_duration=%.6fs trimmed_preroll=%.6fs "
        "review_duration=%.6fs alignment_anchors=%d",
        take_id,
        take_duration,
        preroll_seconds,
        trimmed_duration,
        len(timing_map.anchors),
    )
    return solo_output, accompaniment_output, ensemble_output


class ReviewWorker:
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="take-review")

    def enqueue(self, piece_id: str, movement: int, take_id: str) -> JobRecord:
        take = store.get_take_v2(piece_id, movement, take_id)
        if take.analysis_state != AnalysisState.ALIGNED:
            raise ValueError(
                f"take {take_id} must be aligned before review (state={take.analysis_state.value})"
            )
        input_revision = review_input_revision(piece_id, movement, take_id)
        existing = latest_review_job(piece_id, movement, take_id)
        if (
            existing is not None
            and existing.state == JobState.SUCCEEDED
            and not review_midi_path(take_id).is_file()
        ):
            input_revision += ":artifact-missing"
        job = store.create_job(
            piece_id,
            movement,
            take_id,
            JobKind.RENDER_REVIEW,
            input_revision=input_revision,
            deduplicate=True,
        )
        if job.state == JobState.QUEUED:
            self._publish(piece_id, movement, job)
            self._executor.submit(self._run, piece_id, movement, job.job_id)
        return job

    def _run(self, piece_id: str, movement: int, job_id: str) -> None:
        job = store.get_job(piece_id, movement, job_id)
        if job.state == JobState.SUCCEEDED:
            return
        try:
            if job.state == JobState.QUEUED:
                job = store.transition_stored_job(
                    piece_id, movement, job_id, JobState.RUNNING
                )
                self._publish(piece_id, movement, job)
            if job.take_id is None:
                raise ValueError("review job is missing take_id")
            render_take_review(piece_id, movement, job.take_id)
            job = store.transition_stored_job(piece_id, movement, job_id, JobState.SUCCEEDED)
            self._publish(piece_id, movement, job)
        except Exception as exc:
            logging.getLogger(__name__).exception("Review job %s failed", job_id)
            journal_exception(
                "take:review_failed",
                exc,
                piece_id=piece_id,
                movement=movement,
                take_id=job.take_id,
                job_id=job_id,
            )
            current = store.get_job(piece_id, movement, job_id)
            if current.state == JobState.RUNNING:
                current = store.transition_stored_job(
                    piece_id,
                    movement,
                    job_id,
                    JobState.FAILED,
                    failure=LifecycleFailure(
                        code="review_render_failed", message=str(exc), retryable=True
                    ),
                )
                self._publish(piece_id, movement, current)

    def _publish(self, piece_id: str, movement: int, job: JobRecord) -> None:
        events.publish(
            TakeReviewStatusEvent(
                type="take:review_status",
                take_id=job.take_id or "",
                piece_id=piece_id,
                movement=movement,
                state=_public_state(job),
                job_id=job.job_id,
                error=job.failure.message if job.failure else None,
                midi_url=_review_url(piece_id, movement, job.take_id)
                if _public_state(job) == "ready"
                else None,
            )
        )

    def recover_all(self) -> int:
        recovered = 0
        takes_root = paths.takes_root()
        if not takes_root.is_dir():
            return 0
        for piece_dir in takes_root.iterdir():
            if not piece_dir.is_dir():
                continue
            for movement_dir in piece_dir.iterdir():
                if not movement_dir.is_dir():
                    continue
                try:
                    movement = int(movement_dir.name)
                except ValueError:
                    continue
                for job in store.list_jobs(piece_dir.name, movement):
                    if job.kind != JobKind.RENDER_REVIEW:
                        continue
                    if job.state == JobState.RUNNING:
                        job = store.transition_stored_job(
                            piece_dir.name,
                            movement,
                            job.job_id,
                            JobState.FAILED,
                            failure=LifecycleFailure(
                                code="worker_restarted",
                                message="server restarted while preparing review",
                                retryable=True,
                            ),
                        )
                        job = store.transition_stored_job(
                            piece_dir.name, movement, job.job_id, JobState.QUEUED
                        )
                    if job.state == JobState.QUEUED:
                        self._executor.submit(self._run, piece_dir.name, movement, job.job_id)
                        recovered += 1
        return recovered


def _public_state(job: JobRecord) -> str:
    return {
        JobState.QUEUED: "queued",
        JobState.RUNNING: "running",
        JobState.SUCCEEDED: "ready",
        JobState.FAILED: "failed",
        JobState.CANCELLED: "failed",
    }[job.state]


def _review_url(piece_id: str, movement: int, take_id: str | None) -> str | None:
    if not take_id:
        return None
    return f"/api/takes/{take_id}/review/midi?piece_id={piece_id}&movement={movement}"


review_worker = ReviewWorker()


__all__ = [
    "ReviewWorker",
    "latest_review_job",
    "render_take_review",
    "review_input_revision",
    "review_midi_path",
    "review_preroll_seconds",
    "review_status",
    "review_worker",
]
