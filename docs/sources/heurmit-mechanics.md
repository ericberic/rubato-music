# Source Detail: HeurMiT Mechanics

## Source

- Parent source note: [HeurMiT](heurmit.md)
- Paper: https://arxiv.org/abs/2503.06348

## Summary

HeurMiT frames score following as neural local template matching over piano-roll
windows and score contexts.

## Method

- Convert score and performance into piano-roll matrices over 128 MIDI pitches
  and discrete time.
- Treat the recent solo performance as a `window`.
- Treat the likely score region near the previous prediction as a `context`.
- Encode both with neural convolutional encoders.
- Cross-correlate the encoded window against the encoded context.
- Use heuristics over recent predictions to pick a plausible score position.

## Tyke Model

- `Ew` encodes the window.
- `Ec` encodes the context.
- Cross-correlation produces a probability-like vector over context positions.
- Training uses cross-entropy against the true window position.

MiniTyke example:

- One Conv1d + ReLU stack for context.
- One Conv1d + ReLU stack for window.
- 64 latent channels.
- Kernel size 3.
- 49,280 parameters.

## Runtime Heuristics

Best reported configuration:

- Inference frequency: 10 Hz.
- Window: about 5.21 seconds.
- Context: about 13.02 seconds.
- Moving average smoothing over model output.
- Ring buffer of the last 20 predictions.
- Initial 5 predictions used as stabilization.

## MIDIOgre

MIDIOgre synthesizes performance imperfections:

- Pitch shifts.
- Onset-time shifts.
- Duration shifts.
- Note deletions.
- Note additions.

The ablation did not show a clear robustness gain in the current HeurMiT system,
but symbolic augmentation may matter later for learned Rubato components.

## Related

- [HeurMiT](heurmit.md)
- [Score Following](../concepts/score-following.md)
