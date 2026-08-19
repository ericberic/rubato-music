# Decision 0001: Pivot To Live Accompanist MVP

## Status

Accepted on 2026-06-14.

## Context

Rubato started as a symbolic style-transfer project for generating Eric-style vs
OTHER-style piano MIDI. The user clarified that the immediate desired outcome is
real-time orchestral accompaniment while playing the Chopin E minor concerto
solo part on a Yamaha MIDI piano.

## Decision

Rubato's active MVP is now live accompaniment for Chopin Piano Concerto No. 1 in
E minor. Style transfer is deferred until the underlying score, MIDI, alignment,
tracking, and feedback infrastructure is reliable.

## Consequences

- Documentation must optimize for live accompaniment.
- Score following and section control become core modules.
- A/B ERIC vs OTHER generation is legacy/future work.
- Matchmaker and ACCompanion become priority research/integration references.
- Rehearsal artifacts and human feedback become first-class data.

## Revisit Trigger

Revisit after Rubato can perform a 1-3 minute excerpt with live MIDI input and
acceptable accompaniment timing.
