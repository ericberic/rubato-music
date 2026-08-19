# Decision 0015 — The follower always exists; the score says what to expect

Status: **Accepted** (2026-07-29)

## The question that should never have been askable

Across many sessions we kept asking, of a given passage: *is there a follower
here?* We asked it about cue-record takes, about cold starts, about orchestral
interludes. Every time, the answer was some variant of "no, not on that path" or
"no, not enough data" — and every time the performer's reasonable expectation was
violated.

Eric, closing the loop:

> "A follower should always be able to be in existence. Its functionality just
> needs to be able to handle this state and distinguish between insufficient data
> compared to expected amount of data, vs intentional silence as written by the
> music itself."

That is the redesign. The follower is not a feature that some regions have. It is
a component that always runs, and whose behavior is a function of two things the
system can always answer.

## First principles

### 1. The follower is a predict–correct estimator, not a mode

The follower always has a score position. Performer input is a **correction
signal, not a prerequisite**. Between corrections it predicts forward on its best
available pace.

This single reframing dissolves the old question. "Is there a follower here?"
becomes meaningless, because prediction never stops. What varies is only whether
corrections are arriving and whether their absence is meaningful.

### 2. The score declares what to expect, per cell, a priori

Two artifacts already exist and are correct:

- `derived/solo_reference.mid` — the solo part. Gives **expected onset count**
  for any cell. Verified: its first onset is canonical beat 47.0, which is exactly
  the piano's first note of Movement II.
- `derived/sections.json` — coarse authority regions. Verified correct:
  `opening-orchestra-lead` spans beats 0–47.0, ending precisely at that first
  solo note; `m22-orchestra-interlude` marks m.22–23 LEAD.

Expectation is a property of the music. It does not depend on how much we have
rehearsed, and it never changes.

### 3. Evidence is judged relative to expectation, never absolutely

This is the crux, and the source of nearly every confusion in this project.

The old metric was an absolute count: `support_at(tick) >= 1`. Under it, a bar
where the piano is silent scores 0 forever, and is indistinguishable from a bar
of dense solo writing that has never been rehearsed.

The correct quantity is **observed relative to expected**. Where the score
expects zero onsets, observing zero is a *complete match* — full confidence, no
gap, nothing to record. Where the score expects twelve and we have none, that is
a real gap.

### 4. Three states, never collapsed

| State | Score expects | Evidence | Correct response |
| --- | --- | --- | --- |
| **TACET** | nothing | not applicable — already satisfied | play the calibrated pace; expect silence |
| **UNREHEARSED** | onsets | none recorded | record a pass here |
| **DISAGREEING** | onsets | recorded but inconsistent | play it again more consistently, or set an anchor |

`support == 0` currently means both TACET and UNREHEARSED. They demand opposite
things from the performer — *do nothing* versus *record a pass* — which is why the
surface has been telling Eric to rehearse silence.

### 5. Silence is an anomaly only where the score predicted sound

In TACET, silence *is* the correct performance and must produce no alarm, no
confidence decay, and no dropout handling. In solo material, the same silence is
a dropout. Identical input, opposite meaning, resolved entirely by the score.

### 6. Authority follows expectation; confidence follows agreement

Two independent decisions that were previously entangled in one flag:

- **Who holds timing authority** is a question about the *music*: does the piano
  play here? From the score.
- **How much to trust the learned curve** is a question about the *evidence*:
  do the takes agree? From `dispersion_trust_gain`.

A passage can be orchestra-led with excellent evidence, or piano-led with none.
All four combinations are legitimate and must be representable.

### 7. One path: a rehearsal pass is a performance, recorded

Per [Decision 0013](0013-rehearsal-converges-on-performance.md). Since the
follower always exists, there is no longer any reason for a separate deaf
cue-record path. That path exists only because "no follower here" was once
thinkable.

## The design

### Expectation model (new)

Derived once per bundle, cached, reviewable:

```
ExpectationCell:
  score_tick_start, score_tick_end
  expected_onsets: int         # from solo_reference.mid
  role: TACET | SPARSE | ACTIVE
  authority: LEAD | FOLLOW     # coarse, from sections.json
```

`TACET` when `expected_onsets == 0`. `SPARSE` when onsets are few enough that
localization is weak (held chords, long notes) — expectation exists but the
follower must not demand frequent corrections. `ACTIVE` otherwise.

`sections.json` remains the human-authored authority map and the arbiter on
disagreement; the derived density is what makes per-cell reasoning possible.

### Follower behavior, exhaustively

| Role | Corrections arriving | Behavior | Position honesty |
| --- | --- | --- | --- |
| TACET | none (expected) | predict on calibrated pace; **LEAD**; confidence unchanged | `predicted` |
| TACET | unexpected input | performer entered early — treat as an entry, hand authority over | `corrected` |
| SPARSE / ACTIVE | arriving, takes agree | **FOLLOW**; prior-anchored pace, high trust gain | `corrected` |
| SPARSE / ACTIVE | arriving, takes disagree | **FOLLOW**; reactive, trust gain → 0 | `corrected` |
| SPARSE / ACTIVE | arriving, unrehearsed | **FOLLOW**; reactive on nominal pace | `corrected` |
| SPARSE / ACTIVE | none, brief | **COAST** on last known pace | `predicted` |
| SPARSE / ACTIVE | none, sustained | **HOLD_DROPOUT**; this is a real anomaly | `predicted, stale` |

The existing `TransportAuthority` states (LEAD / FOLLOW / COAST / HOLD_DROPOUT /
ENTRY / STOP) already cover this. Nothing new is needed there. What is new is that
the **role** decides which of them applies, rather than a per-take flag.

### Transitions are the hard part, and now they are named

Every live failure in this project has been at a seam:

- **TACET → ACTIVE** is an **entry**. The follower has been predicting; the first
  real correction arrives and may be far from the prediction. This is where
  `EntryPaceAcquisition` and the entry clamp belong, and it is exactly where the
  cold-start work already landed.
- **ACTIVE → TACET** is a **handback**. The follower stops expecting corrections
  and must not interpret the ensuing silence as a dropout.

Both are derivable from the expectation model, so they can be anticipated rather
than discovered.

### Confidence

Confidence decays only against *expected* corrections that failed to arrive:

```
missing_ratio(expected, observed):
    if expected == 0:  return 0.0          # nothing expected -> nothing missing
    return max(0.0, 1 - observed / expected)
```

The zero guard is load-bearing, not defensive. The obvious-looking
`1 - observed / max(expected, 1)` returns **1.0** for the
nothing-expected-nothing-observed case -- reporting a total miss for a passage
performed exactly as written. Written correctly, TACET is a first-class satisfied
state rather than an absence: no decay, ever, and no amount of recording can
improve it.

## What this replaces

- `_accompaniment_mode_for_passage` — deleted. Both its original form (support at
  the cue-start tick) and my 16-measure-lookahead replacement stamp one global
  mode on an entire take from one position's evidence. Authority is per-cell and
  evolves as the performer moves. My lookahead version produced the right answer
  for m.9 by borrowing confidence from downstream bars, which is the same
  category error in the opposite direction.
- `handoff_at_entry` as a take-level boolean — becomes a consequence of the role
  at the current position.
- Coverage's absolute `min_n` per cell — becomes expectation-relative, so a
  movement can reach 100% without rehearsing silence
  ([Decision 0012](0012-performance-readiness.md)).

## Implementation order

1. **Expectation model + unit-pinning tests.** `solo_reference.mid` is in
   *reference* time at PPQ 240, while canonical score ticks are PPQ 960; mixing
   them silently produces plausible-looking nonsense (it made me report that the
   piano plays at m.3–12, when it is tacet until m.12 beat 4). Pin known bars:
   m.1 tacet, m.12 beat 4 first onset, m.22 tacet.
2. **Readiness consumes roles.** TACET cells ready by default; yellow splits into
   UNREHEARSED and DISAGREEING with different suggested actions.
3. **Follower consumes roles.** Role selects authority; confidence goes
   expectation-relative; transitions become anticipated events.
4. **Retire the deaf cue path.** Route recording through the live follower, so a
   rehearsal pass is a performance that is recorded.
5. **UI states the plan** before recording: which bars the orchestra leads, where
   it starts following, and at what learned tempo.

All five steps are implemented as of 2026-07-29. Steps 4–5 removed the fixed
cue recorder and route `Record pass` with **Orchestra cue** enabled through
`LiveRuntimeManager.start_follow()`. The selected measure is the
runtime entry, the profile's fitted `base_seconds_per_quarter` supplies the
opening pace, and the runtime always records the same Yamaha events it uses for
correction. Before the MIDI ports open, `GET /api/runtime/plan?start_measure=N`
states the orchestra start, the first `FOLLOW` position, local prior-take
support, and the fitted tempo. The button remains disabled until this plan is
visible.

The recording lifecycle was tightened after the first Yamaha validation on
2026-07-30. **Rehearse** and **Perform live** now both stop at the same
ephemeral performance boundary. Stopping never silently approves training
evidence. A sticky decision card offers **Hear piano on Yamaha**, **Preview on
Mac**, **Keep for piano + orchestra review**, or **Nothing for now**; only Keep creates the durable take, starts
alignment/review, and rebuilds the Interpretation. Cue and selected-position
metadata remain attached to the scratch recording and move with that explicit
promotion. Scratch identity is stored beside the MIDI so restart and panic
paths cannot silently promote the same run again.

This is also the aggregation model for long and short overlapping passes. Each
performance is aligned once to the canonical timeline and contributes at most
one vote to each half-quarter cell it actually covers. Recency rank and quality
weight are local to that cell. A long pass therefore fills many cells but does
not contribute extra votes to any one cell, while shorter passes remain
independent corroborating observations where they overlap.

The same hardware validation exposed a second timing-ownership consequence:
note-on commitment cannot make a sustained note's release immutable. Tempo and
follower updates can still move the notated end and following attack. The
scheduler now retimes releases that are still sounding, and an ordinary
within-chord follower advance dispatches a crossed chord immediately instead
of expiring it. Declared skips/relocks retain the no-catch-up-burst rule.

Dropout is expectation-gated at every coast/HOLD seam. Wall time alone cannot
distinguish a memory lapse from a written rest or sustain. The predicted score
cell must be `ACTIVE` before silence can enter dropout HOLD; otherwise the
engine keeps the score clock and orchestra moving until the next piano entrance.

Refined after the 2026-08-11 live rehearsals (see [the log](../LOG.md)), because
even an ACTIVE cell was declaring dropouts that were not real. Three changes make
"unsure != stopped" literal:

- **The silence budget is tempo-relative, not a fixed 1.5 s.** Into the m.45
  climax the pianist decelerated to ~27 BPM, where one beat lasts >2 s, so a
  single notated-ritardando beat tripped a false dropout and panicked the
  orchestra on the very downbeat. The budget is now `follower_coast_beats` at the
  current beat period, clamped by `follower_coast_ms`/`follower_coast_max_ms`.
- **A dropout requires genuine input silence, not merely a stale confident
  position.** Dense and chromatic writing keeps the matcher below its lock
  threshold while onsets keep arriving; those onsets prove the pianist is
  playing, so they can never expire into a hold. This is the FOLLOW "arriving but
  disagreeing" row above, honoured at last.
- **A dropout hold is gentle.** It stops scheduling but passes `hold_panic=False`
  to the scheduler, so a ringing orchestra chord releases on its own note-off
  instead of being slammed off by an all-notes-off. Only a true STOP panics. Where
  the pianist is unexpectedly silent, the coast first decelerates by
  `follower_coast_slowdown` so the orchestra eases into the wait.

The persisted cue is now only an alignment hint: kind, target and cue-start
score anchors, derived duration, and output name. It does not own a clock or
authority policy. Legacy `tempo_scale` and `handoff` fields are accepted by the
one-time migration reader but are not preserved in v2 take artifacts.

## Consequences

- "Is there a follower here?" stops being a question. There is always a follower;
  ask what the score expects.
- The orchestra plays interludes (m.22) because it is *led* there, not because a
  support count happened to clear a threshold.
- A cold start is no longer a take-level mode. It is a per-cell evidence state at
  the cells where the performer actually plays.
- A performance is not a take until the performer keeps it. Capture is
  proactive; profile membership is explicit.
- Sustained-note endings remain on the mutable score clock until they sound,
  so tempo replanning cannot pepper legato orchestral writing with gaps.
- Tacet regions can never be "improved" by recording, and the surface stops
  asking.

## Links

- [Decision 0014](0014-cockpit-is-the-score.md) (visible beat clock, cursor authority)
- [Decision 0013](0013-rehearsal-converges-on-performance.md) (rehearsal converges on performance)
- [Decision 0012](0012-performance-readiness.md) (readiness, not coverage)
- [Decision 0008](0008-predictive-follow-clock.md) (`dispersion_trust_gain`, rehearsal-anchored pace)
- [Section policy](../concepts/section-policy.md)
- Issue #146 (route rehearsal through the live path), #149 (readiness model)
