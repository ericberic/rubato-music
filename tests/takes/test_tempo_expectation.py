"""The Interpretation's tempo prior beats reactive on a repeatable ritardando.

The evaluator fits through the real ``fold_cells`` (weighted, recency-aware),
so these tests exercise the same model the runtime reads.
"""

from __future__ import annotations

import pytest

from aimusic.accompaniment.bundle_v2 import BundleRef
from aimusic.takes.lifecycle import AlignedResultV2, CellSampleV2
from aimusic.takes.profile import fold_cells
from aimusic.takes.tempo_expectation import (
    TempoPrior,
    evaluate_interpretation,
)

_BUNDLE = BundleRef(bundle_id="chopin_op11_movement_2", revision="r1", timeline_id="t1")


def _take(take_id: str, periods: list[float]) -> AlignedResultV2:
    # Canonical cells on the 480-tick half-quarter grid, each a local tempo.
    cells = tuple(
        CellSampleV2(
            score_tick=i * 480,
            seconds_per_quarter=p,
            rubato_ratio=1.0,
            velocity=64.0,
            pedal=0.0,
            quality=0.9,
        )
        for i, p in enumerate(periods)
    )
    return AlignedResultV2(
        take_id=take_id,
        bundle=_BUNDLE,
        aligner="test",
        coordinate_system="canonical_score",
        start_score_tick=0,
        end_score_tick=(len(periods) - 1) * 480,
        start_reference_tick=0,
        end_reference_tick=(len(periods) - 1) * 480,
        mapping_id="m1",
        match_rate=0.9,
        ambiguous=False,
        matched_notes=len(periods),
        extra_notes=0,
        missing_notes=0,
        base_seconds_per_quarter=periods[0],
        cell_samples=cells,
    )


def test_prior_reads_expected_period_and_spread() -> None:
    cells, _base = fold_cells(
        [_take("a", [0.6, 0.5]), _take("b", [0.6, 0.7]), _take("c", [0.6, 0.6])]
    )
    prior = TempoPrior(cells)
    # All three agree at tick 0: expected 0.6, zero spread.
    assert prior.period_at(0) == pytest.approx(0.6)
    assert prior.dispersion_at(0) == pytest.approx(0.0)
    # They disagree at tick 480: nonzero spread.
    assert prior.dispersion_at(480) > 0.0


def test_one_take_has_no_estimated_dispersion() -> None:
    cells, _base = fold_cells([_take("only", [0.6, 0.7])])
    prior = TempoPrior(cells)

    assert prior.period_at(0) == pytest.approx(0.6)
    assert prior.dispersion_at(0) is None


def test_prior_does_not_extrapolate_across_uncovered_cells() -> None:
    cells, _base = fold_cells(
        [_take("a", [0.6, 0.7]), _take("b", [0.6, 0.7])]
    )
    prior = TempoPrior(cells)

    assert prior.period_at(480) == pytest.approx(0.7)
    assert prior.period_at(721) is None
    assert prior.dispersion_at(721) is None


def test_evaluation_beats_reactive_on_a_repeatable_ritardando() -> None:
    curve = [0.50, 0.60, 0.70, 0.80, 0.90, 1.00]
    takes = [
        _take("t1", [p + 0.00 for p in curve]),
        _take("t2", [p + 0.01 for p in curve]),
        _take("t3", [p - 0.01 for p in curve]),
    ]
    r = evaluate_interpretation(takes)

    assert r.skill > 0.5
    assert r.onset_mae_ms < r.reactive_onset_mae_ms
    assert r.coverage == pytest.approx(1.0)
    assert r.take_count == 3


def test_evaluate_requires_two_takes() -> None:
    with pytest.raises(ValueError, match="at least 2 takes"):
        evaluate_interpretation([_take("solo", [0.5, 0.6])])


def test_smoothed_period_averages_out_sub_beat_microrubato() -> None:
    from aimusic.takes.profile import PerformanceProfileCell
    from aimusic.takes.tempo_expectation import TempoPrior

    # Alternating fast/slow half-beat cells (micro-rubato) averaging 1.0 s/quarter.
    cells = [
        PerformanceProfileCell(
            score_tick=480 * i,
            seconds_per_quarter=1.0 + (0.4 if i % 2 else -0.4),
            seconds_per_quarter_mad=0.0,
            rubato_ratio=1.0,
            rubato_ratio_mad=0.0,
            velocity=64.0,
            velocity_mad=0.0,
            pedal=0.0,
            pedal_mad=0.0,
            support=3,
            mean_quality=0.8,
        )
        for i in range(8)
    ]
    prior = TempoPrior(cells, grid_step_ticks=480)
    raw = prior.period_at(480 * 3)
    smoothed = prior.smoothed_period_at(480 * 3, window_ticks=4 * 960)
    assert abs(raw - 1.0) > 0.3  # a single cell carries the micro-rubato
    assert smoothed == pytest.approx(1.0, abs=0.05)  # the bar-average is steady
    assert prior.support_at(480 * 3) == 3
    assert prior.support_at(480 * 999) == 0
