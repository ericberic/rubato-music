# Architecture Brief - Chopin Live Accompanist MVP

## Intent

Rubato's MVP is a local, symbolic AI accompanist for Chopin Piano Concerto No. 1
in E minor. The soloist plays the solo piano part on a MIDI-capable piano; Rubato follows
their score position and timing, decides whether the orchestra should follow or
lead by section, and emits synchronized symbolic accompaniment to the keyboard
synth, configured Pedalboard/BBCSO audio zones, or both.

The product is not currently style transfer, composition, or raw audio. Style can
matter later, but first the system must reliably connect score, solo MIDI,
accompaniment MIDI, timing, traces, and rehearsal feedback.

## Three Workflows

Rubato has three workflows:

1. **Offline score bundling:** score sources become a canonical bundle with a
   solo reference, accompaniment events, source-to-beat maps, PDF geometry,
   section policy, and instruments.
2. **Rehearsal takes:** completed MIDI takes align symbolically to the
   bundle, producing performed-time warps, review playback, score coverage, and
   the soloist's interpretation profile.
3. **Live performance:** incoming MIDI drives the causal score follower;
   its position plus the frozen rehearsal profile feeds the tempo model,
   section policy, and scheduler that emits accompaniment through the selected
   MIDI/audio outputs.

Rehearsal and performance deliberately share real-time components, but they are
not one workflow. Rehearsal may perform background alignment and update learned
artifacts after Stop. Live performance only reads prepared bundle/profile state
on the musical critical path. See [System Design](SYSTEM_DESIGN.md)'s
"Three-Workflow Architecture" for the maintained derivation graph.

## Five-Minute Inference Model

Rubato does not treat the PDF, the reference MIDI, and a recorded take as three
competing clocks. They answer different questions in a fixed order:

```text
offline score bundle:
  symbolic score -> score/reference correspondence -> canonical beat map

rehearsal take:
  take/reference correspondence -> performed-time warp -> review + profile

live performance:
  incoming notes -> causal canonical position + profile prior -> accompaniment
```

- **The score answers where.** Audiveris MusicXML supplies notated measures,
  beats, notes, and PDF geometry. The canonical timeline gives those locations
  stable internal ticks.
- **Symbolic matching answers which.** Pitch, chord, contour, interval, and
  order evidence identify which reference and performance MIDI events express
  each score location. Sparse human landmarks constrain ambiguous regions.
- **The performance clock answers when.** Once identities are matched, MIDI
  timestamps describe the soloist's rubato and fit the score-to-wall-time warp.
- **The profile separates pace from shape.** Each take has a robust baseline
  seconds-per-quarter; each canonical half-quarter cell stores its local
  seconds-per-quarter and the dimensionless ratio to that baseline. Repeated
  takes robustly fuse those observations at integer `score_tick` locations.
- **Geometry answers where to paint.** The canonical position is mapped to the
  PDF only after it is known; pixels never determine musical time.

This is the core invariant: **elapsed time can stretch a beat but cannot invent
or relabel one**. The full derivation graph and source-ownership table live in
[System Design](SYSTEM_DESIGN.md); the rationale and superseded calibration
approach live in [Decision 0006](decisions/0006-canonical-beat-evidence-fusion.md).

## Starting Choices

- First tracker wrapper: Matchmaker.
- Reference architecture: ACCompanion.
- Research-only for now: HeurMiT/neural score following.
- Reliable fallback: MIDI Piano GM/XG synth.
- Current higher-fidelity experiment: a machine-local Pedalboard/BBCSO room
  zone through named CoreAudio output.
- Primary rehearsal interface: local FastAPI-backed PWA.

## Build Sequence

1. Prepare a short Chopin excerpt as a score bundle.
2. Record/upload solo MIDI and render accompaniment offline.
3. Replay the same solo MIDI causally through the online path.
4. Switch input/output adapters to live MIDI.
5. Iterate from traces, latency metrics, and playability feedback.
