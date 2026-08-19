"""Drive the live runtime without a Yamaha: fixtures, replay, load, scorecard.

The point is that the runtime can be exercised, profiled, and regression-tested
by anyone (including an agent) with no hardware attached:

* ``VirtualInputPort`` satisfies the same ``iter_pending()`` contract as a mido
  input, so it drops into ``LiveRuntimeManager(input_factory=...)`` untouched.
  In wall-clock mode it releases notes at their real arrival times, so latency
  and cadence measurements mean the same thing they would live.
* Generators build note streams with controllable density, chord size, tempo and
  bursts — including deliberately hostile ones for load testing.
* ``notes_from_trace`` replays a real recorded take, so fixes are validated
  against the performance that actually failed.
* ``score_run`` reduces a finished run's trace to pass/fail against the
  contracts in ``docs/decisions/0011-realtime-multiprocess-architecture.md``.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Iterable

import mido

from aimusic.accompaniment.runtime_contracts import MixStateTrace, TelemetryLevel
from aimusic.accompaniment.runtime_io import TraceSink
from aimusic.mixing.models import ZoneConfig, ZoneHealth
from aimusic.mixing.policy import MixPolicy


@dataclass(frozen=True)
class InjectedNote:
    """One note to deliver at ``perf_time`` seconds after the port starts."""

    perf_time: float
    pitch: int
    velocity: int = 80


class VirtualInputPort:
    """A mido-shaped input port fed from a scripted note list.

    ``realtime=True`` releases each note when its wall-clock moment arrives (the
    load-test mode: cadence and latency are physically meaningful).
    ``realtime=False`` releases everything immediately on the first drain, for
    fast deterministic tests driven by a manual clock.
    """

    def __init__(
        self,
        notes: Iterable[InjectedNote],
        *,
        realtime: bool = True,
        clock: Any | None = None,
    ) -> None:
        self._notes = sorted(notes, key=lambda n: n.perf_time)
        self._index = 0
        self._realtime = realtime
        self._clock = clock or time
        self._start: float | None = None

    def __enter__(self) -> "VirtualInputPort":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def exhausted(self) -> bool:
        return self._index >= len(self._notes)

    def iter_pending(self) -> list[mido.Message]:
        if self._start is None:
            self._start = self._now()
        due: list[mido.Message] = []
        elapsed = self._now() - self._start
        while self._index < len(self._notes):
            note = self._notes[self._index]
            if self._realtime and note.perf_time > elapsed:
                break
            due.append(mido.Message("note_on", note=note.pitch, velocity=note.velocity))
            self._index += 1
        return due

    def _now(self) -> float:
        now = getattr(self._clock, "now", None)
        return now() if callable(now) else self._clock.monotonic()

    def close(self) -> None:
        self._index = len(self._notes)


class CapturingMidiPort:
    """In-process software MIDI sink used instead of a Yamaha or IAC bus."""

    def __init__(self) -> None:
        self.messages: list[mido.Message] = []

    def send(self, message: mido.Message) -> None:
        self.messages.append(message.copy())

    def close(self) -> None:
        pass


class MixProbeRouter:
    """Deterministic unit-signal renderer for a causal mix load test.

    A real software synth is useful for listening, but a unit signal is more
    useful in CI: its observed RMS/peak is exactly the applied gain. This router
    sits behind the same ``MultiZoneAccompanimentOutput`` interface as the VST
    workers and records score-positioned mix observations without audio hardware.
    """

    def __init__(
        self,
        policy: MixPolicy,
        trace_sink: TraceSink,
        *,
        telemetry_level: TelemetryLevel = TelemetryLevel.TRACE,
        sample_interval_seconds: float = 0.05,
    ) -> None:
        if sample_interval_seconds <= 0:
            raise ValueError("mix probe sample interval must be positive")
        self._policy = policy
        self._trace_sink = trace_sink
        self._telemetry_level = telemetry_level
        self._sample_interval_seconds = sample_interval_seconds
        self._master_gain = 1.0
        self._last_sample_at: float | None = None
        self.sent_events = 0
        identities = {
            (route.active_zone_id, stem_id)
            for route in policy.default_routes
            for stem_id in route.stem_ids
            if route.active_zone_id not in {None, "yamaha_anchor"}
        }
        identities.update(
            (route.active_zone_id, stem_id)
            for region in policy.regions
            for route in region.routes
            for stem_id in route.stem_ids
            if route.active_zone_id not in {None, "yamaha_anchor"}
        )
        self._identities = tuple(sorted(identities))

    def send(self, event: Any, *, sent_at: float) -> None:  # noqa: ARG002
        self.sent_events += 1

    def cancel_pending(self, event_ids: Iterable[str]) -> bool:  # noqa: ARG002
        return True

    def retime_release(self, event_id: str, *, scheduled_at: float) -> bool:  # noqa: ARG002
        return True

    def panic(self, *, sent_at: float, reason: str) -> None:  # noqa: ARG002
        pass

    def set_master_volume(self, volume: float, *, sent_at: float) -> None:  # noqa: ARG002
        self._master_gain = volume

    def set_output_advance(self, output_advance_ms: float) -> None:  # noqa: ARG002
        pass

    def apply_mix_automation(self, score_tick: int, *, sent_at: float) -> None:
        if self._telemetry_level != TelemetryLevel.TRACE:
            return
        if (
            self._last_sample_at is not None
            and sent_at - self._last_sample_at < self._sample_interval_seconds
        ):
            return
        self._last_sample_at = sent_at
        for zone_id, stem_id in self._identities:
            route_gain = (
                self._policy.audio_gain_at(zone_id, stem_id, score_tick)
                if stem_id == "orchestra"
                else self._policy.audio_gain_for_part_at(zone_id, stem_id, score_tick)
            )
            effective = route_gain * self._master_gain
            self._trace_sink.write(
                MixStateTrace(
                    type="mix_state",
                    monotonic_time=sent_at,
                    rendered_at=sent_at,
                    renderer="unit_signal_probe",
                    mix_program_id=self._policy.program_id,
                    mix_program_revision=self._policy.program_revision,
                    zone_id=zone_id,
                    instrument_id=stem_id,
                    score_tick_start=score_tick,
                    score_tick_end=score_tick,
                    route_gain_start=route_gain,
                    route_gain_end=route_gain,
                    master_gain=self._master_gain,
                    effective_gain_start=effective,
                    effective_gain_end=effective,
                    output_rms=effective,
                    output_peak=effective,
                )
            )

    def close(self) -> None:
        pass


def virtual_mix_zones(_audio_config: Any = None) -> tuple[ZoneConfig, ...]:
    """Ready, hardware-free versions of the artistic zones used by programs."""

    return tuple(
        ZoneConfig(
            zone_id=zone_id,
            label=label,
            renderer_id="virtual",
            acoustic_position=position,
            configured_output_advance_ms=0,
            residual_error_p95_ms=0,
            calibration_revision="virtual-v1",
            health=ZoneHealth.READY,
        )
        for zone_id, label, position in (
            ("yamaha_anchor", "Software Yamaha", "piano side"),
            ("room_center", "Virtual room", "center"),
            ("opposite_solo", "Virtual opposite solo", "opposite side"),
        )
    )


# --------------------------------------------------------------------------- #
# Fixture generators
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StreamSpec:
    """Shape of a synthetic performance."""

    name: str
    seconds: float = 20.0
    tempo_bpm: float = 55.0
    notes_per_beat: float = 8.0
    chord_size: int = 1
    chord_every: int = 0  # every Nth event becomes a chord (0 disables)
    chord_spread_ms: float = 0.0  # 0 = struck exactly together
    tempo_drift: float = 0.0  # fractional tempo change across the take
    burst_factor: float = 1.0  # density multiplier during bursts
    burst_every_seconds: float = 0.0  # 0 disables bursts
    burst_seconds: float = 1.0
    gap_every_seconds: float = 0.0  # silence, to exercise coast/relock
    gap_seconds: float = 0.0
    seed: int = 7
    pitches: tuple[int, ...] = field(default=(59, 63, 66, 71, 75, 78, 83))


def generate_notes(spec: StreamSpec) -> list[InjectedNote]:
    """Deterministically synthesize a note stream from a spec."""

    rng = random.Random(spec.seed)
    notes: list[InjectedNote] = []
    t = 0.0
    index = 0
    while t < spec.seconds:
        if spec.gap_every_seconds and (t % spec.gap_every_seconds) < 1e-9 and t > 0:
            t += spec.gap_seconds
            continue
        progress = t / spec.seconds if spec.seconds else 0.0
        tempo = spec.tempo_bpm * (1.0 + spec.tempo_drift * progress)
        density = spec.notes_per_beat
        if spec.burst_every_seconds and (t % spec.burst_every_seconds) < spec.burst_seconds:
            density *= spec.burst_factor
        interval = 60.0 / max(1e-6, tempo * density)

        is_chord = spec.chord_every and index % spec.chord_every == 0
        size = spec.chord_size if is_chord else 1
        base = rng.choice(spec.pitches)
        for voice in range(size):
            offset = rng.uniform(0, spec.chord_spread_ms / 1000.0) if spec.chord_spread_ms else 0.0
            notes.append(
                InjectedNote(
                    perf_time=t + offset,
                    pitch=min(108, base + voice * 4),
                    velocity=rng.randint(55, 95),
                )
            )
        t += interval
        index += 1
    return notes


#: Scenarios worth running on every change. "chords" proves simultaneity
#: survives; "storm" is deliberately hostile; "dropouts" exercises coast/relock.
SCENARIOS: dict[str, StreamSpec] = {
    "sparse": StreamSpec(name="sparse", notes_per_beat=2.0, seconds=15.0),
    "typical": StreamSpec(name="typical", notes_per_beat=8.0, seconds=20.0),
    "chords": StreamSpec(
        name="chords", notes_per_beat=4.0, chord_size=3, chord_every=2, seconds=20.0
    ),
    "storm": StreamSpec(
        name="storm",
        notes_per_beat=16.0,
        chord_size=4,
        chord_every=3,
        burst_every_seconds=4.0,
        burst_factor=2.5,
        seconds=25.0,
    ),
    "dropouts": StreamSpec(
        name="dropouts", notes_per_beat=8.0, gap_every_seconds=5.0, gap_seconds=2.0
    ),
    "ramp": StreamSpec(name="ramp", notes_per_beat=8.0, tempo_drift=0.6, seconds=25.0),
}


def notes_from_trace(trace_path: Path | str, *, origin: float | None = None) -> list[InjectedNote]:
    """Replay a real take: rebuild its note stream from a runtime trace.

    Only the small ``input`` rows are read, so a fixture can be derived from a
    recorded performance without copying the whole (private) trace around.

    The origin is the run's **start**, not the first note. That silence matters:
    in an orchestra-led cue-in the pianist deliberately waits several seconds
    while the orchestra plays in, and collapsing it makes the piano enter at a
    score position the orchestra has not reached yet — the follower then cannot
    localize and the scheduler thrashes. Replaying from run start reproduces the
    real entry.
    """

    rows = [
        json.loads(line)
        for line in Path(trace_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    inputs = [r for r in rows if r.get("type") == "input"]
    if not inputs:
        return []
    start = next(
        (r["monotonic_time"] for r in rows if r.get("type") == "runtime_start"),
        None,
    )
    base = (
        origin
        if origin is not None
        else (start if start is not None else min(r["perf_time"] for r in inputs))
    )
    return [
        InjectedNote(
            perf_time=max(0.0, r["perf_time"] - base),
            pitch=r["pitch"],
            velocity=r.get("velocity", 80),
        )
        for r in inputs
    ]


# --------------------------------------------------------------------------- #
# Scorecard
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Check:
    name: str
    value: float | None
    limit: float | None
    passed: bool
    detail: str = ""

    def line(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        value = "n/a" if self.value is None else f"{self.value:.2f}"
        limit = "" if self.limit is None else f" (limit {self.limit:g})"
        return f"  [{mark}] {self.name}: {value}{limit} {self.detail}".rstrip()


@dataclass(frozen=True)
class Scorecard:
    run_id: str
    checks: tuple[Check, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def report(self) -> str:
        head = f"scorecard {self.run_id}: {'PASS' if self.passed else 'FAIL'}"
        return "\n".join([head, *(check.line() for check in self.checks)])


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def score_run(
    trace_path: Path | str,
    *,
    require_mix: bool = False,
    require_mix_change: bool = False,
) -> Scorecard:
    """Reduce a finished run's trace to contract checks."""

    rows = [
        json.loads(line)
        for line in Path(trace_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    run_id = next(
        (r["status"]["run_id"] for r in rows if r.get("type") == "state"), str(trace_path)
    )
    checks: list[Check] = []

    # Chord simultaneity: notes struck together must stay together.
    inputs = sorted(r["perf_time"] for r in rows if r.get("type") == "input")
    gaps = [(b - a) * 1000 for a, b in zip(inputs, inputs[1:])]
    min_gap = min(gaps) if gaps else None
    # Only meaningful when the stream actually contains near-simultaneous notes:
    # a monophonic passage legitimately has no sub-5 ms pair, and asserting one
    # marked valid runs as failures. Chords are detected as any pair inside a
    # plausible hand-span window; if there are none, the check does not apply.
    has_chords = any(gap < 40.0 for gap in gaps)
    checks.append(
        Check(
            name="input.min_interval_ms",
            value=min_gap,
            limit=None,
            passed=(not has_chords) or (min_gap is not None and min_gap < 5.0),
            detail=(
                "chords present: struck notes must stay simultaneous"
                if has_chords
                else "no chords in this stream - not applicable"
            ),
        )
    )

    # Per-loop cadence and work.
    timing = [r for r in rows if r.get("type") == "loop_timing"]
    for loop, work_limit, interval_limit in (
        ("midi_input", 5.0, 20.0),
        ("process_note", 20.0, None),
        ("processing", 10.0, 50.0),
    ):
        windows = [r for r in timing if r["loop"] == loop]
        work = [r["work_ms_p95"] for r in windows if r.get("work_ms_p95") is not None]
        worst_work = max(work) if work else None
        checks.append(
            Check(
                name=f"{loop}.work_ms_p95",
                value=worst_work,
                limit=work_limit,
                passed=worst_work is None or worst_work <= work_limit,
            )
        )
        if interval_limit is not None:
            intervals = [
                r["interval_ms_max"] for r in windows if r.get("interval_ms_max") is not None
            ]
            worst = max(intervals) if intervals else None
            checks.append(
                Check(
                    name=f"{loop}.interval_ms_max",
                    value=worst,
                    limit=interval_limit,
                    passed=worst is None or worst <= interval_limit,
                    detail="stall detector",
                )
            )

    # Output delivery.
    lateness = [
        r["output_lateness_ms"]
        for r in rows
        if r.get("type") == "midi_output" and r.get("output_lateness_ms") is not None
    ]
    p95 = _percentile(lateness, 0.95)
    checks.append(
        Check(
            name="output.lateness_ms_p95",
            value=p95,
            limit=75.0,
            passed=p95 is None or p95 <= 75.0,
        )
    )

    # Stability.
    # Stopping a run panics by design (all-notes-off); only unplanned panics --
    # a score jump or a failure mid-performance -- indicate a defect.
    # Panics that are part of a designed transition, not a failure: stopping a
    # run, closing the port, and reaching an authored hold (which silences
    # sustained orchestra notes on purpose -- including via the FOLLOW silence
    # timeout at the end of a replay).
    shutdown = {"user_stop", "output_close", "runtime_stop", "section_hold"}
    panics = [
        r
        for r in rows
        if r.get("type") == "midi_output"
        and r.get("action") == "panic"
        and r.get("reason") not in shutdown
    ]
    checks.append(
        Check(
            name="output.unplanned_panics",
            value=float(len(panics)),
            limit=0,
            passed=not panics,
            detail=", ".join(sorted({str(r.get("reason")) for r in panics})),
        )
    )

    follower_rows = [r for r in rows if r.get("type") == "follower"]
    latencies = [
        r["processing_latency_ms"]
        for r in follower_rows
        if r.get("processing_latency_ms") is not None
    ]
    # p95, not median: a median hides a tail where a large minority of notes take
    # far longer, which is exactly the HMM's failure shape.
    follower_p95 = _percentile(latencies, 0.95)
    checks.append(
        Check(
            name="follower.latency_ms_p95",
            value=follower_p95,
            limit=20.0,
            passed=follower_p95 is None or follower_p95 <= 20.0,
            detail=(f"median {median(latencies):.1f} ms" if latencies else ""),
        )
    )

    mix_rows = [row for row in rows if row.get("type") == "mix_state"]
    if require_mix or mix_rows:
        checks.append(
            Check(
                name="mix.observed_samples",
                value=float(len(mix_rows)),
                limit=1,
                passed=bool(mix_rows),
                detail="off-path gain observations",
            )
        )
    if mix_rows:
        observer_delays = [
            max(0.0, (row["monotonic_time"] - row["rendered_at"]) * 1000) for row in mix_rows
        ]
        delay_p95 = _percentile(observer_delays, 0.95)
        checks.append(
            Check(
                name="mix.observer_delay_ms_p95",
                value=delay_p95,
                limit=250.0,
                passed=delay_p95 is not None and delay_p95 <= 250.0,
            )
        )
        probe_rows = [row for row in mix_rows if row.get("renderer") == "unit_signal_probe"]
        if probe_rows:
            signal_error = max(
                abs(row["output_rms"] - row["effective_gain_end"]) for row in probe_rows
            )
            checks.append(
                Check(
                    name="mix.unit_signal_gain_error",
                    value=signal_error,
                    limit=1e-6,
                    passed=signal_error <= 1e-6,
                    detail="captured signal matches applied gain",
                )
            )
        if require_mix_change:
            gains_by_route: dict[tuple[str, str], list[float]] = {}
            for row in mix_rows:
                gains_by_route.setdefault((row["zone_id"], row["instrument_id"]), []).append(
                    row["effective_gain_end"]
                )
            largest_change = max(
                (max(values) - min(values) for values in gains_by_route.values()),
                default=0.0,
            )
            checks.append(
                Check(
                    name="mix.observed_gain_change",
                    value=largest_change,
                    limit=None,
                    passed=largest_change > 1e-3,
                    detail="authored automation changed at least one route",
                )
            )
    return Scorecard(run_id=run_id, checks=tuple(checks))


__all__ = [
    "Check",
    "CapturingMidiPort",
    "InjectedNote",
    "MixProbeRouter",
    "SCENARIOS",
    "Scorecard",
    "StreamSpec",
    "VirtualInputPort",
    "generate_notes",
    "notes_from_trace",
    "score_run",
    "virtual_mix_zones",
]
