# Decision 0008: Predictive FOLLOW clock (LTE leading phase)

- Status: accepted
- Date: 2026-07-25

## Context

Live testing surfaced an orchestra-simultaneity failure in FOLLOW: the
accompaniment lagged the pianist through a broadening climax, felt as a 16th-to-
8th-note late chord. Calibration reported ~0 ms output advance, so the fixed
output path is not the cause.

Two wrong turns were made and are recorded here so they are not repeated:

1. **"The live follower emits no `reference_beat`."** False for the runtime path.
   The raw Matchmaker adapter returns `reference_beat=None`, but the live follow
   path wraps it in `_CanonicalFollower`, which sets `reference_beat` to
   Matchmaker's source position. So the reference position is already present,
   and the scheduler already times orchestra events along the reference rubato
   shape. A first "predictive" clock that only *back-filled a missing*
   `reference_beat` was therefore a no-op in the live path and changed nothing.

2. **"Reactive is enough if it follows the reference shape."** Also not the
   whole story. The `measure-44` trace showed first-note sync is tight
   (+/-30 ms) for most beats, but the arrival chord at the broadening climax was
   ~+130 ms late, growing to ~+360 ms across the chord's own roll. The reactive
   clock is anchored to the *last detected* pianist position, so an arrival beat
   cannot sound until the follower has detected the crossing (~one processing
   delay), and re-anchoring each note means it never commits to a predicted
   arrival. Shape-following does not fix that; it only sets spacing.

The surveyed ACCompanion tempo models place this on a reactive->predictive axis:
`R`/`MA` (reactive), `L` (reactive error-correction, ~= what we run), `K`
(Kalman state-space, can carry tempo derivatives), `LTE` (tempo *expectation*
from a reference performance), `JADAM` (explicit adaptation + anticipation).
The predictive answer is `LTE`/`K`/`JADAM`, not the reactive family.

## Decision

Add `LteTempoModel` (`src/aimusic/accompaniment/predictive_follow.py`), selected
by `RuntimeConfig.follow_clock` (`"reactive"` default, `"lte"` opt-in). It keeps
the reactive model's stable pace estimate (seconds per reference beat, which
does not lag a ritardando because the rubato is in the score<->reference map,
not the pace) and adds a **bounded, non-negative leading phase**:

- On each confident, forward note it measures how much later the note arrived
  than the stable pace predicted (positive during a broadening).
- It adapts a lead offset toward a fraction of that lateness via a slow EMA, so
  the jittery live position averages out instead of being chased.
- The offset is clamped to `[0, max_lead]`: the orchestra can only be moved
  *earlier*, never later than the reactive clock, and never runaway.

The orchestra is then scheduled from `detected_time - lead`, so it anticipates
the arrival instead of waiting to detect it. Lead fraction 0 reproduces reactive.

## Validation

Offline replay of the real `measure-44` follower trace through both clocks
(same scheduler, same input):

- Naive per-note phase correction **oscillated** (+/-200 ms, both bounds) on the
  tracker's jumpy position and made the climax worse. The damped, clamped lead
  removes the oscillation.
- Climax arrival chord: reactive **+342 ms**, LTE **+186 ms** (a ~156 ms
  improvement) relative to the pianist's bass note.
- Mid-measure beats are mixed (+/-50-100 ms; some better, some slightly worse).

So LTE is a genuine but partial win: it meaningfully leads the climax, the exact
spot that fails, while remaining rough mid-passage. Deterministic invariants are
covered in `tests/accompaniment/test_predictive_follow.py` (leads a ritardando,
collapses to reactive when steady, never lags worse than reactive).

## Dispersion-gated Interpretation curve (landed in #129)

The fitted Interpretation now contributes its complete future canonical tempo
curve through `InterpretationArrivalCurve`, after the reference-warp scheduler
seam from #127. It does not replace `reference_beat` or flatten the Oguri warp.
For each future half-quarter interval, the scheduler blends fitted expected
elapsed time with reference-warp elapsed time:

`elapsed = trust × Interpretation + (1 - trust) × reference`

where `trust = clamp(1 - (MAD / expected_period) / 0.15, 0, 1)`. The ratio makes
the gain invariant under the requested global tempo scale. Missing dispersion,
fewer than two supporting takes, invalid data, and relative MAD at or above 15%
all yield trust zero, exactly preserving the prior reference/reactive path.
Zero MAD with real repeated support yields trust one. The lookup converts
canonical score beat to the Interpretation's 960-PPQ tick once; reference beat
never indexes the profile.

This modulation applies only in `FOLLOW`. Autonomous `LEAD` passages retain
their authored reference timing. The LTE phase lead remains a separate bounded
term for follower/detection phase.

## Rehearsal-anchored pace (2026-07-27)

The reactive/LTE pace is derived from the follower's position, which on live
Yamaha input is a jumpy step function — so it manufactured tempo fluctuation
(43-95 BPM while the pianist played a steady ~50). `RehearsalAnchoredTempoModel`
wraps the selected clock and, where the rehearsal Interpretation has take support,
sets the canonical pace to the **bar-smoothed** profile tempo times one
slowly-adapting scale (today's pace vs rehearsal), adapted only from accepted
beat-level candidates. This makes the rehearsal profile — the plan the pianist and
orchestra agreed on — the pace prior, with live evidence contributing only a
robust overall scale and (unchanged) position/phase. Sub-beat micro-rubato is
smoothed away (unresolvable by the follower; fluctuates as a pulse); the
phrase-level arc is kept. Unsupported passages degrade to reactive; missing data
holds the plan. Wired via `LiveEngine.from_bundle(pace_profile=...)` from the same
`profile.json` the arrival curve already loads.

## Deliberate non-decisions / next steps

- **Chord roll.** Separately from tracking, each orchestra chord reproduces the
  reference's rolled voicing, stretched to ~130-230 ms at slow tempo, which
  reads as smear. Tightening the chord attack at slow tempo is a separate,
  low-risk lever not taken here.
- No Kalman/JADAM state model yet; the leading-phase LTE is the minimal step.
- No cockpit toggle; `RUBATO_FOLLOW_CLOCK=lte|reactive` forces the clock at
  launch for hardware A/B, and `follow_clock` is on the run-start request.

## Links

- [Accompaniment Control](../concepts/accompaniment-control.md)
- [ACCompanion Mechanics](../sources/accompanion-mechanics.md)
- [Decision 0007](0007-multi-horizon-runtime-transport.md)
- [Decision 0009](0009-rehearsal-as-dataset-lifecycle.md) (the rehearsal
  Interpretation that P1 wires in as the tempo prior)
- [Rehearsal Model Workflow](../concepts/rehearsal-model-workflow.md)
