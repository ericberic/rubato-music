"""Metronome-loopback latency calibration.

The performer plays one key on each metronome click, locked by ear to the click
sound. Because a human synchronizes *sound-to-sound*, the delay between sending
a click and receiving the played note-on measures the composite output+input
loop we want the Yamaha output-advance to cancel.

One documented bias: humans anticipate a beat (negative mean asynchrony, ~20-50
ms), so the raw median slightly *under*-estimates the advance. The result is
therefore presented as a suggestion with its spread, not silently applied, and
the by-ear output-advance control remains the final arbiter.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from statistics import median
from typing import Protocol

import mido


@dataclass(frozen=True)
class CalibrationResult:
    """Aggregated round-trip offsets from one calibration run."""

    click_count: int
    matched_count: int
    offsets_ms: tuple[float, ...] = ()
    median_offset_ms: float | None = None
    mad_ms: float | None = None
    ci_half_width_ms: float | None = None
    confident: bool = False
    suggested_output_advance_ms: int | None = None
    message: str = ""


class _Port(Protocol):
    def send(self, message: mido.Message) -> None: ...

    def iter_pending(self): ...


def plan_click_times(start: float, tempo_bpm: float, beats: int) -> list[float]:
    """Absolute monotonic times for each metronome click."""

    if tempo_bpm <= 0:
        raise ValueError("tempo_bpm must be positive")
    if beats <= 0:
        raise ValueError("beats must be positive")
    period = 60.0 / tempo_bpm
    return [start + index * period for index in range(beats)]


def match_offsets(
    click_times: list[float],
    arrivals: list[float],
    *,
    match_window_seconds: float,
    warmup_beats: int = 1,
) -> list[float]:
    """Offset (arrival - click) for each click that has a nearby played note.

    Each click keeps the single closest arrival within the window; warm-up
    clicks are skipped so the first, still-settling beats do not bias the pace.
    """

    offsets: list[float] = []
    used: set[int] = set()
    for click_index, click in enumerate(click_times):
        if click_index < warmup_beats:
            continue
        best: tuple[float, int] | None = None
        for arrival_index, arrival in enumerate(arrivals):
            if arrival_index in used:
                continue
            distance = abs(arrival - click)
            if distance > match_window_seconds:
                continue
            if best is None or distance < abs(arrivals[best[1]] - click):
                best = (arrival, arrival_index)
        if best is not None:
            used.add(best[1])
            offsets.append((best[0] - click) * 1000.0)
    return offsets


def _mad(values: list[float], center: float) -> float:
    return median([abs(value - center) for value in values]) if values else 0.0


# Robust standard-deviation estimate from the median absolute deviation, and
# the 95% normal z. The 10 ms confidence target matches the output-advance
# knob's own step: a tighter interval estimates precision the control cannot
# express, so it is the natural stopping point rather than a smaller number.
_MAD_TO_SIGMA = 1.4826
_Z_95 = 1.96
CONFIDENCE_TARGET_CI_MS = 10.0
MIN_CONFIDENT_SAMPLES = 6


def summarize(
    offsets: list[float],
    *,
    click_count: int,
    min_samples: int = 4,
    max_mad_ms: float = 35.0,
    target_ci_ms: float = CONFIDENCE_TARGET_CI_MS,
    min_confident_samples: int = MIN_CONFIDENT_SAMPLES,
) -> CalibrationResult:
    """Robustly reduce offsets to a suggested output-advance in 10 ms steps.

    Reports the 95% confidence half-width of the mean offset (from a robust
    MAD-derived sigma) and a ``confident`` flag once that interval is inside the
    10 ms control step. Because central-limit precision shrinks as sqrt(n), the
    caller can keep playing until the interval is tight; a hard ``max_mad_ms``
    still rejects a run whose timing is too scattered to ever converge usefully.
    """

    matched = len(offsets)
    if matched < min_samples:
        return CalibrationResult(
            click_count=click_count,
            matched_count=matched,
            offsets_ms=tuple(offsets),
            message=(
                f"Only {matched} of {click_count} clicks matched a played note "
                f"(need at least {min_samples}). Play one key on each click and retry."
            ),
        )
    center = median(offsets)
    spread = _mad(offsets, center)
    # Drop gross outliers (a missed or double-struck beat) then re-center.
    kept = [value for value in offsets if abs(value - center) <= 3 * spread + 1e-9]
    if len(kept) >= min_samples:
        center = median(kept)
        spread = _mad(kept, center)
    else:
        kept = offsets
    sigma = _MAD_TO_SIGMA * spread
    ci_half_width = _Z_95 * sigma / (len(kept) ** 0.5)
    if spread > max_mad_ms:
        return CalibrationResult(
            click_count=click_count,
            matched_count=matched,
            offsets_ms=tuple(round(value, 2) for value in kept),
            median_offset_ms=round(center, 2),
            mad_ms=round(spread, 2),
            ci_half_width_ms=round(ci_half_width, 2),
            message=(
                f"Your timing scattered by ±{spread:.0f} ms — too much to trust the "
                "estimate. Play evenly to the click, no rubato, and measure again."
            ),
        )
    confident = len(kept) >= min_confident_samples and ci_half_width <= target_ci_ms
    # The measured loop is what we cancel; clamp to the control's 0-100 ms range
    # and snap to its 10 ms step. A negative median (rare, heavy anticipation)
    # clamps to 0 rather than suggesting a nonsensical delay.
    suggested = max(0, min(100, round(center / 10.0) * 10))
    if confident:
        message = (
            f"Suggested output advance {suggested} ms "
            f"(±{ci_half_width:.0f} ms, 95% over {len(kept)} beats). "
            "Anticipation makes this a lower bound; fine-tune by ear."
        )
    else:
        message = (
            f"Suggested {suggested} ms, but still ±{ci_half_width:.0f} ms at 95% "
            f"over {len(kept)} beats. Play a few more to tighten it, or apply and "
            "adjust by ear."
        )
    return CalibrationResult(
        click_count=click_count,
        matched_count=matched,
        offsets_ms=tuple(round(value, 2) for value in kept),
        median_offset_ms=round(center, 2),
        mad_ms=round(spread, 2),
        ci_half_width_ms=round(ci_half_width, 2),
        confident=confident,
        suggested_output_advance_ms=suggested,
        message=message,
    )


@dataclass
class _ClickVoice:
    pitch: int
    channel: int
    velocity: int
    note_off_seconds: float = 0.06
    pending_off: list[tuple[float, int, int]] = field(default_factory=list)


def measure_output_latency(
    *,
    output_port: _Port,
    input_port: _Port,
    tempo_bpm: float = 90.0,
    beats: int = 16,
    warmup_beats: int = 4,
    click_pitch: int = 76,
    click_channel: int = 9,
    click_velocity: int = 100,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    stop_event=None,
) -> CalibrationResult:
    """Play a fixed metronome sequence, capture note-ons, and summarize offsets.

    A ``warmup_beats`` count-in is discarded, then the remaining measured beats
    are aggregated. The count is fixed (not adaptive) so the performer knows
    exactly how long to keep pressing; the default 4 + 12 keeps the 95%
    confidence interval inside the 10 ms control step for ordinary playing,
    while the summary still reports the achieved interval and flags a retake if
    the timing scattered too much. The click is emitted on the same output port
    as the orchestra so it traverses the identical device path; input note-ons
    are timestamped on arrival with the same monotonic clock.
    """

    if beats <= warmup_beats:
        raise ValueError("beats must exceed warmup_beats")
    period = 60.0 / tempo_bpm
    match_window = period * 0.5
    voice = _ClickVoice(pitch=click_pitch, channel=click_channel, velocity=click_velocity)
    arrivals: list[float] = []
    click_times: list[float] = []

    def drain_input() -> None:
        for message in input_port.iter_pending():
            if message.type == "note_on" and message.velocity > 0:
                arrivals.append(now())

    def flush_due_offs(deadline: float) -> None:
        remaining: list[tuple[float, int, int]] = []
        for off_time, pitch, channel in voice.pending_off:
            if off_time <= deadline:
                output_port.send(mido.Message("note_off", note=pitch, channel=channel))
            else:
                remaining.append((off_time, pitch, channel))
        voice.pending_off = remaining

    def result() -> CalibrationResult:
        return summarize(
            match_offsets(
                click_times, arrivals, match_window_seconds=match_window, warmup_beats=warmup_beats
            ),
            click_count=len(click_times),
        )

    # Discard any stale queued input before the first click.
    drain_input()
    arrivals.clear()
    start = now()
    for beat in range(beats):
        target = start + beat * period
        while True:
            current = now()
            if stop_event is not None and stop_event.is_set():
                _silence(output_port, voice)
                return result()
            drain_input()
            flush_due_offs(current)
            if current >= target:
                break
            sleep(min(0.002, max(0.0, target - current)))
        sent_at = now()
        output_port.send(
            mido.Message(
                "note_on",
                note=voice.pitch,
                channel=voice.channel,
                velocity=voice.velocity,
            )
        )
        voice.pending_off.append((sent_at + voice.note_off_seconds, voice.pitch, voice.channel))
        click_times.append(sent_at)

    # Capture the final beat's response, then release any lingering click note.
    tail_end = now() + period
    while now() < tail_end:
        drain_input()
        flush_due_offs(now())
        sleep(0.002)
    _silence(output_port, voice)
    return result()


def _silence(output_port: _Port, voice: _ClickVoice) -> None:
    for _off_time, pitch, channel in voice.pending_off:
        output_port.send(mido.Message("note_off", note=pitch, channel=channel))
    voice.pending_off = []


__all__ = [
    "CalibrationResult",
    "match_offsets",
    "measure_output_latency",
    "plan_click_times",
    "summarize",
]
