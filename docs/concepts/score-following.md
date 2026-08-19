# Score Following

## Summary

Score following estimates a live or recorded performance's current position in a
known score. For Rubato, the input is Eric's solo piano MIDI and the score is
Chopin Piano Concerto No. 1.

## Current Recommendation

Start with packaged symbolic/probabilistic approaches before neural methods:

- Use Matchmaker for integration experiments.
- Use ACCompanion as a reference for full accompaniment behavior.
- Keep local abstractions thin so followers can be swapped.
- Evaluate MIDI-input methods on Rubato's own Chopin excerpt before committing
  to one follower.

## Details

Rubato needs both offline and online score following:

- Offline alignment validates score preparation and recorded rehearsals.
- Simulated online replay tests causal behavior deterministically.
- Live following uses Yamaha MIDI input.

The output should be a stream of score positions in beats plus confidence and
timing metadata when available.

Matchmaker 0.3's live MIDI generator currently supplies score position but not
a calibrated confidence or lock probability. Rubato therefore distinguishes
the raw estimate from a local warm-up heuristic: three monotonic, bounded-jump
updates permit a provisional configured confidence. This is only an output
gate until Movement 2 replay and Yamaha tracking-error measurements calibrate a
real policy.

## Method Families

### HMM / Probabilistic Followers

HMM followers model hidden score position and infer the most likely state from
observed performance events. ACCompanion's HMM combines score-position states
with a Kalman-filter tempo component.

Benefits:

- Natural way to represent uncertainty.
- Can model insertions and local errors.
- Often available for MIDI input.

Risks:

- Can become sticky around sustained notes or local regions.
- ACCompanion and Matchmaker both report weaker performance than OLTW variants
  on complex piano material.

### OLTW Followers

Online Time Warping is causal dynamic programming for aligning a streaming
performance to a known reference sequence.

Benefits:

- Stronger reported accuracy and coverage than HMM in current primary sources.
- Handles continuous progress more smoothly.
- ACCompanion shows that reference performances can improve robustness.

Risks:

- Depends on feature design and window/step parameters.
- For Rubato, MIDI-input OLTW behavior must be tested directly.
- Reference-performance workflows require extra rehearsal recordings and offline
  alignment.

### Neural Template Followers

HeurMiT/Tyke encodes piano-roll windows and contexts into latent representations
and uses cross-correlation plus heuristics.

Benefits:

- Low fixed inference cost.
- Research path for learned robustness to symbolic deviations.

Risks:

- Current HeurMiT results are not practical for real-world accompaniment.
- Tempo-scale mismatch is a fundamental failure mode.
- Repeated patterns can cause score-position jumps.

## Rubato Interface Target

Internal followers should expose a small common contract:

```text
input: timestamped MIDI events
output: score_beat, performance_time, optional confidence, optional raw_state
```

The tempo model and accompaniment scheduler should depend on this contract, not
on Matchmaker or ACCompanion internals.

## MVP Evaluation Plan

For the first Chopin excerpt:

- Run Matchmaker simulation mode on recorded solo MIDI.
- Compare `pthmm`, `hmm`, `arzt`, and `dixon` if supported for MIDI input.
- Log score-beat estimates and alignment path.
- Measure beat-domain error where ground truth is available.
- Inspect repeated-pattern and long-rest regions manually.
- Only then run live Yamaha MIDI input.

## Open Questions

- Does Matchmaker's MIDI HMM follower handle the chosen Chopin excerpt well
  enough?
- Is a MIDI OLTW integration available or worth implementing locally?
- How much manual score annotation is needed for repeats, cadenzas, and tutti?

## Related

- [ACCompanion](../sources/accompanion.md)
- [Matchmaker](../sources/matchmaker.md)
- [HeurMiT](../sources/heurmit.md)
