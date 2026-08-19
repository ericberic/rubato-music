# Source: Magenta RealTime 2

## Source

- Product page: https://magenta.withgoogle.com/magenta-realtime-2
- Apps/plugins page: https://magenta.withgoogle.com/mrt2
- Repository: https://github.com/magenta/magenta-realtime
- Model card: https://huggingface.co/google/magenta-realtime-2
- Prior paper: https://arxiv.org/abs/2508.04651
- Codec paper: https://arxiv.org/abs/2508.05207

## Date Read

2026-06-14

## Why It Matters

Magenta RealTime 2 is the newest relevant local AI-music system for Rubato. It
advances real-time, controllable neural audio generation, especially because it
runs locally on Apple Silicon and accepts MIDI control.

It does not replace Rubato's score follower or deterministic accompaniment
scheduler.

## Key Facts

- First MRT2 release is `2.0.2` on 2026-06-04.
- Code is Apache 2.0; model weights are CC-BY-4.0 with additional responsible-use
  terms in the model card.
- The Python package is `magenta-rt`; it supports JAX and optional MLX backends.
- The repository includes a C++ streaming inference engine for Apple Silicon and
  examples for standalone apps, AUv3, Jam, Collider, Pure Data, SuperCollider,
  and Max/MSP.
- MRT2 streams 48 kHz stereo audio with low-latency control from text, audio
  examples, and MIDI.
- Google reports a 40 ms frame size and about 200 ms control latency.
- `mrt2_small` has 230M parameters and runs real-time on any Apple Silicon
  MacBook, including MacBook Air.
- `mrt2_base` has 2.4B parameters and requires a Pro/Max-class Apple Silicon
  machine for real-time streaming.
- The model stack is SpectroStream audio tokens, MusicCoCa text/audio style
  embeddings, and a decoder-only Transformer that consumes context, style, and
  MIDI pitch-state tokens.
- The MIDI control representation is a 128-dimensional pitch-state vector per
  frame, not a full symbolic score with measures, parts, articulations, and
  orchestration guarantees.
- The current model card says detailed MRT2 evaluation results are forthcoming.
- Future updates are expected to include supervised fine-tuning.

## Design Lessons For Rubato

- MRT2 is relevant to Rubato's sound-quality problem, not the core score-location
  problem.
- Keep Matchmaker/ACCompanion as the MVP path for `live MIDI -> score_beat ->
  scheduled accompaniment MIDI`.
- Consider MRT2 later as a neural audio renderer or texture layer controlled by
  Rubato's scheduled accompaniment MIDI and style prompts such as `string
  ensemble`.
- Treat MRT2 output as generative audio. It may be musically useful, but it is
  not guaranteed to play the exact Chopin orchestral notes or preserve entrances
  required by concerto rehearsal.
- For Eric's likely MacBook Air target, only `mrt2_small` should be assumed
  real-time until hardware tests prove otherwise.
- A future spike should measure end-to-end latency, attack precision, and
  harmonic drift against the Chopin excerpt before making MRT2 part of the
  performance path.

## MVP Decision

MRT2 does not change the MVP stack. Rubato should continue with:

```text
Yamaha MIDI -> score follower -> tempo/section policy -> MIDI scheduler -> Yamaha synth
```

MRT2 can become a later optional output target:

```text
Rubato scheduled MIDI + style prompt -> MRT2 -> 48 kHz stereo audio
```

This is deferred because the MVP needs exact synchronization and reproducible
symbolic traces more than neural audio color.

## Caveats

- It is not a score follower.
- It does not expose score position or confidence.
- It does not accept a canonical concerto score bundle as its main contract.
- It may hallucinate instrumentation, harmony, or texture.
- About 200 ms control latency is promising for live generative instruments but
  may be too loose for tight classical note attacks.
- The audio pipeline would add device, sample-rate, buffering, and licensing
  surface area that the MIDI-only MVP intentionally avoids.
- Generated outputs must be reviewed for copyright and responsible-use terms if
  used beyond private experimentation.

## Links

- Related decision: [Decision 0003](../decisions/0003-magenta-rt2-renderer-research.md)
- Related concept: [Accompaniment Control](../concepts/accompaniment-control.md)
- Related design: [System Design](../SYSTEM_DESIGN.md)
- Related strategy: [ML and MIR Strategy](../ML_STRATEGY.md)
