from __future__ import annotations

import math

from aimusic.accompaniment.follower_take_eval import FollowerSample, FollowerTakeResult


def _result(samples: list[FollowerSample]) -> FollowerTakeResult:
    return FollowerTakeResult(take_id="t", first_measure=13, seeded=True, samples=samples)


def test_gross_error_is_median_absolute_beat_error() -> None:
    result = _result(
        [
            FollowerSample(perf_time=0.0, gt_beat=10.0, follower_beat=12.0, confidence=0.5),
            FollowerSample(perf_time=1.0, gt_beat=20.0, follower_beat=24.0, confidence=0.5),
            FollowerSample(perf_time=2.0, gt_beat=30.0, follower_beat=39.0, confidence=0.5),
        ]
    )
    assert result.gross_error() == 4.0  # median(|2|, |4|, |9|)
    assert result.matched() == 3


def test_gross_error_restricts_to_a_canonical_beat_window() -> None:
    result = _result(
        [
            FollowerSample(perf_time=0.0, gt_beat=10.0, follower_beat=11.0, confidence=0.5),
            FollowerSample(perf_time=1.0, gt_beat=100.0, follower_beat=170.0, confidence=0.5),
        ]
    )
    # Only the in-window (beats [0,50)) sample counts.
    assert result.gross_error(0.0, 50.0) == 1.0


def test_gross_error_is_nan_when_no_samples_match() -> None:
    assert math.isnan(_result([]).gross_error())


def test_lock_losses_count_confidence_falling_edges() -> None:
    result = _result(
        [
            FollowerSample(perf_time=0.0, gt_beat=0.0, follower_beat=0.0, confidence=0.5),
            FollowerSample(perf_time=1.0, gt_beat=1.0, follower_beat=1.0, confidence=0.0),  # loss
            FollowerSample(perf_time=2.0, gt_beat=2.0, follower_beat=2.0, confidence=0.5),
            FollowerSample(perf_time=3.0, gt_beat=3.0, follower_beat=3.0, confidence=0.0),  # loss
        ]
    )
    assert result.lock_losses() == 2
