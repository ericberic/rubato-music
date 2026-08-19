"""Deterministic tests for metronome-loopback latency calibration."""

from __future__ import annotations

import mido
import pytest

from aimusic.accompaniment.latency_calibration import (
    match_offsets,
    measure_output_latency,
    plan_click_times,
    summarize,
)


def test_plan_click_times_are_evenly_spaced() -> None:
    times = plan_click_times(10.0, tempo_bpm=60.0, beats=4)
    assert times == [10.0, 11.0, 12.0, 13.0]


def test_match_offsets_pairs_nearest_note_and_skips_warmup() -> None:
    clicks = [0.0, 1.0, 2.0, 3.0]
    # Each played note lands 40 ms after its click; warm-up drops the first two.
    arrivals = [0.04, 1.04, 2.04, 3.04]
    offsets = match_offsets(clicks, arrivals, match_window_seconds=0.5, warmup_beats=2)
    assert offsets == pytest.approx([40.0, 40.0])


def test_match_offsets_ignores_notes_outside_window() -> None:
    clicks = [0.0, 2.0]
    arrivals = [0.7]  # more than half a beat from either click
    assert match_offsets(clicks, arrivals, match_window_seconds=0.5, warmup_beats=0) == []


def test_summarize_suggests_snapped_advance_and_rejects_outliers() -> None:
    offsets = [38.0, 41.0, 39.0, 42.0, 40.0, 400.0]  # last is a fumbled beat
    result = summarize(offsets, click_count=8)
    assert result.matched_count == 6
    assert result.median_offset_ms == 40.0
    assert result.suggested_output_advance_ms == 40
    assert 400.0 not in result.offsets_ms


def test_summarize_requests_retake_when_timing_scatters() -> None:
    # Wildly inconsistent timing: median is ~42 but far too scattered to trust.
    offsets = [0.0, 85.0, 5.0, 80.0, 40.0, 45.0, -5.0, 90.0]
    result = summarize(offsets, click_count=16)
    assert result.suggested_output_advance_ms is None
    assert result.mad_ms is not None and result.mad_ms > 35.0
    assert "again" in result.message.lower()


def test_summarize_reports_confidence_interval_and_flag() -> None:
    # 12 tight beats around 40 ms → a narrow 95% interval, flagged confident.
    offsets = [38.0, 41.0, 39.0, 42.0, 40.0, 41.0, 39.0, 40.0, 41.0, 39.0, 42.0, 38.0]
    result = summarize(offsets, click_count=16)
    assert result.suggested_output_advance_ms == 40
    assert result.ci_half_width_ms is not None and result.ci_half_width_ms <= 10.0
    assert result.confident is True


def test_summarize_flags_not_confident_when_interval_is_wide() -> None:
    # Moderate scatter under the retake gate: a value is still suggested, but
    # the interval is too wide to call confident.
    offsets = [20.0, 60.0, 25.0, 55.0, 40.0, 45.0, 30.0, 50.0]
    result = summarize(offsets, click_count=16)
    assert result.suggested_output_advance_ms is not None
    assert result.confident is False
    assert result.ci_half_width_ms is not None and result.ci_half_width_ms > 10.0


def test_summarize_reports_insufficient_matches() -> None:
    result = summarize([40.0, 41.0], click_count=8)
    assert result.suggested_output_advance_ms is None
    assert "at least" in result.message


def test_summarize_clamps_negative_median_to_zero() -> None:
    result = summarize([-30.0, -28.0, -32.0, -29.0], click_count=6)
    assert result.suggested_output_advance_ms == 0


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(seconds, 1e-4)


class _FakeInput:
    """Emits one note-on as the clock passes each scheduled arrival time."""

    def __init__(self, clock: _FakeClock, arrivals: list[float]) -> None:
        self._clock = clock
        self._arrivals = sorted(arrivals)
        self._index = 0

    def iter_pending(self):
        emitted = []
        while (
            self._index < len(self._arrivals)
            and self._arrivals[self._index] <= self._clock.now()
        ):
            emitted.append(mido.Message("note_on", note=60, velocity=64))
            self._index += 1
        return emitted

    def close(self) -> None:  # pragma: no cover - parity with real ports
        pass


class _FakeOutput:
    def __init__(self) -> None:
        self.messages: list[mido.Message] = []

    def send(self, message: mido.Message) -> None:
        self.messages.append(message)

    def close(self) -> None:  # pragma: no cover - parity with real ports
        pass


def test_measure_output_latency_recovers_a_known_offset() -> None:
    clock = _FakeClock()
    # Standard protocol: 16 clicks at 90 BPM (period ~0.667 s), 4-beat count-in
    # leaving 12 measured. Each played note lands 40 ms after its click.
    period = 60.0 / 90.0
    arrivals = [beat * period + 0.04 for beat in range(16)]
    output = _FakeOutput()
    inp = _FakeInput(clock, arrivals)

    result = measure_output_latency(
        output_port=output,
        input_port=inp,
        tempo_bpm=90.0,
        beats=16,
        warmup_beats=4,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert result.click_count == 16
    assert result.matched_count == 12
    assert result.suggested_output_advance_ms == 40
    assert result.confident is True
    assert result.median_offset_ms is not None
    assert abs(result.median_offset_ms - 40.0) <= 3.0
    assert any(message.type == "note_on" for message in output.messages)
    assert any(message.type == "note_off" for message in output.messages)
