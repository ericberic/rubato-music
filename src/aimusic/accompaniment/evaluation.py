"""Evaluation helpers for simulated accompaniment runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from aimusic.accompaniment.scheduler import ScheduledAccompanimentEvent


@dataclass(frozen=True)
class ScheduleEvaluation:
    """Timing comparison between expected and scheduled accompaniment events."""

    timing_errors_seconds: dict[str, float]
    missing_event_ids: tuple[str, ...]
    unexpected_event_ids: tuple[str, ...]
    tolerance_seconds: float

    @property
    def max_abs_error_seconds(self) -> float:
        if not self.timing_errors_seconds:
            return 0.0
        return max(abs(error) for error in self.timing_errors_seconds.values())

    @property
    def passed(self) -> bool:
        return (
            not self.missing_event_ids
            and not self.unexpected_event_ids
            and self.max_abs_error_seconds <= self.tolerance_seconds
        )


def evaluate_scheduled_events(
    scheduled_events: tuple[ScheduledAccompanimentEvent, ...],
    expected_perf_times: Mapping[str, float],
    *,
    tolerance_seconds: float,
) -> ScheduleEvaluation:
    """Compare scheduled accompaniment event times against expected times."""

    if tolerance_seconds < 0:
        raise ValueError("tolerance_seconds must be non-negative")

    actual_perf_times: dict[str, float] = {}
    for event in scheduled_events:
        event_id = event.event.event_id
        if event_id in actual_perf_times:
            raise ValueError(f"Duplicate scheduled event ID: {event_id}")
        actual_perf_times[event_id] = event.perf_time
    expected_ids = set(expected_perf_times)
    actual_ids = set(actual_perf_times)
    common_ids = sorted(expected_ids & actual_ids)
    errors = {
        event_id: actual_perf_times[event_id] - expected_perf_times[event_id]
        for event_id in common_ids
    }
    return ScheduleEvaluation(
        timing_errors_seconds=errors,
        missing_event_ids=tuple(sorted(expected_ids - actual_ids)),
        unexpected_event_ids=tuple(sorted(actual_ids - expected_ids)),
        tolerance_seconds=tolerance_seconds,
    )
