# Decision 0001: Pivot To Live Accompanist MVP

## Status

Accepted on 2026-06-14.

## Context

Rubato started as a symbolic style-transfer project for generating soloist-style vs
generic-style performances.

## Decision

Pivot Rubato to a **live accompanist MVP**:

> Provide real-time orchestral accompaniment for Chopin Piano Concerto No. 1 in
> E minor while the soloist plays the solo piano part on a MIDI-capable piano.

## Rationale

- Live accompaniment solves a clear, high-value problem for soloist practice.
- The style-transfer path required heavy training loops before proving basic utility.
- Symbolic score following, tempo tracking, and accompaniment scheduling provide
  immediate interactive feedback.

## Consequences

- Documentation must optimize for live accompaniment.
- Score following and section control become core modules.
- A/B SOLOIST vs OTHER generation is legacy/future work.
- Matchmaker and ACCompanion become priority research/integration references.
- Rehearsal artifacts and human feedback become first-class data.

## Revisit Trigger

Revisit after Rubato can perform a 1-3 minute excerpt with live MIDI input and
acceptable accompaniment timing.
