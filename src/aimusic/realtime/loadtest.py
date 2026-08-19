"""Run the live runtime under synthetic or replayed load, with no hardware.

Drives the real ``LiveRuntimeManager`` (real follower, tempo model, scheduler and
deadline output) from a :class:`~aimusic.realtime.harness.VirtualInputPort`, into
a null MIDI sink, then scores the resulting trace against the contracts in
``docs/decisions/0011-realtime-multiprocess-architecture.md``.

Usage::

    uv run python -m aimusic.realtime.loadtest --scenario storm
    uv run python -m aimusic.realtime.loadtest --trace /path/to/runtime.jsonl
    uv run python -m aimusic.realtime.loadtest --list
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import mido

from aimusic.accompaniment.runtime_contracts import RuntimeConfig, TelemetryLevel
from aimusic.accompaniment.runtime_io import TraceSink
from aimusic.audio.live_config import LiveAudioConfig
from aimusic.core import paths
from aimusic.mixing.policy import MixPolicy
from aimusic.realtime.harness import (
    SCENARIOS,
    CapturingMidiPort,
    InjectedNote,
    MixProbeRouter,
    VirtualInputPort,
    generate_notes,
    notes_from_trace,
    score_run,
    virtual_mix_zones,
)


def _probe_router_factory(
    _audio_config: LiveAudioConfig,
    policy: MixPolicy,
    trace_sink: TraceSink,
    telemetry_level: TelemetryLevel,
) -> MixProbeRouter:
    return MixProbeRouter(
        policy,
        trace_sink,
        telemetry_level=telemetry_level,
    )


def run_load(
    notes: list[InjectedNote],
    *,
    run_id: str,
    bundle_id: str = "chopin_op11_movement_2",
    start_measure: int | None = 36,
    tempo_bpm: float = 70.0,
    settle_seconds: float = 2.0,
    mix_program_id: str | None = None,
    mix_program_revision: int | None = None,
    telemetry_level: TelemetryLevel = TelemetryLevel.COUNTERS,
    midi_output_name: str | None = None,
) -> Path:
    """Play ``notes`` through the real runtime; return the trace path."""

    from aimusic.server.live_runtime import LiveRuntimeManager

    port = CapturingMidiPort()
    span = notes[-1].perf_time if notes else 0.0
    manager = LiveRuntimeManager(
        input_factory=lambda _name: VirtualInputPort(notes, realtime=True),
        output_factory=(
            (lambda _name: mido.open_output(midi_output_name))
            if midi_output_name is not None
            else (lambda _name: port)
        ),
        audio_config_loader=LiveAudioConfig,
        mix_zones_factory=virtual_mix_zones,
        vst_router_factory=_probe_router_factory,
    )
    manager.start_follow(
        bundle_id=bundle_id,
        revision=None,
        input_name="virtual-in",
        output_name=midi_output_name or "software-midi-sink",
        start_measure=start_measure,
        config=RuntimeConfig(
            run_id=run_id,
            initial_tempo_bpm=tempo_bpm,
            mix_program_id=mix_program_id,
            mix_program_revision=mix_program_revision,
            telemetry_level=telemetry_level,
        ),
    )
    deadline = time.monotonic() + span + settle_seconds
    while time.monotonic() < deadline:
        time.sleep(0.05)
    manager.stop()
    # Let the hardware-control thread finish flushing the trace.
    for _ in range(100):
        status = manager.status()
        if status is not None and status.phase in {"completed", "failed"}:
            break
        time.sleep(0.05)
    time.sleep(0.5)
    return paths.run_trace_dir(run_id) / "runtime.jsonl"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), help="synthetic fixture")
    parser.add_argument("--trace", type=Path, help="replay a recorded take's input")
    parser.add_argument("--start-measure", type=int, default=36)
    parser.add_argument("--tempo", type=float, default=70.0)
    parser.add_argument("--mix-program", help="durable mix program id to exercise")
    parser.add_argument("--mix-revision", type=int, help="pin an expected mix revision")
    parser.add_argument(
        "--telemetry",
        choices=[level.value for level in TelemetryLevel],
        help="off/counters/trace; defaults to trace for a mix run",
    )
    parser.add_argument(
        "--midi-output",
        help="optional real/IAC software-synth MIDI output; default is in-process capture",
    )
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    args = parser.parse_args()

    if args.list:
        for name, spec in sorted(SCENARIOS.items()):
            count = len(generate_notes(spec))
            print(
                f"{name:10s} {count:5d} notes over {spec.seconds:.0f}s  "
                f"({spec.notes_per_beat}/beat)"
            )
        return 0

    if args.trace:
        notes = notes_from_trace(args.trace)
        label = f"replay-{Path(args.trace).parent.parent.name}"
    elif args.scenario:
        notes = generate_notes(SCENARIOS[args.scenario])
        label = f"load-{args.scenario}"
    else:
        parser.error("choose --scenario, --trace, or --list")

    run_id = f"{label}-{int(time.time())}"
    telemetry_level = TelemetryLevel(
        args.telemetry or ("trace" if args.mix_program else "counters")
    )
    print(f"running {label}: {len(notes)} notes over {notes[-1].perf_time:.1f}s")
    trace = run_load(
        notes,
        run_id=run_id,
        start_measure=args.start_measure,
        tempo_bpm=args.tempo,
        mix_program_id=args.mix_program,
        mix_program_revision=args.mix_revision,
        telemetry_level=telemetry_level,
        midi_output_name=args.midi_output,
    )
    if not trace.is_file():
        print(f"no trace written at {trace}")
        return 1
    card = score_run(
        trace,
        require_mix=bool(args.mix_program and telemetry_level == TelemetryLevel.TRACE),
        require_mix_change=bool(args.mix_program and telemetry_level == TelemetryLevel.TRACE),
    )
    print(card.report())
    print(f"trace: {trace}")
    return 0 if card.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
