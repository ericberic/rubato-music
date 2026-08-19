"""Tests for the `WS /api/events` payload models (issue #85, design doc §2.1).

`events.publish()` now takes one of these typed models instead of a hand-
built dict literal (see tests/test_events_ws.py and
tests/takes/test_aligner_events.py for the publish-side behavior); this
covers the models' own round-trip and discriminated-union parsing.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from aimusic.server.schemas import (
    LiveFollowStartRequest,
    LiveReplayStartRequest,
    TakeAlignmentDone,
    TakeEvent,
    TakeRecordingStarted,
    TakeRecordingStopped,
    TakeReviewStatusEvent,
)

_event_adapter: TypeAdapter[TakeEvent] = TypeAdapter(TakeEvent)


def test_take_recording_started_round_trips_through_the_union() -> None:
    event = TakeRecordingStarted(
        type="take:recording_started", take_id="t1", piece_id="chopin_op11", movement=2
    )
    parsed = _event_adapter.validate_json(event.model_dump_json())
    assert parsed == event


def test_take_recording_stopped_round_trips_through_the_union() -> None:
    event = TakeRecordingStopped(
        type="take:recording_stopped",
        take_id="t1",
        piece_id="chopin_op11",
        movement=2,
        duration_seconds=12.5,
    )
    parsed = _event_adapter.validate_json(event.model_dump_json())
    assert parsed == event


def test_take_alignment_done_round_trips_through_the_union() -> None:
    event = TakeAlignmentDone(
        type="take:alignment_done",
        take_id="t1",
        piece_id="chopin_op11",
        movement=2,
        status="aligned",
        score_start_beat=10.0,
        score_end_beat=14.0,
    )
    parsed = _event_adapter.validate_json(event.model_dump_json())
    assert parsed == event
    assert isinstance(parsed, TakeAlignmentDone)


def test_take_review_status_round_trips_through_the_union() -> None:
    event = TakeReviewStatusEvent(
        type="take:review_status",
        take_id="t1",
        piece_id="chopin_op11",
        movement=2,
        state="ready",
        job_id="job-1",
        midi_url="/api/takes/t1/review/midi",
    )
    parsed = _event_adapter.validate_json(event.model_dump_json())
    assert parsed == event
    assert isinstance(parsed, TakeReviewStatusEvent)


def test_type_is_required_not_defaulted() -> None:
    """`type` lost its Python-side default (see the comment on the model
    definitions in `aimusic.server.schemas`): a discriminator field with a
    default is `optional` in the emitted JSON Schema, which broke the
    generated Zod discriminated union (issue #83/#84) -- Zod v4 couldn't
    extract a literal value through the `optional` wrapper and read every
    event's `type` as `undefined`. Constructing an event without `type` must
    fail loudly, not silently default.
    """

    with pytest.raises(ValidationError):
        TakeRecordingStarted(take_id="t1", piece_id="chopin_op11", movement=2)  # type: ignore[call-arg]


def test_unknown_event_type_raises_validation_error() -> None:
    """The discriminator (`type`) must reject an unknown event kind.
    """

    with pytest.raises(ValidationError):
        _event_adapter.validate_json(
            '{"type": "take:something_else", "take_id": "t1", '
            '"piece_id": "chopin_op11", "movement": 2}'
        )


def test_take_alignment_done_malformed_status_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        _event_adapter.validate_json(
            '{"type": "take:alignment_done", "take_id": "t1", "piece_id": "chopin_op11", '
            '"movement": 2, "status": "not_a_real_status"}'
        )


def test_live_start_requests_use_bundle_identity_not_client_paths() -> None:
    replay_schema = LiveReplayStartRequest.model_json_schema()
    follow_schema = LiveFollowStartRequest.model_json_schema()

    assert {"bundle_id", "revision"}.issubset(replay_schema["properties"])
    assert {"bundle_id", "revision"}.issubset(follow_schema["properties"])
    assert "bundle_root" not in replay_schema["properties"]
    assert "bundle_root" not in follow_schema["properties"]
    assert "score_file" not in follow_schema["properties"]
