from __future__ import annotations

import pytest

from aimusic.accompaniment.bundle_v2 import BundleRef
from aimusic.takes.lifecycle import AlignedResultV2, CellSampleV2
from aimusic.takes.passage_analysis import analyze_passage

BUNDLE = BundleRef(bundle_id="test", revision="r1", timeline_id="t1")


def _alignment(take_id: str, periods: tuple[float, ...], velocities: tuple[float, ...]):
    baseline = sorted(periods)[len(periods) // 2]
    return AlignedResultV2(
        take_id=take_id,
        bundle=BUNDLE,
        aligner="fixture",
        coordinate_system="canonical_score",
        start_score_tick=960,
        end_score_tick=960 * len(periods),
        start_reference_tick=100,
        end_reference_tick=1000,
        mapping_id="fixture-map",
        match_rate=0.84,
        ambiguous=False,
        matched_notes=100,
        extra_notes=2,
        missing_notes=3,
        base_seconds_per_quarter=baseline,
        cell_samples=tuple(
            CellSampleV2(
                score_tick=(index + 1) * 480,
                seconds_per_quarter=period,
                rubato_ratio=period / baseline,
                velocity=velocities[index],
                pedal=0,
                quality=0.84,
            )
            for index, period in enumerate(periods)
        ),
    )


def test_three_consistent_passes_provide_repeatability_evidence() -> None:
    result = analyze_passage(
        (
            _alignment("a", (0.45, 0.455, 0.45), (70, 75, 72)),
            _alignment("b", (0.445, 0.45, 0.455), (71, 75, 73)),
            _alignment("c", (0.455, 0.46, 0.445), (70, 76, 72)),
        )
    )

    assert result.take_count == 3
    assert result.common_cell_count == 3
    assert result.learned_tempo_bpm == pytest.approx(133.3, abs=0.1)
    # Normalizing each take by its own baseline removes global pace changes;
    # this fixture's remaining local ratios have a robust median MAD of zero.
    assert result.typical_tempo_variation_percent == 0.0
    assert result.evidence_multiplier > 2.5
    assert result.high_variance_points == ()


def test_high_variance_cell_is_reported_without_distorting_typical_tempo() -> None:
    result = analyze_passage(
        (
            _alignment("a", (0.45, 0.325, 0.45), (70, 70, 70)),
            _alignment("b", (0.45, 0.45, 0.45), (70, 70, 70)),
            _alignment("c", (0.45, 0.60, 0.45), (70, 70, 70)),
        )
    )

    assert result.learned_tempo_bpm == pytest.approx(133.3, abs=0.1)
    assert result.typical_tempo_variation_percent == 0.0
    assert result.high_variance_points[0].score_tick == 960
    assert result.high_variance_points[0].tempo_variation_percent == pytest.approx(27.8, abs=0.1)


def test_one_pass_does_not_claim_observed_repeatability() -> None:
    result = analyze_passage((_alignment("a", (0.45,), (70,)),))

    assert result.take_count == 1
    assert result.confidence == result.one_pass_baseline
    assert result.evidence_multiplier == 1.0
