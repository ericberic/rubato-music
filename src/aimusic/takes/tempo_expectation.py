"""Reading the Interpretation as a tempo prior, and grading its fit.

Two small things live here, both thin layers over the canonical Interpretation
(the per-cell profile produced by :func:`aimusic.takes.profile.fold_cells`):

- ``TempoPrior`` -- a read-only view that answers "what tempo do we expect at
  this score tick, and how consistently is it played?" It is what the live LTE
  clock leans on; it never fits anything itself.
- ``evaluate_interpretation`` -- the offline report card. It grades the model
  leave-one-take-out: fit the Interpretation on the other takes with the *same*
  fold the runtime uses, predict each held-out take, and measure the error
  against a reactive baseline and against the pianist's own take-to-take spread.

Honesty rules baked in: only held-out numbers are produced (never in-sample
fit), and the evaluator fits through ``fold_cells`` so it scores exactly the
model live playback reads -- not a look-alike.

Known limits (do not over-read the numbers): this is a **1-step math proxy**
(``delta_beats * |expected - actual|`` per cell), not a closed-loop
Follower->TempoModel->Scheduler->Output simulation, so it measures the tempo
*model's* prediction, not end-to-end live dispatch accuracy. It is bounded by
the alignment quality of the takes it grades (alignment error shows up as model
error) and, at the small take counts of early rehearsal, is a coarse estimate.
It is a curation compass, not a substitute for the deterministic closed-loop
harness that architectural ship/kill decisions require
(docs/decisions/0009-rehearsal-as-dataset-lifecycle.md).
"""

from __future__ import annotations

import statistics
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass

from aimusic.takes.lifecycle import AlignedResultV2, CellSampleV2
from aimusic.takes.models import Interpretation, PerformanceProfileCell
from aimusic.takes.profile import fold_cells

TICKS_PER_QUARTER = 960
MIN_CELL_QUALITY = 0.5


class TempoPrior:
    """Read-only view of an Interpretation's expected tempo per score tick."""

    def __init__(
        self,
        cells: Sequence[PerformanceProfileCell],
        *,
        grid_step_ticks: int = 480,
    ) -> None:
        if grid_step_ticks <= 0:
            raise ValueError("grid_step_ticks must be positive")
        self._cells = tuple(sorted(cells, key=lambda c: c.score_tick))
        self._ticks = [c.score_tick for c in self._cells]
        self._coverage_radius_ticks = grid_step_ticks / 2

    @classmethod
    def from_interpretation(cls, interpretation: Interpretation) -> "TempoPrior":
        return cls(
            interpretation.cells,
            grid_step_ticks=interpretation.grid_step_ticks,
        )

    @property
    def covered_ticks(self) -> tuple[int, ...]:
        return tuple(self._ticks)

    def period_at(self, score_tick: float) -> float | None:
        """Expected seconds/quarter at a tick (nearest covered cell)."""

        cell = self._nearest(score_tick)
        return cell.seconds_per_quarter if cell is not None else None

    def dispersion_at(self, score_tick: float) -> float | None:
        """Take-to-take spread at a tick, only when at least two takes voted.

        One observation has a numerical MAD of zero but no estimated
        dispersion. Returning ``None`` makes runtime trust degrade to reactive
        until consistency is actually supported by repeated takes.
        """

        cell = self._nearest(score_tick)
        return cell.seconds_per_quarter_mad if cell is not None and cell.support >= 2 else None

    def support_at(self, score_tick: float) -> int:
        """Number of takes that voted on the nearest covered cell (0 if none)."""

        cell = self._nearest(score_tick)
        return cell.support if cell is not None else 0

    def smoothed_period_at(self, score_tick: float, *, window_ticks: int) -> float | None:
        """Phrase-level expected seconds/quarter, averaged over a window.

        Per-cell periods carry real but fine-grained micro-rubato (which notes
        are leaned on); a live follower cannot resolve it and playing it as a
        pulse fluctuates. Averaging over roughly a bar keeps the phrase-level
        tempo arc the takes agree on while dropping the sub-beat detail, giving a
        stable pace the runtime can anchor to. ``None`` when no covered cell
        falls in the window.
        """

        if window_ticks <= 0:
            raise ValueError("window_ticks must be positive")
        lo, hi = score_tick - window_ticks / 2, score_tick + window_ticks / 2
        periods = [
            cell.seconds_per_quarter
            for cell in self._cells
            if lo <= cell.score_tick <= hi and cell.seconds_per_quarter > 0
        ]
        if not periods:
            return self.period_at(score_tick)
        return sum(periods) / len(periods)

    def _nearest(self, score_tick: float) -> PerformanceProfileCell | None:
        if not self._cells:
            return None
        i = bisect_left(self._ticks, score_tick)
        if i == 0:
            nearest = self._cells[0]
        elif i >= len(self._cells):
            nearest = self._cells[-1]
        else:
            before, after = self._cells[i - 1], self._cells[i]
            nearest = (
                before
                if (score_tick - before.score_tick) <= (after.score_tick - score_tick)
                else after
            )
        if abs(score_tick - nearest.score_tick) > self._coverage_radius_ticks:
            return None
        return nearest


@dataclass(frozen=True)
class Readiness:
    """Offline goodness of the Interpretation against its takes, per passage."""

    take_count: int
    evaluated_cells: int
    coverage: float  # fraction of held-out steps the prior could predict

    onset_mae_ms: float  # leave-one-out one-step onset prediction error
    tempo_mae_ms_per_beat: float  # leave-one-out tempo prediction error

    reactive_onset_mae_ms: float  # same error for the naive last-tempo baseline
    skill: float  # 1 - LTE/reactive; > 0 means the prior beats reactive

    consistency_mad_percent: float  # median per-cell take-to-take tempo MAD (%)
    consistency_floor_ms_per_beat: float  # irreducible take-to-take spread

    verdict: str  # thin / unsteady / learning / ready

    def summary(self) -> str:
        return (
            f"{self.take_count} takes, {self.evaluated_cells} cells "
            f"({self.coverage:.0%} covered)\n"
            f"  onset error (LTE):      {self.onset_mae_ms:.1f} ms  "
            f"[reactive {self.reactive_onset_mae_ms:.1f} ms, skill {self.skill:+.2f}]\n"
            f"  tempo error (LTE):      {self.tempo_mae_ms_per_beat:.1f} ms/beat  "
            f"[consistency floor {self.consistency_floor_ms_per_beat:.1f} ms/beat]\n"
            f"  take-to-take spread:    {self.consistency_mad_percent:.1f}% tempo MAD\n"
            f"  verdict: {self.verdict}"
        )


def evaluate_interpretation(
    takes: Sequence[AlignedResultV2],
    *,
    min_quality: float = MIN_CELL_QUALITY,
) -> Readiness:
    """Leave-one-take-out grading of the Interpretation fit.

    ``takes`` are aligned results in recency order (most-recent-first), matching
    the order the profile fold assumes. Each held-out take is predicted by an
    Interpretation fitted on the others through :func:`fold_cells`.
    """

    takes = tuple(takes)
    if len(takes) < 2:
        raise ValueError("need at least 2 takes to evaluate (leave-one-out)")
    per_take = [_best_samples(t, min_quality) for t in takes]

    lte_onset: list[float] = []
    lte_tempo: list[float] = []
    reactive_onset: list[float] = []
    predictable = 0
    total_steps = 0

    for held_out in range(len(takes)):
        others = [takes[i] for i in range(len(takes)) if i != held_out]
        cells, _base = fold_cells(others)
        prior = TempoPrior(cells)
        rows = sorted(per_take[held_out].items())
        prev_period: float | None = None
        for (tick, sample), (next_tick, _next) in zip(rows, rows[1:]):
            total_steps += 1
            delta_beats = (next_tick - tick) / TICKS_PER_QUARTER
            actual = sample.seconds_per_quarter
            expected = prior.period_at(tick)
            if expected is not None:
                predictable += 1
                lte_tempo.append(abs(expected - actual) * 1000.0)
                lte_onset.append(delta_beats * abs(expected - actual) * 1000.0)
            if prev_period is not None:
                reactive_onset.append(delta_beats * abs(prev_period - actual) * 1000.0)
            prev_period = actual

    # Consistency: the fitted model's own take-to-take spread where >= 2 takes voted.
    full_cells, _ = fold_cells(takes)
    shared = [c for c in full_cells if c.support >= 2]
    rel_mads = [c.seconds_per_quarter_mad / c.seconds_per_quarter for c in shared]
    floor_ms = [c.seconds_per_quarter_mad * 1000.0 for c in shared]
    consistency_mad = statistics.median(rel_mads) * 100.0 if rel_mads else 0.0
    consistency_floor = statistics.median(floor_ms) if floor_ms else 0.0

    onset_mae = statistics.median(lte_onset) if lte_onset else float("nan")
    tempo_mae = statistics.median(lte_tempo) if lte_tempo else float("nan")
    reactive_mae = statistics.median(reactive_onset) if reactive_onset else float("nan")
    skill = 1.0 - (onset_mae / reactive_mae) if reactive_mae else 0.0
    coverage = predictable / total_steps if total_steps else 0.0

    return Readiness(
        take_count=len(takes),
        evaluated_cells=predictable,
        coverage=round(coverage, 3),
        onset_mae_ms=round(onset_mae, 1),
        tempo_mae_ms_per_beat=round(tempo_mae, 1),
        reactive_onset_mae_ms=round(reactive_mae, 1),
        skill=round(skill, 2),
        consistency_mad_percent=round(consistency_mad, 1),
        consistency_floor_ms_per_beat=round(consistency_floor, 1),
        verdict=_verdict(len(takes), skill, tempo_mae, consistency_floor, consistency_mad),
    )


def _verdict(
    take_count: int,
    skill: float,
    tempo_mae: float,
    floor: float,
    consistency_mad: float,
) -> str:
    if take_count < 3:
        return "thin: fewer than 3 takes, the Interpretation is under-supported"
    if consistency_mad > 15.0:
        return "unsteady: >15% tempo spread; more takes won't help until you play it steadier"
    if skill <= 0.0:
        return "unsteady: the Interpretation does not beat reactive here; record steadier takes"
    if floor > 0 and tempo_mae <= 1.5 * floor:
        return "ready: near your take-to-take floor -- limited by your variability, not the model"
    return "learning: the model trails your consistency floor; more takes should help"


def _best_samples(
    result: AlignedResultV2, min_quality: float
) -> dict[int, CellSampleV2]:
    samples: dict[int, CellSampleV2] = {}
    for sample in result.cell_samples:
        if sample.quality < min_quality:
            continue
        current = samples.get(sample.score_tick)
        if current is None or sample.quality > current.quality:
            samples[sample.score_tick] = sample
    return samples


__all__ = ["Readiness", "TempoPrior", "evaluate_interpretation"]
