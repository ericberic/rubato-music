from __future__ import annotations

import pytest

from aimusic.accompaniment.evaluation import evaluate_scheduled_events
from aimusic.accompaniment.scheduler import ScheduledAccompanimentEvent
from aimusic.accompaniment.score_bundle import ScoreEvent
from aimusic.accompaniment.section_policy import AccompanimentMode


def scheduled(event_id: str, perf_time: float) -> ScheduledAccompanimentEvent:
    return ScheduledAccompanimentEvent(
        event=ScoreEvent(
            event_id=event_id,
            measure=1,
            beat=0,
            part_id="strings",
            role="accompaniment",
            duration_beats=1,
            pitch=60,
        ),
        perf_time=perf_time,
        section_mode=AccompanimentMode.FOLLOW,
        tempo_bpm=120,
    )


def test_evaluate_scheduled_events_reports_errors_and_missing_events() -> None:
    evaluation = evaluate_scheduled_events(
        (scheduled("a", 1.02), scheduled("extra", 3.0)),
        {"a": 1.0, "missing": 2.0},
        tolerance_seconds=0.01,
    )

    assert not evaluation.passed
    assert evaluation.timing_errors_seconds == pytest.approx({"a": 0.02})
    assert evaluation.missing_event_ids == ("missing",)
    assert evaluation.unexpected_event_ids == ("extra",)
    assert evaluation.max_abs_error_seconds == pytest.approx(0.02)


def test_evaluate_scheduled_events_raises_on_duplicate_ids() -> None:
    with pytest.raises(ValueError, match="Duplicate scheduled event ID"):
        evaluate_scheduled_events(
            (scheduled("a", 1.0), scheduled("a", 2.0)),
            {"a": 1.0},
            tolerance_seconds=0.01,
        )
