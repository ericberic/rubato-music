"""Lock-free numeric mix-state gauges shared with a VST render process."""

from __future__ import annotations

import multiprocessing as mp
from ctypes import Structure, c_double, c_int64
from dataclasses import dataclass
from typing import Any


class _MixGauges(Structure):
    _fields_ = [
        ("sequence", c_int64),
        ("rendered_at", c_double),
        ("score_tick_start", c_int64),
        ("score_tick_end", c_int64),
        ("route_gain_start", c_double),
        ("route_gain_end", c_double),
        ("master_gain", c_double),
        ("effective_gain_start", c_double),
        ("effective_gain_end", c_double),
        ("output_rms", c_double),
        ("output_peak", c_double),
    ]


@dataclass(frozen=True)
class MixGaugeSample:
    instrument_id: str
    sequence: int
    rendered_at: float
    score_tick_start: int
    score_tick_end: int
    route_gain_start: float
    route_gain_end: float
    master_gain: float
    effective_gain_start: float
    effective_gain_end: float
    output_rms: float
    output_peak: float


class MixGaugeBlock:
    """One fixed shared-memory slot per configured instrument.

    The render process performs only plain numeric stores. Readers may observe a
    near-coherent cut while a block is being published; that is acceptable for
    sampled telemetry and self-corrects at the next observation.
    """

    def __init__(
        self,
        instrument_ids: tuple[str, ...],
        *,
        context: Any | None = None,
    ) -> None:
        if len(instrument_ids) != len(set(instrument_ids)):
            raise ValueError("mix gauge instrument ids must be unique")
        ctx = context or mp.get_context("spawn")
        self.instrument_ids = instrument_ids
        self._slots = {
            instrument_id: ctx.Value(_MixGauges, lock=False) for instrument_id in instrument_ids
        }

    def publish(
        self,
        instrument_id: str,
        *,
        rendered_at: float,
        score_tick_start: int,
        score_tick_end: int,
        route_gain_start: float,
        route_gain_end: float,
        master_gain: float,
        effective_gain_start: float,
        effective_gain_end: float,
        output_rms: float,
        output_peak: float,
    ) -> None:
        slot = self._slots[instrument_id]
        slot.rendered_at = rendered_at
        slot.score_tick_start = score_tick_start
        slot.score_tick_end = score_tick_end
        slot.route_gain_start = route_gain_start
        slot.route_gain_end = route_gain_end
        slot.master_gain = master_gain
        slot.effective_gain_start = effective_gain_start
        slot.effective_gain_end = effective_gain_end
        slot.output_rms = output_rms
        slot.output_peak = output_peak
        # Publish last so a changed sequence means the preceding values belong
        # to at least this render iteration.
        slot.sequence += 1

    def sample(self) -> tuple[MixGaugeSample, ...]:
        samples = []
        for instrument_id in self.instrument_ids:
            slot = self._slots[instrument_id]
            samples.append(
                MixGaugeSample(
                    instrument_id=instrument_id,
                    sequence=slot.sequence,
                    rendered_at=slot.rendered_at,
                    score_tick_start=slot.score_tick_start,
                    score_tick_end=slot.score_tick_end,
                    route_gain_start=slot.route_gain_start,
                    route_gain_end=slot.route_gain_end,
                    master_gain=slot.master_gain,
                    effective_gain_start=slot.effective_gain_start,
                    effective_gain_end=slot.effective_gain_end,
                    output_rms=slot.output_rms,
                    output_peak=slot.output_peak,
                )
            )
        return tuple(samples)


__all__ = ["MixGaugeBlock", "MixGaugeSample"]
