# Decision 0012 — Readiness means "will this passage go well", not "how many takes"

Status: **Proposed** (2026-07-29)

## Context

The rehearsal surface currently colours a measure by *coverage*: a measure is
green when every 0.5-beat cell has ≥3 aligned observations and ≥0.5 quality
(`_measure_state` in `src/aimusic/takes/coverage.py`), and yellow otherwise.

That metric fails on its own terms:

- **It asks for impossible work.** m.22 of Movement II has **2 solo notes** in the
  whole bar — an orchestral interlude. Coverage requires ≥3 observations in each
  of its 8 cells, so `min_n` is 0 forever. No amount of recording turns it green.
- **It conflates three unrelated situations** into one yellow: never recorded
  (actionable), nothing to record (not a gap at all), and recorded but
  inconsistent (needs a *better* pass, not another).
- **It measures effort, not outcome.** Three mutually contradictory takes score
  the same as three tightly agreeing ones, though only the latter means the
  accompanist knows what to expect.

The soloist, on seeing an orchestral interlude marked uncertain:

> "In the span where it's really just orchestra, I think it should be certain
> without requiring a ton of my takes covering what, silence? It's not the number
> of takes that matters, it's the confidence or certainty that the performance can
> go well."

## Decision

Replace coverage with **readiness**: the estimated probability that the
accompanist will perform *this passage* well, live, with this performer.

Readiness is derived from **what the accompanist must actually do here**, and
whether it has what that job requires. Three roles, three different evidence
requirements:

| Role at this position | What the accompanist does | What readiness requires |
| --- | --- | --- |
| **Orchestra-led** (piano tacet/sparse) | plays its own plan from the reference | **nothing.** Ready by default |
| **Piano-led** (solo material present) | follows the performer's timing | a *trustworthy expectation* of that timing |
| **Transition** (entry/exit, hand-off) | finds the performer and swaps authority | evidence at the seam specifically |

### Piano-led readiness is agreement, not count

Where the performer leads, the accompanist's real question is "how much do I
trust my learned curve here?" — which the runtime **already computes**, as
`dispersion_trust_gain(expected_period, dispersion)` in `predictive_follow.py`.
It is high when takes agree, zero when they disagree or are unsupported, and it
is what the scheduler actually acts on.

So readiness should *display the signal the runtime already obeys*. Then green
means "the accompanist will trust its plan here", and yellow means "it will fall
back to reactive following" — a literal prediction of live behaviour rather than
a proxy for it. Two consistent takes can be ready; five contradictory ones are
not.

### Transitions are first-class

Entries are where this system has actually failed in practice (the whole cue-in
investigation). A hand-off needs evidence *at the seam* — enough to localize and
to establish pace — not uniform coverage of every cell around it. Readiness must
score entries separately and be allowed to say "the passage is fine, the entry
into it is not".

### States the performer sees

Every state must answer "what, if anything, should I do?":

| State | Meaning | Performer action |
| --- | --- | --- |
| **Orchestra leads** | nothing to learn here | none — and it does not count against readiness |
| **Ready** | takes agree; the accompanist will trust its plan | none |
| **Unsure** | takes disagree here | play it again more consistently, or mark a beat anchor |
| **Unrehearsed** | no evidence and material to play | record a pass |
| **Entry at risk** | the hand-off into this passage is weak | record a pass starting a bar earlier |

"Unsure" and "Unrehearsed" are both yellow today, and they call for opposite
responses. That distinction is the point of the redesign.

## Consequences

- **A movement can reach 100% ready** without recording silence. The headline
  figure becomes achievable and therefore meaningful.
- **Readiness can fall when a take is added** — a wildly different interpretation
  lowers agreement. That is correct and honest; it is information the current
  metric hides.
- Anchors (Decision 0010) become a *readiness lever*: marking a beat is another
  way to make a risky seam safe, not only recording more.
- The performer-facing number and the runtime's own behaviour stop disagreeing.

## Implementation sketch

1. Classify each cell's role from solo note density in the score plus the section
   map (`solo`/`tutti` already exists but is not applied to sparse solo bars).
2. For piano-led cells, compute readiness from the existing per-cell
   `seconds_per_quarter_mad` / support via `dispersion_trust_gain` — the same
   function the scheduler uses, not a parallel metric.
3. Score transitions at section/entry boundaries separately.
4. Roll a measure up as its **weakest piano-led cell**, ignoring orchestra-led
   cells entirely.
5. Report state + reason + suggested action per measure, so the UI never has to
   guess what a colour means.

## Deliberate non-decisions

- **Not** a calibrated probability. "Readiness" is an ordinal confidence, not a
  validated forecast; claiming a percentage chance of success would overstate it.
- **Not** removing take counts — they stay visible as provenance ("3 passes"),
  but they are no longer the *state*, and must not contradict it (see #149).
- **Not** auto-recording or auto-fixing anything. The performer decides.

## Links

- [Decision 0009](0009-rehearsal-as-dataset-lifecycle.md) (rehearsal as dataset)
- [Decision 0010](0010-human-beat-anchors.md) (anchors as sync points)
- [Decision 0008](0008-predictive-follow-clock.md) (`dispersion_trust_gain`)
- Issue #149 (coverage demands impossible evidence)
