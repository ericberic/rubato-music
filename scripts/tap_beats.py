#!/usr/bin/env python3
"""Play a passage and record the performer tapping its beats.

Produces a direct beat -> performance-time correspondence for passages where
automated alignment cannot help. In a cadenza the score, the reference
recording and the performer's playing share no reliable note correspondence --
the Chopin mvt II reference reaches only 0.32 pitch-sequence similarity with
the notation -- so every content-based method has nothing to match on. A tap
carries no pitch information at all, only time, which is exactly the signal
that is missing.

The performer hears the recording and plays any key on each beat. Tap k of the
run is beat k counting from ``--from-measure``, so the mapping is asserted by
the performer's ear rather than inferred.

Taps arrive late by the performer's reaction time plus MIDI round trip. That is
a roughly constant offset: tap a passage with a known grid first (``--metronome``)
to measure it, then subtract. Do not assume it is negligible -- a uniform lateness
looks exactly like a real finding.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import mido

# The performer's chosen metronome voicing on the GM drum channel: Claves on
# every beat with an Open Triangle layered on the downbeat -- matching the
# Clavinova's own metronome, whose internal click is NOT transmitted over MIDI
# and so cannot be timestamped by us.
CLICK_NOTE = 75   # Claves
BELL_NOTE = 81    # Open Triangle
DRUM_CHANNEL = 9


def click_messages(downbeat: bool) -> list[mido.Message]:
    notes = [(CLICK_NOTE, 96 if downbeat else 70)]
    if downbeat:
        notes.append((BELL_NOTE, 104))
    return [
        mido.Message("note_on", channel=DRUM_CHANNEL, note=n, velocity=v) for n, v in notes
    ]


def click_offs(downbeat: bool) -> list[mido.Message]:
    notes = [CLICK_NOTE] + ([BELL_NOTE] if downbeat else [])
    return [
        mido.Message("note_off", channel=DRUM_CHANNEL, note=n, velocity=0) for n in notes
    ]


def load_measure_times(alignment: Path) -> dict[int, float]:
    payload = json.loads(alignment.read_text(encoding="utf-8"))
    return {int(k): float(v) for k, v in payload["measure_downbeat_seconds"].items()}


def absolute_events(path: Path) -> tuple[list[tuple[float, mido.Message]], float]:
    """[(seconds, message)] for every note event, plus the file's duration."""

    midi = mido.MidiFile(path)
    ppq = midi.ticks_per_beat
    tempos: list[tuple[int, int]] = []
    for track in midi.tracks:
        tick = 0
        for message in track:
            tick += message.time
            if message.type == "set_tempo":
                tempos.append((tick, message.tempo))
    tempos.sort()

    def seconds(target: int) -> float:
        total, last, current = 0.0, 0, 500000
        for tick, tempo in tempos:
            if tick >= target:
                break
            total += mido.tick2second(tick - last, ppq, current)
            last, current = tick, tempo
        return total + mido.tick2second(target - last, ppq, current)

    events: list[tuple[float, mido.Message]] = []
    for track in midi.tracks:
        tick = 0
        for message in track:
            tick += message.time
            if message.type in ("note_on", "note_off"):
                events.append((seconds(tick), message.copy()))
    events.sort(key=lambda e: e[0])
    return events, (events[-1][0] if events else 0.0)


def run(args: argparse.Namespace) -> int:
    events, _ = absolute_events(Path(args.midi))
    measures = load_measure_times(Path(args.alignment))
    start = measures[args.from_measure]
    end = measures.get(args.to_measure + 1, start + args.seconds)
    window = [(t, m) for t, m in events if start - args.lead <= t <= end]
    print(f"passage: m.{args.from_measure}-{args.to_measure}   "
          f"{start:.2f}s - {end:.2f}s in the recording ({end - start:.1f}s)")
    print(f"playing to {args.output!r}, listening on {args.input!r}")
    print(f"\nTap ANY key on each beat, starting on the downbeat of m.{args.from_measure}.")
    print(f"{args.lead:.0f}s lead-in first.\n")

    taps: list[float] = []
    outp = mido.open_output(args.output)
    inp = mido.open_input(args.input)
    try:
        for message in inp.iter_pending():  # drain anything stale
            pass
        t0 = time.monotonic()
        origin = start - args.lead
        index = 0
        while index < len(window):
            now = time.monotonic() - t0
            due = window[index][0] - origin
            if due <= now:
                message = window[index][1]
                if message.type == "note_on" and message.velocity > 0:
                    outp.send(message)
                elif message.type == "note_off":
                    outp.send(message)
                index += 1
                continue
            for message in inp.iter_pending():
                if message.type == "note_on" and message.velocity > 0:
                    taps.append(time.monotonic() - t0 + origin)
                    print(f"   tap {len(taps):>3}  at {taps[-1]:8.3f}s "
                          f"(+{taps[-1] - start:6.3f}s into the passage)")
            time.sleep(0.001)
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            for message in inp.iter_pending():
                if message.type == "note_on" and message.velocity > 0:
                    taps.append(time.monotonic() - t0 + origin)
                    print(f"   tap {len(taps):>3}  at {taps[-1]:8.3f}s")
            time.sleep(0.005)
    finally:
        outp.send(mido.Message("control_change", control=123, value=0))
        outp.close()
        inp.close()

    if not taps:
        print("\nno taps recorded")
        return 1
    beats = [
        {"beat_index": i, "measure": args.from_measure + i // 4, "beat_in_measure": i % 4 + 1,
         "performance_seconds": round(t, 4)}
        for i, t in enumerate(taps)
    ]
    payload = {
        "source": "human_tap",
        "midi": Path(args.midi).name,
        "from_measure": args.from_measure,
        "to_measure": args.to_measure,
        "note": "taps include reaction time and MIDI round trip; measure and subtract the offset",
        "beats": beats,
    }
    Path(args.out).write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"\n{len(taps)} taps -> {args.out}")
    gaps = [b - a for a, b in zip(taps, taps[1:])]
    if gaps:
        gaps_sorted = sorted(gaps)
        print(f"   inter-tap interval: median {gaps_sorted[len(gaps) // 2]:.3f}s  "
              f"min {min(gaps):.3f}  max {max(gaps):.3f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("midi", help="performance MIDI to play back")
    ap.add_argument("--alignment", required=True, help="measure -> seconds JSON")
    ap.add_argument("--from-measure", type=int, required=True)
    ap.add_argument("--to-measure", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--input", default="Clavinova")
    ap.add_argument("--output", default="Clavinova")
    ap.add_argument("--lead", type=float, default=4.0, help="seconds of context before the passage")
    ap.add_argument("--seconds", type=float, default=30.0, help="fallback length if the end measure is unknown")
    return run(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
