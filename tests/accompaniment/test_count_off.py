from __future__ import annotations

import mido
import pytest

from aimusic.accompaniment.count_off import play_count_off
from aimusic.accompaniment.runtime_io import ManualClock, MemoryTraceSink
from aimusic.accompaniment.section_policy import AccompanimentMode


class _Port:
    def __init__(self) -> None:
        self.messages: list[tuple[float, mido.Message]] = []
        self.clock: ManualClock | None = None

    def send_count_off(self, message: mido.Message, *, sent_at: float) -> None:
        assert self.clock is not None
        assert sent_at == self.clock.now()
        self.messages.append((self.clock.now(), message.copy()))


def test_count_off_leaves_one_period_to_the_selected_entry() -> None:
    clock = ManualClock(10.0)
    port = _Port()
    port.clock = clock
    trace = MemoryTraceSink()

    entry_at = play_count_off(
        output=port,
        clock=clock,
        trace_sink=trace,
        tempo_bpm=120.0,
        entry_score_beat=172.0,
        entry_reference_beat=181.25,
        entry_section_mode=AccompanimentMode.FOLLOW,
    )

    note_ons = [(at, message) for at, message in port.messages if message.type == "note_on"]
    assert [at for at, _message in note_ons] == [10.0, 10.5, 11.0, 11.5]
    assert note_ons[0][1].note != note_ons[1][1].note
    assert entry_at == 12.0
    assert clock.now() < entry_at
    assert len(trace.records) == 4
    assert all(row.type == "count_off" for row in trace.records)
    assert trace.records[-1].entry_perf_time == 12.0
    assert trace.records[-1].entry_score_beat == 172.0
    assert trace.records[-1].entry_reference_beat == 181.25
    assert trace.records[-1].entry_section_mode is AccompanimentMode.FOLLOW


class _FailingClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.waits = 0

    def now(self) -> float:
        return self.value

    def wait_until(self, target: float) -> None:
        self.value = target
        self.waits += 1
        if self.waits == 2:
            raise RuntimeError("clock failed")


def test_count_off_releases_click_when_wait_fails() -> None:
    clock = _FailingClock()
    port = _Port()
    port.clock = clock  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="clock failed"):
        play_count_off(
            output=port,
            clock=clock,
            trace_sink=MemoryTraceSink(),
            tempo_bpm=120.0,
            entry_score_beat=0.0,
            entry_reference_beat=0.0,
            entry_section_mode=AccompanimentMode.FOLLOW,
        )

    assert [message.type for _at, message in port.messages] == ["note_on", "note_off"]
