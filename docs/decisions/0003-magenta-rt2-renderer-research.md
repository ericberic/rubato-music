# Decision 0003: Defer Magenta RealTime 2 To Rendering Research

## Status

Accepted for MVP scope on 2026-06-14.

## Context

The user asked whether the latest Magenta RealTime releases advance state of the
art for Rubato's use-case: real-time accompaniment for Chopin Piano Concerto No.
1 while the soloist plays the solo part on a MIDI-capable piano.

## Decision

Do not replace the Matchmaker/ACCompanion-inspired symbolic accompaniment stack
with Magenta RealTime 2 for the MVP.

Track Magenta RealTime 2 as a later optional neural audio renderer or generative
texture layer controlled by Rubato's scheduled MIDI and style prompts.

## Rationale

- MRT2 is a major advance for local, open-weights, low-latency music audio
  generation on Apple Silicon.
- MRT2 accepts MIDI conditioning, text prompts, and audio style examples, which
  is useful for future accompaniment sound rendering.
- Rubato's first hard problem is exact score location and concerto-appropriate
  synchronization, not open-ended music generation.
- MRT2 does not output `score_beat`, confidence, or deterministic orchestral
  events, and it cannot guarantee the exact Chopin accompaniment.
- The MIDI-only MVP avoids audio buffering, sample-rate, and generative-output
  risks while the tracking/scheduling machinery is still being built.

## Consequences

- Keep Yamaha CLP-795GP GM/XG synth playback as the first output target.
- Keep IAC/Logic/Kontakt/VST as the first higher-fidelity deterministic upgrade.
- Add a future spike for `scheduled MIDI -> MRT2 -> audio` only after the Chopin
  excerpt works through the symbolic pipeline.
- Do not add `magenta-rt` to project dependencies until there is a focused spike
  with hardware and latency acceptance criteria.

## Future Spike Criteria

A Magenta RT2 spike should answer:

- Can `mrt2_small` run reliably on target hardware at 48 kHz without dropouts?
- What is measured end-to-end MIDI-to-audio latency through the app/plugin path?
- Does MIDI steering follow fast Chopin accompaniment rhythms tightly enough?
- Does it preserve the intended harmony and orchestral role from scheduled MIDI?
- Is the audio more useful than Yamaha GM/XG or a simple DAW/VST route for the
  first recital-rehearsal MVP?

## Related

- [Magenta RealTime 2](../sources/magenta-realtime-2.md)
- [Decision 0002](0002-tracker-and-synth-mvp.md)
- [System Design](../SYSTEM_DESIGN.md)
- [ML Strategy](../ML_STRATEGY.md)
