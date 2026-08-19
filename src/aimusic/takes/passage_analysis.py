"""Repeatability evidence derived from aligned rehearsal passes.

One pass supplies a timing curve. Repeated passes additionally supply a
distribution: the median captures stable intent while median absolute
deviation (MAD) identifies expressive choices and mistakes.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Iterable

from aimusic.takes.lifecycle import AlignedResultV2
from aimusic.takes.profile import fold_cells

TARGET_REPEAT_COUNT = 3
CONSISTENCY_SCALE = 0.12


@dataclass(frozen=True)
class VariancePoint:
    score_tick: int
    tempo_variation_percent: float


@dataclass(frozen=True)
class PassageAnalysis:
    take_count: int
    common_cell_count: int
    mean_alignment_quality: float
    learned_tempo_bpm: float | None
    typical_tempo_variation_percent: float | None
    typical_velocity_variation: float | None
    confidence: float
    one_pass_baseline: float
    evidence_multiplier: float
    high_variance_points: tuple[VariancePoint, ...]


def analyze_passage(
    alignments: Iterable[AlignedResultV2],
) -> PassageAnalysis:
    """Summarize stable and variable evidence shared by aligned passes.

    The per-cell tempo/velocity/rubato statistics come from the single canonical
    fold (:func:`aimusic.takes.profile.fold_cells`) -- the same
    quality-and-recency-weighted median the runtime Interpretation uses -- so
    the rehearsal-UI summary and the live model can never disagree about what a
    take "says".
    """

    results = tuple(alignments)
    if any(result.coordinate_system != "canonical_score" for result in results):
        raise ValueError("passage analysis requires canonical-score alignments")
    mean_quality = statistics.fmean(result.match_rate for result in results) if results else 0.0
    cells, base_seconds_per_quarter = fold_cells(results)
    take_count = len(results)
    if not cells:
        return PassageAnalysis(
            take_count=take_count,
            common_cell_count=0,
            mean_alignment_quality=round(mean_quality, 4),
            learned_tempo_bpm=None,
            typical_tempo_variation_percent=None,
            typical_velocity_variation=None,
            confidence=0.0,
            one_pass_baseline=round(mean_quality / TARGET_REPEAT_COUNT, 4),
            evidence_multiplier=0.0,
            high_variance_points=(),
        )

    common_cell_count = sum(1 for cell in cells if cell.support == take_count)
    relative_period_mads = [cell.rubato_ratio_mad / cell.rubato_ratio for cell in cells]
    velocity_mads = [cell.velocity_mad for cell in cells]
    variance_points = [
        VariancePoint(cell.score_tick, round(100.0 * ratio, 1))
        for cell, ratio in zip(cells, relative_period_mads)
    ]

    typical_relative_mad = statistics.median(relative_period_mads)
    support = min(take_count / TARGET_REPEAT_COUNT, 1.0)
    confidence = support * mean_quality * math.exp(-typical_relative_mad / CONSISTENCY_SCALE)
    baseline = mean_quality / TARGET_REPEAT_COUNT
    learned_period = base_seconds_per_quarter or statistics.median(
        [cell.seconds_per_quarter for cell in cells]
    )
    highest_variance = tuple(
        sorted(
            (point for point in variance_points if point.tempo_variation_percent >= 10.0),
            key=lambda point: point.tempo_variation_percent,
            reverse=True,
        )[:5]
    )
    return PassageAnalysis(
        take_count=take_count,
        common_cell_count=common_cell_count,
        mean_alignment_quality=round(mean_quality, 4),
        learned_tempo_bpm=(round(60.0 / learned_period, 1) if learned_period else None),
        typical_tempo_variation_percent=round(typical_relative_mad * 100, 1),
        typical_velocity_variation=round(statistics.median(velocity_mads), 1),
        confidence=round(confidence, 4),
        one_pass_baseline=round(baseline, 4),
        evidence_multiplier=round(confidence / baseline, 2) if baseline else 0.0,
        high_variance_points=highest_variance,
    )


__all__ = ["PassageAnalysis", "VariancePoint", "analyze_passage"]
