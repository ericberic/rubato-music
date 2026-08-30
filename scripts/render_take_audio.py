#!/usr/bin/env python
"""Render a captured live take to a WAV by replaying it through a MIDI instrument.

Rubato captures every take without saving audio: the full solo performance
(note-ons, note-offs, sustain/soft pedal, aftertouch) lands in
``<session_dir>/solo.mid``, and the orchestra it generated is in the run trace
(``runtime.jsonl`` ``midi_output`` rows carry pitch and note-off). This plays
both back out to the instrument (e.g. digital piano / synthesizer) -- which renders
the sound, applying the real pedal to the solo -- while recording the instrument's
audio input, and writes the result to a WAV.

For NEW takes prefer capturing the instrument's audio live during the
performance (a WAV recording, no replay). This script is for takes already played.

Usage:
    uv run python scripts/render_take_audio.py                 # latest run, full take
    uv run python scripts/render_take_audio.py --run-id live-<id>
    uv run python scripts/render_take_audio.py --seconds 45    # first 45 s only
    uv run python scripts/render_take_audio.py --out take.wav

Ports/devices default to "Clavinova" on both MIDI-out and audio-in; override with
--midi-port / --audio-device (see `mido.get_output_names()` /
`sounddevice.query_devices()`).
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

import mido

from aimusic.core import paths

SAMPLE_RATE = 44100


def _latest_run_id() -> str:
    runs = sorted(paths.runs_root().glob("live-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not runs:
        raise SystemExit("no runs found")
    return runs[0].name


def _orchestra_events(rows: list[dict]) -> list[tuple[float, mido.Message]]:
    """Orchestra note events on the perf clock, reconstructed from the trace.

    Includes ``retrigger_note_off`` (a note-off variant the scheduler emits when
    a held pitch is re-struck; dropping it leaves the note droning for the rest
    of the piece), and closes any still-open note at the end as a safety net --
    a silent stuck note pollutes the whole render.
    """

    orchestra: list[tuple[float, mido.Message]] = []
    for r in rows:
        if r.get("type") != "midi_output":
            continue
        action = r.get("action")
        if action not in ("note_on", "note_off", "retrigger_note_off"):
            continue
        perf = r.get("target_perf_time") or r.get("requested_send_time") or r.get("monotonic_time")
        kind = "note_off" if action == "retrigger_note_off" else action
        orchestra.append(
            (
                perf,
                mido.Message(
                    kind,
                    note=int(r["pitch"]),
                    velocity=int(r.get("velocity") or 0),
                    channel=int(r.get("channel") or 0),
                ),
            )
        )

    balance: dict[tuple[int, int], int] = {}
    for _time, message in orchestra:
        key = (message.channel, message.note)
        if message.type == "note_on" and message.velocity > 0:
            balance[key] = balance.get(key, 0) + 1
        elif message.type == "note_off":
            balance[key] = balance.get(key, 0) - 1
    unbalanced = {key: n for key, n in balance.items() if n > 0}
    if unbalanced:
        print(f"WARNING: {sum(unbalanced.values())} note(s) left open, closing at end: {unbalanced}")
        tail = max(t for t, _ in orchestra) + 0.05
        for (channel, note), n in unbalanced.items():
            for _ in range(n):
                orchestra.append((tail, mido.Message("note_off", note=note, velocity=0, channel=channel)))
    return orchestra


def _solo_events(
    trace_onsets: list[float], solo_mid: mido.MidiFile
) -> list[tuple[float, mido.Message]]:
    """Solo events (with pedal + releases) anchored onto the perf clock.

    solo.mid's t=0 is the first CAPTURED event -- often a pedal press ~1 s before
    the first note -- so anchoring on t=0 (or on the first note-on's own rel time)
    offsets the whole solo and desyncs it from the orchestra. Instead match
    solo.mid's note-ons 1:1 to the trace's exact note-on timestamps and take the
    median offset as the true capture origin; the median resists the ~50 ms
    tick-quantization jitter in solo.mid's cumulative deltas.
    """

    tempo = next(
        (m.tempo for m in mido.merge_tracks(solo_mid.tracks) if m.type == "set_tempo"), 500000
    )
    solo_rel: list[tuple[float, mido.Message]] = []
    onset_rel: list[float] = []
    elapsed = 0.0
    for message in mido.merge_tracks(solo_mid.tracks):
        elapsed += mido.tick2second(message.time, solo_mid.ticks_per_beat, tempo)
        if message.is_meta:
            continue
        solo_rel.append((elapsed, message))
        if message.type == "note_on" and message.velocity > 0:
            onset_rel.append(elapsed)
    count = min(len(trace_onsets), len(onset_rel))
    if count == 0:
        raise SystemExit("no solo onsets to align")
    capture_origin = statistics.median(trace_onsets[i] - onset_rel[i] for i in range(count))
    return [(capture_origin + rel, m) for rel, m in solo_rel]


def _combine(
    orchestra: list[tuple[float, mido.Message]], solo: list[tuple[float, mido.Message]]
) -> list[tuple[float, mido.Message]]:
    """Merge both streams onto a render clock starting at 0, releases first."""

    origin = min(t for t, _ in orchestra + solo)
    events = [(t - origin, m) for t, m in orchestra + solo]
    # note_off / pedal before note_on at the same instant.
    events.sort(key=lambda e: (e[0], 0 if e[1].type in ("note_off", "control_change") else 1))
    return events


def _load_events(run_id: str) -> tuple[list[tuple[float, mido.Message]], list[tuple[int, int]]]:
    """Return (events on a shared render clock, channel-volume CCs) for a run."""

    trace = paths.run_trace_dir(run_id) / "runtime.jsonl"
    rows = [json.loads(line) for line in trace.read_text().splitlines() if line.strip()]

    channel_volumes = [
        (int(r["channel"]), int(r["value"]))
        for r in rows
        if r.get("type") == "midi_output" and r.get("action") == "channel_volume"
    ]
    orchestra = _orchestra_events(rows)
    trace_onsets = [r["perf_time"] for r in rows if r.get("type") == "input"]
    solo = _solo_events(trace_onsets, mido.MidiFile(paths.session_dir(run_id) / "solo.mid"))
    return _combine(orchestra, solo), channel_volumes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--seconds", type=float, default=None, help="render only N seconds (from --start)")
    parser.add_argument("--start", type=float, default=0.0, help="skip the first N seconds")
    parser.add_argument("--out", default=None)
    parser.add_argument("--midi-port", default="Clavinova")
    parser.add_argument("--audio-device", default="Clavinova")
    parser.add_argument(
        "--orchestra-gain",
        type=float,
        default=1.0,
        help="scale the orchestra channel volumes (CC7), like nudging its volume knob; 1.05 = +5%%",
    )
    args = parser.parse_args()

    import numpy as np
    import sounddevice as sd
    import soundfile as sf

    run_id = args.run_id or _latest_run_id()
    events, channel_volumes = _load_events(run_id)
    end = args.start + args.seconds if args.seconds is not None else float("inf")
    events = [(t - args.start, m) for t, m in events if args.start <= t <= end]
    if not events:
        raise SystemExit("no events in the requested window")
    duration = max(t for t, _ in events)
    out_path = Path(args.out) if args.out else paths.runs_root() / run_id / f"{run_id}.wav"
    print(f"run {run_id}: {len(events)} messages, {duration:.1f}s -> {out_path}")

    out = mido.open_output(args.midi_port)
    recording = sd.rec(
        int((duration + 1.5) * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=2, device=args.audio_device
    )
    start = time.monotonic()
    for channel, value in channel_volumes:
        scaled = max(0, min(127, round(value * args.orchestra_gain)))
        out.send(mido.Message("control_change", channel=channel, control=7, value=scaled))
    for at, message in events:
        delay = at - (time.monotonic() - start)
        if delay > 0:
            time.sleep(delay)
        out.send(message)
    # Release every channel so nothing sticks in the recording's tail.
    for channel in range(16):
        out.send(mido.Message("control_change", channel=channel, control=123, value=0))
        out.send(mido.Message("control_change", channel=channel, control=64, value=0))
    out.close()
    sd.wait()

    sf.write(out_path, recording, SAMPLE_RATE)
    peak = float(np.max(np.abs(recording)))
    print(f"done: {out_path}  ({recording.shape[0] / SAMPLE_RATE:.1f}s, peak {peak:.3f})")
    if peak < 0.005:
        print("WARNING: near-silent -- check the instrument is the audio input and MIDI-out target")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
