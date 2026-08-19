#!/usr/bin/env python3
"""Drive the real LiveEngine through the cadenza and check the orchestra enters.

Everything up to the handoff was verified by driving CadenzaDetector directly,
which is exactly the seam that hid a bug: the engine accepted a detector and
never built one. This harness crosses every join instead -- bundle load,
projection, engine factory, note processing, transport snap, scheduler -- and
asserts on what the orchestra actually plays, because that is the only thing
the performer will hear.

Usage:
    python scripts/replay_cadenza_handoff.py --take <solo.mid> [--from-measure 97]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import mido

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aimusic.accompaniment.bundle_v2 import BundleLoaderV2, BundleRegistry  # noqa: E402
from aimusic.accompaniment.live_engine import LiveEngine, _cadenza_detector_for  # noqa: E402
from aimusic.accompaniment.following import PerformedNote  # noqa: E402
from aimusic.accompaniment.runtime_projection import (  # noqa: E402
    project_bundle_v2_to_provisional_runtime,
)
from aimusic.core import paths  # noqa: E402


def note_events(path: Path) -> list[tuple[float, int, int]]:
    """(seconds, pitch, velocity) for every note-on, in order."""

    midi = mido.MidiFile(path)
    ppq = midi.ticks_per_beat
    tempos = [(0, 500000)]
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

    events: list[tuple[float, int, int]] = []
    for track in midi.tracks:
        tick = 0
        for message in track:
            tick += message.time
            if message.type == "note_on" and message.velocity > 0:
                events.append((seconds(tick), message.note, message.velocity))
    events.sort()
    return events


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--take", required=True)
    ap.add_argument("--piece", default="chopin_op11")
    ap.add_argument("--movement", type=int, default=2)
    ap.add_argument("--lead-seconds", type=float, default=25.0,
                    help="how much playing before the cadenza to replay")
    args = ap.parse_args()

    root = paths.score_bundle_dir(args.piece, args.movement)
    loaded = BundleLoaderV2.load(root)
    projection = project_bundle_v2_to_provisional_runtime(
        bundle_id=loaded.manifest.bundle_id,
        revision=loaded.manifest.revision,
        registry=BundleRegistry(),
        explicit_root=root,
    )
    bundle = projection.bundle

    detector = _cadenza_detector_for(bundle)
    if detector is None:
        print("FAIL: no cadenza detector built from the bundle")
        return 1
    model = detector._model
    print(f"detector: region starts beat {model.region_start_beat}, "
          f"handoff {model.handoff_beat}, hands back {model.hand_back_beat}")

    events = note_events(Path(args.take))
    landing = max(
        (s for i, (s, p, _) in enumerate(events)
         if p in (56, 90)
         and {q for t, q, _ in events if abs(t - s) < 0.15} >= {56, 90}),
        default=None,
    )
    if landing is None:
        print("FAIL: the take never reaches the cadenza's landing dyad")
        return 1
    # Cut at the landing dyad: the performer is silent through the interlude, so
    # feeding the chromatic re-entry here would advance the follower and collapse
    # m.104 -- the opposite of what happens live.
    window = [e for e in events if landing - args.lead_seconds <= e[0] <= landing + 0.1]
    origin = window[0][0]
    print(f"take: {len(events)} notes; replaying {len(window)} around the landing "
          f"dyad at {landing:.2f}s")

    from aimusic.accompaniment.following import ReferencePitchFollower
    from aimusic.accompaniment.runtime_contracts import RuntimeConfig
    from aimusic.accompaniment.runtime_io import (
        CapturingOutput,
        JsonlTraceSink,
        ManualClock,
    )

    clock = ManualClock()
    output = CapturingOutput()
    trace_path = Path("/tmp/cadenza_handoff_trace.jsonl")
    trace_sink = JsonlTraceSink(trace_path)
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(run_id="cadenza-handoff-check"),
        clock=clock,
        follower=ReferencePitchFollower(bundle.solo_events),
        output=output,
        trace_sink=trace_sink,
    )
    # Start AT the region. The lead-in is the follower's job and is already
    # exercised elsewhere; what has never been verified is the handoff itself,
    # so this isolates it rather than letting an unrelated follower miss decide
    # the result. A pitch follower turned loose on the cadenza matches it to
    # early material -- score_beat 48 for a passage at 401.
    engine.start(start_beat=model.region_start_beat)

    messages: list[str] = []
    # Interleave ticks between notes at their real spacing, so the transport has
    # clock time to advance the interlude once the handoff begins it. Processing
    # every note first and ticking afterwards collapses the interlude, because
    # nothing runs the LEAD clock between the handoff and the end of the burst.
    clk = 0.0
    for seconds, pitch, velocity in window:
        target = seconds - origin
        while clk + 0.02 < target:
            clk += 0.02
            clock.wait_until(clk)
            engine.tick()
        clk = max(clk, target)
        clock.wait_until(clk)
        status = engine.process_note(
            PerformedNote(pitch=pitch, velocity=velocity, perf_time=clk)
        )
        text = getattr(status, "message", "") or ""
        if text and (not messages or messages[-1] != text):
            messages.append(text)

    # PASS is measured on what the orchestra PLAYS, not on the handoff event.
    # An earlier version keyed success on the handoff firing and so reported
    # PASS while emitting zero interlude notes -- the exact bug that reached a
    # live run. The interlude only plays if ticks run during the performer's
    # silence, so tick it out here.
    for _ in range(400):
        clk += 0.02
        clock.wait_until(clk)
        engine.tick()
    engine.stop("replay_complete")
    trace_sink.close()

    import json as _json

    free = [
        _json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if '"free_region"' in line
    ]
    print("\nfree-region trace events:")
    for e in free or [{"event": "(none written)"}]:
        print(f"  {e.get('event'):<10} beat={e.get('score_beat')} "
              f"p={e.get('probability')} notes={e.get('notes_observed')} "
              f"{e.get('detail') or ''}")
    handoff = [e for e in free if e.get("event") == "handoff"]
    interlude = [
        e for _, e in output.sent if e.event.beat >= model.hand_back_beat
    ]
    print(f"\norchestra notes in the interlude (beat >= {model.hand_back_beat}): "
          f"{len(interlude)}")
    ok = bool(handoff) and len(interlude) >= 10
    print(f"\n{'PASS' if ok else 'FAIL'}: "
          + ("orchestra played the interlude" if ok
             else "handoff fired but the interlude did not play"
                  if handoff else "no handoff"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
