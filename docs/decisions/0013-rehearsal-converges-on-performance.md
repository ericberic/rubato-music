# Decision 0013 — Rehearsal converges on the live performance

Status: **Accepted** (2026-07-29; amended 2026-08-10)

## The principle

> Rehearsals get us closer and closer to the live performance, just like real
> musicians. — Soloist

Every rehearsal pass is a joint attempt at the real thing. The accompanist plays
with everything it has learned so far; the performer adapts to what they hear;
the take is recorded and learned from; the next pass starts from more. Performer
and accompanist converge on a shared interpretation.

This is the organizing principle for the whole rehearsal subsystem. Where an
earlier design treated rehearsal as *data collection* — the orchestra sets a
pulse, gets out of the way, and the performer supplies clean solo evidence —
that is now understood as the **cold-start exception**, correct only when there
is nothing yet to converge from.

## What follows from it

### 1. A rehearsal pass *is* the live performance, recorded

With prior data, a pass should run the same path as performing — follower, tempo
model, accompaniment — and simply also be recorded. There is no separate
"rehearsal mode" behaviour to design; there is performing, plus capture.

*Status*: implemented. `start_follow` always writes the performed notes it
already receives under the public runtime `run_id`
(`_write_captured_performance`) and owns accompanied recording entry points. See
[Decision 0015](0015-follower-always-exists.md).

Every run is proactively captured, but capture is not the same thing as
committing rehearsal evidence. **Perform live** leaves an ephemeral,
publicly-named recording beside its runtime trace so the performer can hear it,
reference it while debugging, promote it into the rehearsal take store, or do
nothing. **Record pass** performs that promotion as part of its explicit
workflow. The old request-level `keep_recording` switch is unnecessary:
recording is an engine invariant; durable take membership is the later user
decision.

### 2. Cold start changes the prior, not the path

With no evidence at a passage, the same follower still exists and reacts from
the movement default rather than a learned interpretation. Expectation and
`sections.json` still determine authority per cell. There is no take-level
cold-start mode and no “trail off at entry” boolean.

### 3. The lead-in preserves the orchestra's performance

The lead-in exists so the passage is in the performer's body before they enter.
While the pianist is silent, that passage is the Oguri orchestra performance:
its source-time intervals pass through unchanged under one global tempo scale.
The canonical beat map still projects score identity and the entry location,
but neither its local slopes nor the fitted rehearsal pace may push and pull
individual orchestral beats. The learned Interpretation resumes when FOLLOW
authority transfers to the pianist.

### 4. The orchestra starts where the performer points, and waits

The performer selects the measure the orchestra starts from and enters whenever
they are ready at any point after it. The cue plays open-endedly, so warming up
for longer is just waiting.

Superseded: the cue used to start two measures *before* the selection and stop at
a *predicted* entry beat. Both existed to make a fixed-length cue land correctly,
and both were surprising — selecting m.52 started the orchestra at m.50 and
silenced it exactly where m.52's own orchestral interlude began.

### 5. Readiness measures convergence, not effort

If rehearsal converges, the right question is "how close are we?", not "how many
takes?". See [Decision 0012](0012-performance-readiness.md): readiness is derived
from what the accompanist must *do* at each position and whether it has what that
requires — agreement between takes where the performer leads, nothing at all
where the orchestra leads.

### 6. Take provenance matters more, not less

Once the orchestra follows, a take records the performer responding to a model
that is responding to them. That is ecologically valid — it is the condition of
real performance — but it is no longer the same *kind* of evidence as clean solo
playing. Takes must record how they were captured (issue #143) so the fit can
weight or separate them.

## Consequences

- Live and accompanied recording now share one causal path. The score cursor
  uses the follower's corrected position during a recorded pass; an open-loop
  playback transport is only a fallback for fixed audition/review.
- Readiness can *fall* when a take is added, if it disagrees with the others.
  That is honest: convergence is not monotonic.
- "Rehearsal mode" is not a feature to build. It is performing, recorded.

## Links

- [Decision 0012](0012-performance-readiness.md) (readiness as confidence)
- [Decision 0010](0010-human-beat-anchors.md) (entry contract, anchors)
- [Decision 0009](0009-rehearsal-as-dataset-lifecycle.md) (dataset lifecycle)
- [Decision 0008](0008-predictive-follow-clock.md) (rehearsal-anchored pace)
