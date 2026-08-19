# Source: HeurMiT

## Source

- Paper: https://arxiv.org/abs/2503.06348

## Date Read

2026-06-14

## Why It Matters

HeurMiT is relevant because it explores neural score following for computer
accompaniment, but it is not a practical MVP engine for Rubato.

## Key Facts

- Uses a compact neural model called Tyke plus heuristic safeguards.
- Learns latent representations from piano-roll-like performance and score
  inputs.
- Includes MIDI augmentation ideas through MIDIOgre.
- The paper states that the approach is not ready for real-world applications.
- It reports losing track more frequently than a baseline in some cases.
- It struggles when performance tempo differs significantly from the score.

## Design Lessons For Rubato

- Do not use HeurMiT as the MVP follower.
- Neural compression plus cross-correlation may be useful later for a fast
  fallback or confidence signal.
- Any neural follower must solve tempo-scale invariance before it is useful for
  Romantic rubato.
- Rubato should log repeated-pattern failures explicitly, because Chopin concerto
  material can contain similar figurations and recurrences.
- Onset-only and multi-scale matching are worth tracking as future research
  ideas, but not first implementation targets.

## Caveats

- Useful as research direction, not production dependency.
- Neural score following may become relevant later after deterministic baselines
  are in place.

## Links

- Detail page: [HeurMiT Mechanics](heurmit-mechanics.md)
- Detail page: [HeurMiT Evaluation](heurmit-evaluation.md)
- Related concept: [Score Following](../concepts/score-following.md)
- Related source: [Matchmaker](matchmaker.md)

## Decision Impact

HeurMiT should not be the MVP engine. It can become a future comparison target
after Rubato has deterministic score-bundle ingestion, Matchmaker baselines, and
trace-based evaluation on Chopin excerpts.
