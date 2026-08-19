# Source: ACCompanion

## Source

- Repository: https://github.com/CPJKU/accompanion
- Paper: https://www.ijcai.org/proceedings/2023/0641.pdf

## Date Read

2026-06-14

## Why It Matters

ACCompanion is the closest known open-source reference system for Rubato's
desired MVP: symbolic real-time accompaniment from MIDI input to MIDI output.

## Key Facts

- Works in the symbolic domain with MIDI-capable instruments.
- Separates score following from expressive accompaniment generation.
- Uses MIDI input/output and score information.
- Models tempo, dynamics, and articulation of the soloist.
- Includes HMM and OLTW score-following approaches.
- The IJCAI paper reports OLTW outperforming HMM on complex Romantic piano
  examples.
- The paper is candid that trust, starts, complex passages, and shared musical
  intention remain difficult.

## Design Lessons For Rubato

- Rubato should preserve ACCompanion's modular split: score follower, tempo
  model, accompaniment renderer, and MIDI routing.
- For Chopin, pre-recorded Eric reference takes may be useful for OLTW/LTE-style
  rehearsal-specific priors.
- A pure HMM follower is unlikely to be enough for dense Romantic material.
- A purely reactive tempo model is musically dangerous; it can amplify mistakes
  or pauses.
- Rubato needs explicit follow/lead policy. The paper identifies this as a
  missing fourth task beyond note detection, score following, and accompaniment
  generation.
- Starts and entrances after rests need special handling, because human players
  rely on visual/gestural communication in those moments.

## Caveats

- The public repo uses a conda/Python 3.9-style environment and several heavy or
  older dependencies.
- It should be treated as reference architecture first, not copied wholesale into
  Rubato without an integration spike.
- Full concerto accompaniment remains harder than short excerpt demos.
- The included repository data/models have non-commercial Creative Commons terms;
  code is Apache 2.0, but bundled data/model artifacts should not be assumed
  usable in all contexts.

## Links

- Detail page: [ACCompanion Mechanics](accompanion-mechanics.md)
- Detail page: [ACCompanion Evaluation](accompanion-evaluation.md)
- Related concept: [Score Following](../concepts/score-following.md)
- Related concept: [Accompaniment Control](../concepts/accompaniment-control.md)
- Related decision: [Pivot to Live Accompanist](../decisions/0001-pivot-live-accompanist.md)
- Related runbook: [First Chopin MVP Excerpt](../runbooks/chopin-mvp.md)
