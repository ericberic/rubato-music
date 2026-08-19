# PRD - Chopin Live Accompanist MVP

## Product Goal

Rubato gives Eric a real-time orchestral accompanist for Chopin Piano Concerto
No. 1 in E minor. Eric plays the solo piano part on his Yamaha CLP-795GP;
Rubato follows his score position and timing and plays the orchestra part back
through the Yamaha synth, a configured local orchestral-audio zone, or both, in
sync with his rubato. The current room renderer hosts BBCSO through Pedalboard
and sends its audio to a named CoreAudio device such as the LG soundbar.

The product is local-first and symbolic-first: everything runs on Eric's
machine, and the musical representation is MIDI/MusicXML, not audio. The pivot
rationale is recorded in
[Decision 0001](decisions/0001-pivot-live-accompanist.md). Product intent,
the rehearsal/performance loop, the learning model, and the interface design
are owned by [Vision and UX Design](VISION_AND_UX_DESIGN.md); this PRD owns
scope, success criteria, users, and non-goals.

## Users

- **Eric (soloist)** is the only end user. He is at the piano, mid-rehearsal,
  and needs low-friction controls: pick an excerpt, start, play, stop or
  silence instantly when something goes wrong, and leave quick feedback.
- **Agents (operators)** prepare score bundles, run experiments, and analyze
  rehearsal traces between sessions. The product must leave enough artifacts
  behind that agents can iterate without Eric present.

## Scope

The MVP covers, in rough delivery order (see the MVP Build Order in
[AGENTS.md](../AGENTS.md) and progress in the [Knowledge Log](LOG.md)):

- **Score bundles**: canonical symbolic excerpts of Op. 11 with solo reference,
  accompaniment events, section map, and source references. The first live
  target is the second movement via the Oguri MIDI, chosen because it is slower
  and easier to sight-read.
- **Offline render**: align a recorded solo take against the solo reference and
  render retimed accompaniment MIDI deterministically.
- **Simulated online**: replay recorded takes causally through the real runtime
  path to validate behavior reproducibly.
- **Live rehearsal**: read live Yamaha MIDI input, follow the score, apply
  section policy (follow/lead/hold/stop), and emit accompaniment to the Yamaha
  synth, configured BBCSO/CoreAudio zones, or both.
- **Interpretation profile**: learn Eric's tempo and dynamic shape from kept
  rehearsal takes so the orchestra anticipates rather than merely reacts;
  learning is a byproduct of playing, never of labeling (see the learning
  model in [Vision and UX Design](VISION_AND_UX_DESIGN.md)).
- **Rehearsal PWA**: a local FastAPI-backed at-piano interface for device
  checks, cued recording, rendering, playback, review, and feedback. The
  target three-face design is owned by
  [Vision and UX Design](VISION_AND_UX_DESIGN.md); the current cockpit
  implementation lives in [PWA Rehearsal UI](concepts/pwa-rehearsal-ui.md).
- **Rehearsal data**: every run writes traces, MIDI artifacts, metrics, and
  human feedback as first-class data under DVC-managed directories.

Architecture and data contracts are owned by
[System Design](SYSTEM_DESIGN.md); the five-minute overview is the
[Architecture Brief](ARCHITECTURE_BRIEF.md).

## Success Criteria

- Eric can rehearse a 1-3 minute excerpt with live MIDI input and
  accompaniment timing he judges acceptable. This is also the revisit trigger
  for [Decision 0001](decisions/0001-pivot-live-accompanist.md).
- Entrances land: the orchestra neither rushes ahead of nor lags behind solo
  entries, follows Eric's rubato in FOLLOW sections, and holds steady in LEAD
  sections (see [Section Policy](concepts/section-policy.md)).
- Rehearsal compounds: kept takes sharpen the interpretation profile with no
  labeling or configuration effort, and everything learned in rehearsal
  carries to performance automatically.
- The at-piano UI passes the furniture test from
  [Vision and UX Design](VISION_AND_UX_DESIGN.md): glanceable from two
  meters, operable in one press, no text entry at the piano.
- Recovery is instant: stop/silence controls halt sound immediately, and a bad
  run never requires restarting the stack or losing the take.
- Every rehearsal run leaves reviewable artifacts — solo take, rendered or
  emitted accompaniment, score-position/tempo/mode traces, and feedback notes —
  so agents can diagnose timing problems offline.
- The offline and simulated-online paths stay covered by deterministic base
  tests, so regressions are caught without hardware.

Playability is judged by Eric's rehearsal feedback rather than a fixed latency
number; traces record the timing data needed to debug his reports.

## Non-Goals

- Eric-style piano generation, style transfer, or ERIC-vs-OTHER A/B output
  (deferred, per [Decision 0001](decisions/0001-pivot-live-accompanist.md)).
- Raw-audio score following, analysis, or neural generation; the implemented
  BBCSO path remains symbolic MIDI driving a deterministic local instrument
  renderer (see [Decision 0003](decisions/0003-magenta-rt2-renderer-research.md)).
- Cloud runtime, training, or inference; the system runs entirely locally.
- A full score editor; source prep stays file editing plus a thin UI.
- General DAW hosting or arbitrary plug-in routing. The bounded BBCSO/Pedalboard
  room renderer is the current higher-fidelity path; IAC/Logic/Kontakt remain
  optional future integrations (see
  [Decision 0016](decisions/0016-pedalboard-decoupled-spatial-synth.md)).
- Multi-user or hosted product; this is a single-user local tool.

## Related

- [Vision and UX Design](VISION_AND_UX_DESIGN.md)
- [Decision 0001](decisions/0001-pivot-live-accompanist.md)
- [Architecture Brief](ARCHITECTURE_BRIEF.md)
- [System Design](SYSTEM_DESIGN.md)
- [PWA Rehearsal UI](concepts/pwa-rehearsal-ui.md)
- [Section Policy](concepts/section-policy.md)
