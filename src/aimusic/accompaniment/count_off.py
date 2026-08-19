"""Audible one-bar count-off for a selected live-performance entry."""

from __future__ import annotations

from typing import Protocol

import mido

from aimusic.accompaniment.runtime_contracts import CountOffTrace
from aimusic.accompaniment.runtime_io import MonotonicClock, TraceSink
from aimusic.accompaniment.section_policy import AccompanimentMode

DEFAULT_COUNT_OFF_BEATS = 4
_CLICK_CHANNEL = 9
_CLICK_PITCH = 76
_ACCENT_PITCH = 77
_CLICK_VELOCITY = 92
_ACCENT_VELOCITY = 112
_CLICK_DURATION_SECONDS = 0.06


class CountOffOutput(Protocol):
    def send_count_off(self, message: mido.Message, *, sent_at: float) -> None: ...


def play_count_off(
    *,
    output: CountOffOutput,
    clock: MonotonicClock,
    trace_sink: TraceSink,
    tempo_bpm: float,
    entry_score_beat: float,
    entry_reference_beat: float | None,
    entry_section_mode: AccompanimentMode,
    beat_count: int = DEFAULT_COUNT_OFF_BEATS,
    stop_event=None,
) -> float:
    """Play fixed quarter-note clicks and return the following downbeat time.

    The last click is beat ``beat_count`` of the count-off. The selected score
    entry is one period later, leaving that final period for the engine to seed
    canonical/reference state and transfer its immutable deadline prefix.
    """

    if tempo_bpm <= 0:
        raise ValueError("tempo_bpm must be positive")
    if beat_count <= 0:
        raise ValueError("beat_count must be positive")
    if entry_score_beat < 0:
        raise ValueError("entry_score_beat must be non-negative")

    period = 60.0 / tempo_bpm
    start = clock.now()
    entry_perf_time = start + beat_count * period
    for index in range(beat_count):
        if stop_event is not None and stop_event.is_set():
            return clock.now()
        target = start + index * period
        clock.wait_until(target)
        accent = index == 0
        pitch = _ACCENT_PITCH if accent else _CLICK_PITCH
        output.send_count_off(
            mido.Message(
                "note_on",
                channel=_CLICK_CHANNEL,
                note=pitch,
                velocity=_ACCENT_VELOCITY if accent else _CLICK_VELOCITY,
            ),
            sent_at=clock.now(),
        )
        try:
            sent_at = clock.now()
            trace_sink.write(
                CountOffTrace(
                    type="count_off",
                    monotonic_time=sent_at,
                    target_perf_time=target,
                    beat_index=index + 1,
                    beat_count=beat_count,
                    tempo_bpm=tempo_bpm,
                    entry_perf_time=entry_perf_time,
                    entry_score_beat=entry_score_beat,
                    entry_reference_beat=entry_reference_beat,
                    entry_section_mode=entry_section_mode,
                )
            )
            clock.wait_until(min(target + _CLICK_DURATION_SECONDS, entry_perf_time))
        finally:
            output.send_count_off(
                mido.Message(
                    "note_off",
                    channel=_CLICK_CHANNEL,
                    note=pitch,
                    velocity=0,
                ),
                sent_at=clock.now(),
            )
    return entry_perf_time


__all__ = ["DEFAULT_COUNT_OFF_BEATS", "play_count_off"]
