# Decision 0002: Start With Matchmaker Wrapper And Yamaha Synth

## Status

Accepted for MVP spike.

## Context

Rubato needs live accompaniment for Chopin Piano Concerto No. 1. The user asked
whether ACCompanion, HeurMiT, Yamaha CLP-795GP synth playback, raw audio, or a
custom approach should be the starting point.

## Decision

Use Matchmaker as the first score-following integration target, while using
ACCompanion as the reference architecture for full accompaniment behavior.

Use the Yamaha CLP-795GP internal GM/XG synth as the first output target. Keep
IAC/Logic/Kontakt/VST output as a later quality upgrade.

Do not start from HeurMiT for the MVP. Keep it as a research reference only.

## Rationale

- ACCompanion demonstrates the desired behavior: live MIDI input, score
  following, tempo/dynamic/articulation adaptation, and MIDI accompaniment
  output.
- ACCompanion's public repository is older/heavier and is better treated as a
  design reference before direct integration.
- Matchmaker has a cleaner Python package/API for score-following experiments,
  simulation mode, live MIDI device mode, and Web MIDI byte-stream integration.
- HeurMiT explicitly reports that it is not practical for real-world score
  following yet, despite its computational efficiency.
- The CLP-795GP has MIDI IN/OUT/THRU, USB TO HOST, 256 polyphony, and XG/GM
  voices, making it sufficient for proof-of-concept accompaniment playback.
- Yamaha synth playback avoids DAW/VST setup friction during the first MVP, even
  though orchestral realism will be limited.

## Consequences

- The first tracker API should be follower-agnostic:
  `MIDI events + score bundle -> score_beat, perf_time, confidence, raw_state`.
- The scheduler must consume score position and timing/tempo state, not tempo
  alone.
- The first renderer should target simple GM/XG program/channel mappings.
- PWA/backend design is required because Eric needs start/stop, device selection,
  volume, panic, restart, and source-ingestion workflows at the piano.

## Related

- [Matchmaker](../sources/matchmaker.md)
- [ACCompanion](../sources/accompanion.md)
- [HeurMiT](../sources/heurmit.md)
- [Yamaha CLP-795GP](../sources/yamaha-clp-795gp.md)
- [Realtime Performance Dataflow](../concepts/realtime-performance-dataflow.md)
- [PWA Rehearsal UI](../concepts/pwa-rehearsal-ui.md)
