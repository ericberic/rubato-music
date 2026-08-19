"""The cadenza handoff must make the orchestra actually PLAY the interlude.

This is the test that was missing. Every earlier check asserted that a handoff
fired; none asserted its effect. So a handoff that fired perfectly -- correct
beat, p=0.99 -- while the orchestra played nothing passed every gate, and the
failure only surfaced live. The bug: throughout a FREE section the scheduler
dispatch sets authority to STOP, and the handoff moved the position without
beginning the LEAD section it landed in, so the interlude never started.

These drive the real engine end to end and assert on emitted orchestra notes,
not on the handoff event. Skipped when the score bundle is not pulled.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aimusic.accompaniment.following import PerformedNote, ReferencePitchFollower
from aimusic.accompaniment.live_engine import LiveEngine, _cadenza_detector_for
from aimusic.accompaniment.runtime_contracts import RuntimeConfig
from aimusic.accompaniment.runtime_io import CapturingOutput, ManualClock, NullTraceSink
from aimusic.core import paths

BUNDLE_PRESENT = (
    paths.score_bundle_dir("chopin_op11", 2) / "derived" / "solo_reference.mid"
).is_file()
DEMOS = Path(
    "data/scores/chopin_op11_movement_2/derived/cadenza_handoff.model.json"
)


def _find_take_through_cadenza() -> Path | None:
    """A recorded take that reaches the landing dyad, if any is on disk.

    The chroma trigger fires on the performer's actual gestures, which a
    synthetic sequence does not reproduce, so this reads a real take. It is the
    faithful reproduction of the live failure.
    """

    root = Path(os.path.expanduser("~/Library/Application Support/Rubato"))
    for path in sorted(root.glob("**/solo.mid"), reverse=True):
        try:
            ev = _note_events(path)
        except Exception:
            continue
        if any(
            p in (56, 90)
            and {q for tt, q, _ in ev if abs(tt - s) < 0.15} >= {56, 90}
            for s, p, _ in ev
        ):
            return path
    return None


def _note_events(path: Path) -> list[tuple[float, int, int]]:
    import mido

    midi = mido.MidiFile(path)
    ppq = midi.ticks_per_beat
    tempos = [(0, 500000)]
    for track in midi.tracks:
        tick = 0
        for m in track:
            tick += m.time
            if m.type == "set_tempo":
                tempos.append((tick, m.tempo))
    tempos.sort()

    def seconds(target: int) -> float:
        total, last, cur = 0.0, 0, 500000
        for tk, tp in tempos:
            if tk >= target:
                break
            total += mido.tick2second(tk - last, ppq, cur)
            last, cur = tk, tp
        return total + mido.tick2second(target - last, ppq, cur)

    ev = []
    for track in midi.tracks:
        tick = 0
        for m in track:
            tick += m.time
            if m.type == "note_on" and m.velocity > 0:
                ev.append((seconds(tick), m.note, m.velocity))
    ev.sort()
    return ev


TAKE = _find_take_through_cadenza() if BUNDLE_PRESENT else None

pytestmark = pytest.mark.skipif(
    not (BUNDLE_PRESENT and DEMOS.is_file() and TAKE is not None),
    reason="chopin bundle / trained detector / a take through the cadenza not present",
)


def _cadenza_notes() -> list[tuple[float, int, int]]:
    """The recorded cadenza up to and including the landing dyad, at t0=0.

    Cut at the landing so the performer is silent through the interlude, which
    is what the score asks and what makes the interlude playable.
    """

    assert TAKE is not None
    ev = _note_events(TAKE)
    landing = max(
        s for s, p, _ in ev
        if p in (56, 90) and {q for tt, q, _ in ev if abs(tt - s) < 0.15} >= {56, 90}
    )
    window = [e for e in ev if landing - 16 <= e[0] <= landing + 0.1]
    o = window[0][0]
    return [(s - o, p, v) for s, p, v in window]


def _build_engine():
    from aimusic.accompaniment.bundle_v2 import BundleLoaderV2, BundleRegistry
    from aimusic.accompaniment.runtime_projection import (
        project_bundle_v2_to_provisional_runtime,
    )

    root = paths.score_bundle_dir("chopin_op11", 2)
    loaded = BundleLoaderV2.load(root)
    projection = project_bundle_v2_to_provisional_runtime(
        bundle_id=loaded.manifest.bundle_id,
        revision=loaded.manifest.revision,
        registry=BundleRegistry(),
        explicit_root=root,
    )
    bundle = projection.bundle
    clock = ManualClock()
    output = CapturingOutput()
    engine = LiveEngine.from_bundle(
        bundle=bundle,
        config=RuntimeConfig(run_id="cadenza-integration"),
        clock=clock,
        follower=ReferencePitchFollower(bundle.solo_events),
        output=output,
        trace_sink=NullTraceSink(),
    )
    return engine, clock, output, bundle


def test_the_orchestra_plays_the_interlude_after_the_handoff() -> None:
    engine, clock, output, bundle = _build_engine()
    detector = _cadenza_detector_for(bundle)
    assert detector is not None, "no cadenza detector built from the bundle"
    region = next(s for s in bundle.section_map.sections if str(s.mode) == "FREE")

    engine.start(start_beat=region.start_beat)

    notes = _cadenza_notes()
    clk = 0.0
    # Interleave ticks between notes so the silent interlude gets clock time to
    # play, exactly as it does live. Feeding all notes then ticking would let a
    # single burst race the transport past the interlude.
    for target, pitch, velocity in notes:
        while clk + 0.02 < target:
            clk += 0.02
            clock.wait_until(clk)
            engine.tick()
        clk = max(clk, target)
        clock.wait_until(clk)
        engine.process_note(PerformedNote(pitch=pitch, velocity=velocity, perf_time=clk))

    # The performer is silent through m.104; tick the interlude out.
    for _ in range(400):
        clk += 0.02
        clock.wait_until(clk)
        engine.tick()
    engine.stop("done")

    played = [event for _, event in output.sent]
    interlude = [e for e in played if e.event.beat >= region.end_beat]
    assert len(interlude) >= 10, (
        f"orchestra played {len(interlude)} notes in the interlude; the handoff "
        "moved the cursor but never began the LEAD section"
    )
    assert all(e.section_mode.name == "LEAD" for e in interlude)


def test_a_handoff_that_only_moves_position_would_be_caught() -> None:
    """Guards the assertion itself: firing is necessary but nowhere near enough.

    The prior harness declared success on the handoff event alone and so was
    blind to an orchestra that played nothing. This records that the meaningful
    signal is emitted notes.
    """

    engine, clock, output, bundle = _build_engine()
    region = next(s for s in bundle.section_map.sections if str(s.mode) == "FREE")
    engine.start(start_beat=region.start_beat)
    # No cadenza notes at all: nothing should reach the interlude.
    for _ in range(50):
        engine.tick()
    interlude = [e for _, e in output.sent if e.event.beat >= region.end_beat]
    assert interlude == []
