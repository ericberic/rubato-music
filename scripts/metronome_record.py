#!/usr/bin/env python3
"""Play a steady click and record the performer playing a passage against it.

For deriving anchor triggers by demonstration. The performer plays the same
passage several times separated by rests; segmentation splits the repetitions,
and comparing them separates what is invariant (a reliable trigger) from what
varies (an ornament, which must never be a trigger). A single take cannot make
that distinction -- a pitch that looks decisive once may occur four times.

The click is the performer's chosen voicing: Claves on every beat with an Open
Triangle layered on the downbeat. The Clavinova's own metronome is generated
internally and is not transmitted over MIDI, so a click we can timestamp has to
be one we send.

Stop with Ctrl-C. Everything played is written out with both wall-clock and
beat positions relative to the click grid.
"""

from __future__ import annotations

import argparse
import json
import signal
import time
from pathlib import Path

import mido

CLICK_NOTE = 75  # Claves
BELL_NOTE = 81  # Open Triangle
DRUM_CHANNEL = 9
PITCH_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

_stop = False


def _handle_stop(signum, frame):  # noqa: ARG001
    global _stop
    _stop = True


def run(args: argparse.Namespace) -> int:
    period = 60.0 / args.bpm
    outp = mido.open_output(args.output)
    inp = mido.open_input(args.input)
    signal.signal(signal.SIGINT, _handle_stop)

    print(f"click: {args.bpm:g} BPM, {args.beats_per_bar} beats/bar "
          f"(Claves every beat, Open Triangle on the downbeat)")
    print(f"listening on {args.input!r}\n")
    print("Play the passage, rest a few beats, repeat. Ctrl-C when done.\n")

    notes: list[dict] = []
    for _ in inp.iter_pending():  # drain stale input
        pass
    t0 = time.monotonic()
    beat_index = 0
    try:
        while not _stop:
            due = t0 + beat_index * period
            now = time.monotonic()
            if now >= due:
                downbeat = beat_index % args.beats_per_bar == 0
                pitches = [(CLICK_NOTE, 96 if downbeat else 70)]
                if downbeat:
                    pitches.append((BELL_NOTE, 104))
                for note, velocity in pitches:
                    outp.send(mido.Message("note_on", channel=DRUM_CHANNEL,
                                           note=note, velocity=velocity))
                off_at = now + 0.05
                beat_index += 1
                while time.monotonic() < off_at:
                    time.sleep(0.001)
                for note, _v in pitches:
                    outp.send(mido.Message("note_off", channel=DRUM_CHANNEL,
                                           note=note, velocity=0))
                continue
            for message in inp.iter_pending():
                if message.type == "note_on" and message.velocity > 0:
                    t = time.monotonic() - t0
                    notes.append({
                        "seconds": round(t, 4),
                        "beat": round(t / period, 4),
                        "pitch": message.note,
                        "velocity": message.velocity,
                    })
                    if len(notes) % 25 == 0:
                        print(f"   {len(notes)} notes, beat {t / period:.1f}")
            time.sleep(0.001)
    finally:
        outp.send(mido.Message("control_change", channel=DRUM_CHANNEL, control=123, value=0))
        outp.close()
        inp.close()

    if not notes:
        print("\nno notes recorded")
        return 1
    payload = {
        "source": "metronome_record",
        "bpm": args.bpm,
        "beats_per_bar": args.beats_per_bar,
        "beat_period_seconds": period,
        "note": "beat positions are relative to the click grid; beat 0.0 is the first click",
        "notes": notes,
    }
    Path(args.out).write_text(json.dumps(payload, indent=1), encoding="utf-8")
    span = notes[-1]["seconds"] - notes[0]["seconds"]
    print(f"\n{len(notes)} notes over {span:.1f}s ({span / period:.1f} beats) -> {args.out}")

    # rough segmentation preview: a rest longer than `gap_beats` splits a repetition
    reps: list[list[dict]] = [[notes[0]]]
    for previous, current in zip(notes, notes[1:]):
        if (current["beat"] - previous["beat"]) > args.gap_beats:
            reps.append([])
        reps[-1].append(current)
    print(f"   segmented into {len(reps)} repetition(s) at a {args.gap_beats}-beat gap:")
    for i, rep in enumerate(reps, 1):
        lo, hi = rep[0]["beat"], rep[-1]["beat"]
        print(f"      rep {i}: {len(rep):>4} notes, beats {lo:7.2f} - {hi:7.2f} ({hi - lo:5.2f} beats)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True)
    ap.add_argument("--bpm", type=float, default=60.0)
    ap.add_argument("--beats-per-bar", type=int, default=4)
    ap.add_argument("--input", default="Clavinova")
    ap.add_argument("--output", default="Clavinova")
    ap.add_argument("--gap-beats", type=float, default=3.0,
                    help="a rest this long or longer starts a new repetition")
    return run(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
