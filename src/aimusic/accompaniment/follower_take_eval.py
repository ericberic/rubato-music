"""Offline follower evaluation on real recorded takes, seeded like the live path.

The live runtime seeds the Matchmaker follower at the cue-in
(``initial_reference_beat``) and maps its source-performance position to
canonical beats through :class:`CanonicalFollower`. A follower eval that
cold-starts the matcher at beat 0 does **not** track like production -- the take
begins mid-piece, so a cold matcher never locks and drifts tens of beats behind,
which silently invalidates any accuracy comparison built on it. This harness
reproduces the live seeding and coordinate mapping so follower changes can be
measured honestly against the reviewed take alignments
(``take_alignments.machine.json``).

Scoring is in canonical score beats -- the same coordinate the take alignments
and the ``CanonicalFollower`` output share -- so ``gross_error`` is directly
"how many beats is the reported score position off from ground truth".
"""

from __future__ import annotations

import bisect
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from statistics import median

import mido

from aimusic.accompaniment.following import (
    PerformedNote,
    RelockingFollower,
    ScoreFollower,
)
from aimusic.accompaniment.matchmaker_follower import MatchmakerStreamFollower
from aimusic.accompaniment.runtime_projection import (
    MOVEMENT_2_BUNDLE_ID,
    ProvisionalRuntimeProjection,
    default_bundle_registry,
    project_bundle_v2_to_provisional_runtime,
    runtime_follower,
)

# A follower factory receives the seeded reference beat (or ``None`` for a cold
# start) and returns the *raw* follower; the harness applies the canonical
# mapping around it, exactly as the live worker does.
FollowerFactory = Callable[[str, float | None], ScoreFollower]


@dataclass(frozen=True)
class FollowerSample:
    """One emitted position: performed time, ground-truth and reported canonical
    beat, and the follower's (uncalibrated) confidence."""

    perf_time: float
    gt_beat: float
    follower_beat: float
    confidence: float


@dataclass
class FollowerTakeResult:
    take_id: str
    first_measure: int
    seeded: bool
    samples: list[FollowerSample]

    def gross_error(self, beat_lo: float | None = None, beat_hi: float | None = None) -> float:
        """Median |reported - ground-truth| canonical beats, optionally within a
        canonical-beat window. This is *not* de-trended: it is the real position
        error the accompaniment would act on."""
        errs = [
            abs(s.follower_beat - s.gt_beat)
            for s in self.samples
            if (beat_lo is None or beat_lo <= s.gt_beat < beat_hi)
        ]
        return median(errs) if errs else float("nan")

    def lock_losses(self) -> int:
        """Count confidence 1->0 transitions (the follower's own 'lost lock')."""
        return sum(
            1
            for a, b in zip(self.samples, self.samples[1:])
            if a.confidence > 0 and b.confidence == 0
        )

    def matched(self) -> int:
        return len(self.samples)


def _default_recording_path(take_id: str) -> Path:
    return Path.home() / "Library/Application Support/Rubato/data/recordings" / f"{take_id}.mid"


def _default_alignments_path(piece_id: str, movement: int) -> Path:
    return Path(f"data/scores/{piece_id}_movement_{movement}/derived/take_alignments.machine.json")


def _ground_truth_interpolator(
    alignments_path: Path, take_id: str
) -> tuple[Callable[[float], float], int]:
    document = json.loads(alignments_path.read_text(encoding="utf-8"))
    take = document["takes"][take_id]
    pairs = sorted(take["pairs"], key=lambda pair: pair[1])
    times = [pair[1] for pair in pairs]
    beats = [pair[0] for pair in pairs]

    def gt_at(perf_time: float) -> float:
        index = bisect.bisect_left(times, perf_time)
        if index <= 0:
            return beats[0]
        if index >= len(times):
            return beats[-1]
        t0, t1, b0, b1 = times[index - 1], times[index], beats[index - 1], beats[index]
        if t1 == t0:
            return b1
        return b0 + (b1 - b0) * (perf_time - t0) / (t1 - t0)

    return gt_at, int(take["first_measure"])


def _load_performed_onsets(recording_path: Path) -> list[tuple[float, int]]:
    onsets: list[tuple[float, int]] = []
    now = 0.0
    for message in mido.MidiFile(str(recording_path)):
        now += message.time
        if message.type == "note_on" and message.velocity > 0:
            onsets.append((now, message.note))
    onsets.sort()
    return onsets


def _seeded_matchmaker_factory(
    projection: ProvisionalRuntimeProjection,
    *,
    tempo_bpm: float,
    max_wait_seconds: float,
) -> FollowerFactory:
    score_file = str(projection.follower_reference_path)

    def make(_reference_path: str, initial_reference_beat: float | None) -> ScoreFollower:
        return MatchmakerStreamFollower(
            score_file,
            method="pthmm",
            tempo_bpm=tempo_bpm,
            max_wait_seconds=max_wait_seconds,
            minimum_lock_updates=3,
            initial_reference_beat=initial_reference_beat,
        )

    return make


def evaluate_follower_on_take(
    take_id: str,
    *,
    piece_id: str = "chopin_op11",
    movement: int = 2,
    bundle_id: str = MOVEMENT_2_BUNDLE_ID,
    seed: bool = True,
    relock: bool = False,
    start_measure: int | None = None,
    recording_path: Path | None = None,
    alignments_path: Path | None = None,
    follower_factory: FollowerFactory | None = None,
    tempo_bpm: float = 52.0,
    max_wait_seconds: float = 0.25,
) -> FollowerTakeResult:
    """Replay one recorded take through a follower seeded like the live path.

    ``seed`` (default) warm-starts the follower at the take's entry measure, the
    way the live worker does; ``seed=False`` reproduces the invalid cold start
    for A/B. ``relock`` wraps the canonical follower in the same
    :class:`RelockingFollower` watchdog the live worker uses, so the harness can
    A/B the re-localization recovery. ``follower_factory`` overrides the raw
    follower under test; the canonical mapping is always applied around it.
    """
    # Import here so the shared entry-point logic stays owned by the live runtime.
    from aimusic.server.live_runtime import _runtime_entry_point

    registry = default_bundle_registry()
    projection = project_bundle_v2_to_provisional_runtime(
        bundle_id=bundle_id, revision=None, registry=registry
    )
    alignments_path = alignments_path or _default_alignments_path(piece_id, movement)
    recording_path = recording_path or _default_recording_path(take_id)
    gt_at, first_measure = _ground_truth_interpolator(alignments_path, take_id)
    entry_measure = start_measure if start_measure is not None else first_measure

    initial_reference_beat: float | None = None
    if seed:
        entry = _runtime_entry_point(projection, start_measure=entry_measure)
        if entry is not None:
            initial_reference_beat = entry.follower_prior_reference_beat

    factory = follower_factory or _seeded_matchmaker_factory(
        projection, tempo_bpm=tempo_bpm, max_wait_seconds=max_wait_seconds
    )
    raw = factory(str(projection.follower_reference_path), initial_reference_beat)
    follower = runtime_follower(raw, projection)
    if relock:
        relocalize = getattr(raw, "relocalize", None)
        if relocalize is not None:
            follower = RelockingFollower(follower, relocalize=relocalize)

    samples: list[FollowerSample] = []
    try:
        for perf_time, pitch in _load_performed_onsets(recording_path):
            update = follower.observe(PerformedNote(perf_time=perf_time, pitch=pitch, velocity=70))
            if update is None:
                continue
            samples.append(
                FollowerSample(
                    perf_time=perf_time,
                    gt_beat=gt_at(perf_time),
                    follower_beat=update.score_beat,
                    confidence=update.confidence,
                )
            )
    finally:
        close = getattr(raw, "close", None)
        if close is not None:
            close()

    return FollowerTakeResult(
        take_id=take_id,
        first_measure=entry_measure,
        seeded=seed,
        samples=samples,
    )
