"""Direct live BBCSO audition of an Oguri MIDI cue."""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import mido
import numpy as np

from aimusic.accompaniment.live_midi import midi_cue_events
from aimusic.audio.orchestra_render import (
    DEFAULT_MIX_GAIN,
    OGURI_MOVEMENT_2_PARTS,
    BbcsoPart,
    ScoreCueWindow,
    default_plugin_state_dir,
)
from aimusic.audio.plugin_host import DEFAULT_BBCSO_VST3_PATH


@dataclass(frozen=True)
class OrchestraAuditionSummary:
    note_count: int
    duration_seconds: float
    load_seconds: float


def play_oguri_bbcso_live(
    midi_path: Path,
    *,
    window: ScoreCueWindow,
    output_device_name: str,
    state_dir: Path | None = None,
    plugin_path: Path = DEFAULT_BBCSO_VST3_PATH,
    parts: tuple[BbcsoPart, ...] = OGURI_MOVEMENT_2_PARTS,
    volume: float = 0.75,
    mix_gain: float = DEFAULT_MIX_GAIN,
    sample_rate: float = 48_000.0,
    block_size: int = 512,
    lead_seconds: float = 2.0,
    release_tail_seconds: float = 3.0,
    progress: Callable[[str], None] | None = None,
) -> OrchestraAuditionSummary:
    """Load each BBCSO patch in an isolated host and stream all parts together."""

    state_dir = state_dir or default_plugin_state_dir()
    state_paths = tuple(state_dir / part.state_filename for part in parts)
    for state_path in state_paths:
        if not state_path.is_file():
            raise FileNotFoundError(f"BBCSO state is missing: {state_path}")
    if not plugin_path.exists():
        raise FileNotFoundError(f"BBCSO plug-in is missing: {plugin_path}")

    note_count = sum(
        _note_on_count(
            _part_events(
                midi_path,
                track_name=part.track_name,
                source_start_seconds=window.source_start_seconds,
                duration_seconds=window.duration_seconds,
                release_tail_seconds=release_tail_seconds,
                volume=volume,
            )
        )
        for part in parts
    )
    if progress is not None:
        progress(f"Loading {len(parts)} isolated BBCSO instruments once...")
    load_started = time.monotonic()
    processes: list[subprocess.Popen[str]] = []
    with tempfile.TemporaryDirectory(prefix="rubato-bbcso-live-") as temp:
        barrier_dir = Path(temp)
        start_file = barrier_dir / "start"
        try:
            for index, (part, state_path) in enumerate(
                zip(parts, state_paths, strict=True)
            ):
                if progress is not None:
                    progress(f"Loading {part.track_name} ({index + 1}/{len(parts)})...")
                ready_file = barrier_dir / f"ready-{index}"
                command = (
                    sys.executable,
                    "-m",
                    "aimusic.audio.orchestra_live_audition",
                    "--part-worker",
                    "--midi",
                    str(midi_path),
                    "--track",
                    part.track_name,
                    "--state",
                    str(state_path),
                    "--plugin",
                    str(plugin_path),
                    "--device",
                    output_device_name,
                    "--source-start",
                    str(window.source_start_seconds),
                    "--duration",
                    str(window.duration_seconds),
                    "--tail",
                    str(release_tail_seconds),
                    "--volume",
                    str(volume),
                    "--mix-gain",
                    str(mix_gain),
                    "--sample-rate",
                    str(sample_rate),
                    "--block-size",
                    str(block_size),
                    "--ready-file",
                    str(ready_file),
                    "--start-file",
                    str(start_file),
                )
                processes.append(
                    subprocess.Popen(
                        command,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )
                # BBCSO's initialization is not concurrency-safe even across
                # independent hosts. Wait for each resident instance before
                # creating the next one.
                _wait_until_ready(
                    processes,
                    barrier_dir,
                    index + 1,
                    timeout_seconds=15.0,
                )
            load_seconds = time.monotonic() - load_started
            if progress is not None:
                progress(
                    f"Orchestra ready after {load_seconds:.1f}s; playback starts in "
                    f"{lead_seconds:.1f}s."
                )
            time.sleep(lead_seconds)
            start_file.touch()
            _wait_for_playback(
                processes,
                timeout_seconds=window.duration_seconds + release_tail_seconds + 15.0,
            )
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                if process.poll() is None:
                    try:
                        process.wait(timeout=3.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3.0)

    return OrchestraAuditionSummary(
        note_count=note_count,
        duration_seconds=window.duration_seconds,
        load_seconds=load_seconds,
    )


def _wait_until_ready(
    processes: list[subprocess.Popen[str]],
    barrier_dir: Path,
    expected: int,
    *,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        _raise_for_exited_worker(processes, phase="startup")
        if len(tuple(barrier_dir.glob("ready-*"))) == expected:
            return
        time.sleep(0.02)
    raise TimeoutError("BBCSO part workers did not become ready")


def _wait_for_playback(
    processes: list[subprocess.Popen[str]], *, timeout_seconds: float
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if all(process.poll() is not None for process in processes):
            _raise_for_exited_worker(processes, phase="playback", allow_success=True)
            return
        _raise_for_exited_worker(processes, phase="playback", allow_success=True)
        time.sleep(0.05)
    raise TimeoutError("BBCSO part workers did not finish playback")


def _raise_for_exited_worker(
    processes: list[subprocess.Popen[str]],
    *,
    phase: str,
    allow_success: bool = False,
) -> None:
    for process in processes:
        return_code = process.poll()
        if return_code is None or (allow_success and return_code == 0):
            continue
        error = process.stderr.read().strip() if process.stderr is not None else ""
        detail = error[-2000:] if error else f"exit code {return_code}"
        raise RuntimeError(f"BBCSO part worker failed during {phase}: {detail}")


def _run_part_worker(args: argparse.Namespace) -> int:
    import pedalboard
    from pedalboard.io import AudioStream

    events = _part_events(
        args.midi,
        track_name=args.track,
        source_start_seconds=args.source_start,
        duration_seconds=args.duration,
        release_tail_seconds=args.tail,
        volume=args.volume,
    )
    plugin = pedalboard.load_plugin(str(args.plugin), initialization_timeout=1.0)
    print("stage=plugin_loaded", file=sys.stderr, flush=True)
    if not plugin.is_instrument:
        raise ValueError(f"plug-in is not an instrument: {args.plugin}")
    plugin.raw_state = args.state.read_bytes()
    print("stage=state_applied", file=sys.stderr, flush=True)
    block_seconds = args.block_size / args.sample_rate
    plugin.process(
        [],
        duration=block_seconds,
        sample_rate=args.sample_rate,
        num_channels=2,
        buffer_size=args.block_size,
        reset=False,
    )
    print("stage=plugin_warmed", file=sys.stderr, flush=True)
    stream = AudioStream(
        output_device_name=args.device,
        sample_rate=args.sample_rate,
        buffer_size=args.block_size,
        num_output_channels=2,
    )
    print("stage=stream_constructed", file=sys.stderr, flush=True)
    stream.__enter__()
    print("stage=stream_started", file=sys.stderr, flush=True)
    try:
        args.ready_file.write_text("ready\n", encoding="utf-8")
        while not args.start_file.exists():
            time.sleep(0.005)
        _stream_part(
            plugin,
            stream,
            events,
            duration_seconds=args.duration + args.tail,
            sample_rate=args.sample_rate,
            block_size=args.block_size,
            mix_gain=args.mix_gain,
        )
    finally:
        stream.close()
    return 0


def _stream_part(
    plugin,
    stream,
    events: list[tuple[float, mido.Message]],
    *,
    duration_seconds: float,
    sample_rate: float,
    block_size: int,
    mix_gain: float,
) -> None:
    block_seconds = block_size / sample_rate
    block_count = math.ceil(duration_seconds / block_seconds)
    event_index = 0
    for block_index in range(block_count):
        block_start = block_index * block_seconds
        block_end = block_start + block_seconds
        messages: list[tuple[bytes, float]] = []
        while event_index < len(events) and events[event_index][0] < block_end:
            timestamp, message = events[event_index]
            if message.type in {
                "note_on",
                "note_off",
                "control_change",
                "pitchwheel",
                "aftertouch",
            }:
                messages.append(
                    (
                        bytes(message.copy(channel=0, time=0).bytes()),
                        max(0.0, timestamp - block_start),
                    )
                )
            event_index += 1
        rendered = np.asarray(
            plugin.process(
                messages,
                duration=block_seconds,
                sample_rate=sample_rate,
                num_channels=2,
                buffer_size=block_size,
                reset=False,
            ),
            dtype=np.float32,
        )
        if rendered.shape != (2, block_size):
            raise RuntimeError(
                f"BBCSO returned {rendered.shape}; expected {(2, block_size)}"
            )
        stream.write(rendered * mix_gain, sample_rate)


def _part_events(
    midi_path: Path,
    *,
    track_name: str,
    source_start_seconds: float,
    duration_seconds: float,
    release_tail_seconds: float,
    volume: float,
) -> list[tuple[float, mido.Message]]:
    events = midi_cue_events(
        midi_path,
        volume=volume,
        start_seconds=source_start_seconds,
        duration_seconds=duration_seconds,
        release_tail_seconds=release_tail_seconds,
        include_track_names=(track_name,),
    )
    return [
        (0.0, mido.Message("control_change", channel=0, control=7, value=127)),
        (0.0, mido.Message("control_change", channel=0, control=11, value=127)),
        *events,
    ]


def _note_on_count(events: list[tuple[float, mido.Message]]) -> int:
    return sum(
        message.type == "note_on" and message.velocity > 0
        for _timestamp, message in events
    )


def _part_worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--part-worker", action="store_true", required=True)
    parser.add_argument("--midi", type=Path, required=True)
    parser.add_argument("--track", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--source-start", type=float, required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--tail", type=float, required=True)
    parser.add_argument("--volume", type=float, required=True)
    parser.add_argument("--mix-gain", type=float, required=True)
    parser.add_argument("--sample-rate", type=float, required=True)
    parser.add_argument("--block-size", type=int, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--start-file", type=Path, required=True)
    return parser


if __name__ == "__main__":  # pragma: no cover - exercised by hardware audition
    raise SystemExit(_run_part_worker(_part_worker_parser().parse_args()))


__all__ = ["OrchestraAuditionSummary", "play_oguri_bbcso_live"]
