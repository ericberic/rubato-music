"""The transport machine must be total, and say why a cell is inert."""

from __future__ import annotations

import pytest

from aimusic.accompaniment.live_engine import LiveEngine, TransportAuthority
from aimusic.accompaniment.transport import (
    Inert,
    TransportEvent,
    TransportTable,
    validate_total,
)


def test_a_missing_cell_is_rejected_at_construction() -> None:
    """This is the bug class the table exists to prevent.

    FOLLOW once had no tick handler, so a pianist who simply stopped playing
    never triggered the coast contract. A partial table must not build.
    """

    cells = {
        (state, event): Inert("test")
        for state in TransportAuthority
        for event in TransportEvent
    }
    del cells[(TransportAuthority.FOLLOW, TransportEvent.TICK)]

    with pytest.raises(ValueError) as excinfo:
        TransportTable(TransportAuthority, cells)
    assert "follow" in str(excinfo.value)
    assert "tick" in str(excinfo.value)


def test_every_missing_cell_is_reported_at_once() -> None:
    with pytest.raises(ValueError) as excinfo:
        validate_total(TransportAuthority, {})
    message = str(excinfo.value)
    for state in TransportAuthority:
        assert state.value in message


def test_unknown_states_are_rejected() -> None:
    import enum

    class Other(enum.Enum):
        SOMETHING = "something"

    cells = {
        (state, event): Inert("test")
        for state in TransportAuthority
        for event in TransportEvent
    }
    cells[(Other.SOMETHING, TransportEvent.TICK)] = Inert("bogus")
    with pytest.raises(ValueError, match="unknown states"):
        TransportTable(TransportAuthority, cells)


def test_the_live_engine_declares_a_total_machine(tmp_path) -> None:
    """Every authority answers both events, and inert cells carry a reason."""

    from aimusic.accompaniment.runtime_contracts import RuntimeConfig
    from aimusic.accompaniment.runtime_io import CapturingOutput, ManualClock
    from aimusic.accompaniment.following import OracleFollower
    from aimusic.accompaniment.score_bundle import ScoreBundle

    engine = LiveEngine.from_bundle(
        bundle=ScoreBundle.load(
            "tests/fixtures/score_bundles/synthetic_accompaniment_capability"
        ),
        config=RuntimeConfig(run_id="table-test"),
        clock=ManualClock(0.0),
        follower=OracleFollower(),
        output=CapturingOutput(),
    )
    table = engine._transport_table

    for state in TransportAuthority:
        for event in TransportEvent:
            table.handler(state, event)  # raises KeyError if undeclared
            if table.is_inert(state, event):
                assert table.reason(state, event), (
                    f"{state.value} x {event.value} is inert without a stated reason"
                )

    rendered = table.describe()
    assert rendered.count("\n") + 1 == len(TransportAuthority) * len(TransportEvent)
