# Source Detail: ACCompanion Mechanics

## Source

- Parent source note: [ACCompanion](accompanion.md)
- Paper: https://www.ijcai.org/proceedings/2023/0641.pdf

## Summary

ACCompanion is a modular symbolic accompanist with MIDI routing, score
following, tempo modeling, and expressive accompaniment generation.

## Runtime Modules

- `MIDI Handler`: routes MIDI input and output.
- `Score Follower`: estimates where the soloist is in the score.
- `Accompanist`: generates accompaniment MIDI from score, follower output, and
  expressive parameters.

## Representation

The score model represents each score note by pitch, score onset in beats, and
score duration. A performed note adds performed onset, duration, and MIDI
velocity.

Chord notes share a score onset, while actual performed notes may be spread
slightly in time. For polyphonic MIDI input, ACCompanion groups incoming
messages into non-overlapping 10 ms windows and treats all notes in the window
as one score position/chord event.

## Followers

The HMM follower is a switching Kalman filter:

- Observed variables: performed MIDI pitches and performed inter-onset
  intervals.
- Hidden variables: score onset states plus insertion states.
- Tempo is modeled in the Kalman-filter part.
- Inference uses the forward algorithm online.

The OLTW follower is causal dynamic programming. Instead of aligning directly to
the score, the stronger version aligns live input to one or more pre-recorded
reference performances already aligned to the score. Multiple OLTW followers can
be ensembled and averaged.

This is the closest research analogue to Rubato's repeated-take plan. The same
aligned take may contribute two different derived products: a robust local
expression prior for the tempo/control layer, and an individual reference path
for a future multiple-reference OLTW follower. Those products must not be
collapsed—median fusion helps the prior, while retaining distinct performances
helps the follower ensemble.

## Expressive Parameters

The Accompanist encodes soloist performance into:

- MIDI velocity for dynamics.
- Beat period for local tempo.
- Microtiming deviation from the chord onset.
- Log articulation ratio: performed duration divided by notated duration at the
  current tempo.

It decodes these parameters into accompaniment note timing, velocity, and
duration.

## Tempo Models

The paper compares:

- `R`: purely reactive IOI-derived beat period.
- `MA`: moving-average beat period.
- `K`: Kalman-filter baseline.
- `L`: linear synchronization/error-correction model.
- `LTE`: linear tempo expectation model using reference performances.
- `JADAM`: joint adaptation/anticipation model.

Key lesson: raw IOI tempo is too jagged for accompaniment. Tempo modeling must
smooth and predict, not just mirror the latest aligned note.

## Related

- [ACCompanion](accompanion.md)
- [Accompaniment Control](../concepts/accompaniment-control.md)
- [Score Following](../concepts/score-following.md)
