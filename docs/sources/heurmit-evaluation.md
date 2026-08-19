# Source Detail: HeurMiT Evaluation

## Source

- Parent source note: [HeurMiT](heurmit.md)
- Paper: https://arxiv.org/abs/2503.06348

## Results

Against the Flippy OLTW baseline on `(n)ASAP`, HeurMiT had lower latency but
higher misalignment rate.

At a 100 ms misalignment threshold:

- Flippy misalignment rate: 54.05%.
- HeurMiT misalignment rate: 77.24%.
- HeurMiT latency: about 1.1 ms on CUDA and 6.07 ms on CPU.
- Flippy latency: about 1678.77 ms in the paper's comparison setup.

The latency comparison is not apples-to-apples because the systems operate very
differently; the paper explicitly avoids claiming a clean practical win from the
latency result.

## Failure Modes

- Significant tempo mismatches break the cross-correlation assumption.
- More than roughly +/-5 BPM mismatch caused misalignment rate to exceed 50% in
  one analysis.
- Early stabilization can fail because the heuristic buffer lacks history.
- Repeated or similar musical patterns can cause jumps to the wrong location.
- Hyperparameters are performance-sensitive.
- Listening tests revealed perceptible lag even when metrics looked acceptable.

## Rubato Implication

HeurMiT should stay in the research bucket for now. It is useful for future ML
ideas, but it does not replace OLTW/HMM-style baselines for the Chopin MVP.

## Related

- [HeurMiT](heurmit.md)
- [Score Following](../concepts/score-following.md)
