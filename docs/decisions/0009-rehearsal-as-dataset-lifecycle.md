# Decision 0009: Rehearsal as dataset curation; the fitted model is an Interpretation

- Status: accepted
- Date: 2026-07-26

> **Framing update (2026-07-29).** Rehearsal is not primarily data collection; it
> is convergence on the live performance, and takes are the record of that
> process rather than its purpose. See
> [Decision 0013](0013-rehearsal-converges-on-performance.md). The lifecycle
> below is unchanged, but a take's *provenance* — whether the orchestra was
> following at the time — now matters for how it is weighted.

## Context

Rubato's live accompaniment is data-driven: the ACCompanion result we build on
(Decision 0008) shows the winning tempo model is LTE -- a reactive
error-correction term plus a tempo *expectation fitted from the soloist's own
rehearsal performances*. That makes rehearsal takes literally the training data
for the live predictor. The existing take system already modelled most of the
pipeline (capture -> align -> curate -> fold into a per-cell profile), but it
lacked (a) an honest measure of *fit quality* to guide curation, and (b) a clean
conceptual frame naming what rehearsal, fitting, and the model actually are.

A first-principles review (music-performance + machine-learning best practice)
produced the frame below.

## Decision

Treat the workflow as a standard train / deploy / iterate loop, dressed in
musician's language.

### Lifecycle (three phases, one loop)

1. **Practice** -- record takes, curate the dataset (keep/discard; select/bench).
   Training is **not a phase**: the fold is milliseconds and refits
   automatically whenever the curated set changes, so the performer just
   "curates and watches the compass move."
2. **Run-through** -- a live run with diagnostics on. A dress rehearsal *is*
   validation on a genuinely new take; its trace is a free live-conditions test
   set. Maps to the existing `RunMode.REHEARSAL`.
3. **Concert** -- deployment with a pinned model version. Maps to
   `RunMode.PERFORMANCE` + the existing frozen-profile mechanism.

The backtrack loop (Practice <- Run-through) is human-in-the-loop active
learning: the metrics point at weak passages; the performer rehearses those.

### Vocabulary (music term owns the surface; ML term only where music has none)

| concept | name Rubato uses |
| --- | --- |
| the fitted per-cell tempo/dynamics model | **Interpretation** (persisted as `profile.json`; `ProfileDoc` is a compat alias of the `Interpretation` type) |
| the curated training set | **the selects** (kept + `ProfileMembership.INCLUDED` takes); verbs *select / bench* |
| fitting | **fit** (`fold_cells` / `fit_interpretation_for`); UI verb *learn* |
| held-out data | **held-out take** (leave-one-take-out) |
| predictive quality vs. a reactive baseline | **skill** |
| take-to-take spread | **consistency** / **consistency floor** |
| per-passage judgement | **Readiness** (thin / unsteady / learning / ready) |
| runtime read of the model | **TempoPrior** (`period_at`, `dispersion_at`) |

Cells stay internal -- never a UI concept; passages are the performer-facing
unit.

### Metrics as a curation compass

Per passage, four signals: **takes** (support), **consistency** (tempo MAD %),
**skill** (held-out onset error vs reactive), and a one-word **Readiness**
verdict. "Ready" means held-out error is near the consistency floor -- *you are
the limit now, more takes will not help*. That is the anti-treadmill guarantee
that tells the performer when to stop collecting. Only held-out numbers are ever
shown; the evaluator fits through the same fold the runtime reads, so the compass
never points at a look-alike model.

### The overfit ("follows too tightly") risk is structural, not editorial

Each cell's **dispersion** is intended to become a live trust gain (Decision
0008 P1): where the pianist is consistent the orchestra leans confidently on the
Interpretation; where inconsistent it degrades toward reactive. "Good fit" is
therefore definitionally about generalization to a new take.

## What this changed in code (this pass)

- Extracted one fitting implementation, `profile.fold_cells(aligned_results)`;
  `fit_interpretation_for(piece, movement)` (was `fold_takes`) is the thin
  store wrapper; the offline evaluator now fits through `fold_cells` so it
  scores the real model (previously it re-implemented a different, unweighted
  fold -- a phantom).
- `takes/tempo_expectation.py`: `TempoPrior` read-view + `evaluate_interpretation`
  producing `Readiness` (held-out onset error, skill, consistency floor, verdict).
- `ProfileDoc` -> `Interpretation`, `fold_takes` -> `fit_interpretation_for`,
  `_included_takes` -> `selects` -- a complete rename, no compatibility aliases
  (the on-disk artifact stays `profile.json`, a stable file name). Removed the
  unused `ProfileMembership.QUARANTINED`. `analyze_passage` now folds through the
  single `fold_cells` implementation instead of its own median/MAD, so the
  rehearsal-UI summary and the runtime model can never disagree.

### P1 (live wiring): first cut removed; the conclusion was invalid

A first cut wired the Interpretation into the live LTE clock and offline replay
of the measure-44 climax appeared to show it losing to reference-LTE
(reference ~2 ms, Interpretation-pace ~46 ms). **That conclusion was wrong**, and
an adversarial review (PR #124) found why: the wiring set `reference_beat=None`,
which makes the scheduler *strip the reference warp table entirely* and fall back
to flat score-beat extrapolation. The prior was never given a fair test -- it was
compared with the expressive warp deleted. The broken code has been removed
(committed speculative generality, YAGNI), and the "Interpretation is worse"
result is retracted.

### Accepted next architectural steps (deferred, not band-aided)

The same review named the real design work, and it is accepted here rather than
patched around:

1. **Curve-aware scheduler (explicit and measured in #127).** The diagnosis
   above was only true for the canonical fallback. Movement II's mapped events
   already carried an Oguri reference-performance position, and the scheduler
   multiplied the *reference-coordinate* delta by the live scale. Since that
   coordinate is proportional to performed time, this is the exact integral of
   the piecewise canonical-to-reference warp; it was curve-aware behavior hidden
   inside `_schedule_event`, not a canonical scalar extrapolation. #127 made the
   `ArrivalTimeCurve` seam explicit and injectable, retained the flat canonical
   path as a named A/B baseline, and measured both through the closed-loop
   harness. On the fixed synthetic ritardando the warp path is effectively exact
   while the flat path misses by more than 250 ms; the error gap grows with
   curvature and the paths agree at steady tempo.
2. **A deterministic, closed-loop evaluation harness (landed in #128).** The
   previous offline replay was nondeterministic (climax numbers ranged
   +66..+342 ms and did not reproduce live ~+132 ms), while
   `evaluate_interpretation` remains a 1-step curation proxy. The versioned
   `closed-loop-onset-v1` harness now drives
   Follower->TempoModel->Scheduler->deadline output on a virtual clock, records
   an input digest/config/seed, and measures delivered onset error against the
   aligned solo timing map. Scheduler/model ship-or-kill decisions must use that
   harness. This unblocks a fair test of the curve-aware scheduler in (1).

The reference-based LTE remains the shipped clock, and its phase-lead constants
are acknowledged un-tuned defaults, not searched values. The curve result does
not justify deleting that lead: it predicts through follower/detection delay,
which is orthogonal to scheduler geometry. A zero-lead closed-loop arm is exact
when the reference warp matches the synthetic take. Reduce the live default only
when matched Yamaha runtime traces show equal or better landmark error with a
smaller bound.

### P1 live wiring (landed in #129)

The Interpretation is now wired without repeating the removed flat-path bug.
`InterpretationArrivalCurve` wraps the production reference-warp curve and
integrates the fitted canonical period cell by cell. Per-cell tempo MAD becomes
a continuous trust gain: supported low-dispersion cells lean toward the fitted
curve; high, missing, invalid, or one-take dispersion becomes exactly the
reference/reactive baseline. The gain is dimensionless, so expected period and
MAD scale together when the requested global tempo differs from the fitted
base.

The deterministic closed-loop regression uses one input digest for all arms.
The supported low-dispersion fitted curve removes the synthetic broadening
bias; high-dispersion and missing-dispersion arms reproduce the reference
delivery times to `1e-12` seconds. Scheduler JSONL rows and schema-3 evaluation
reports record the stable arrival-curve ID. `LEAD` is explicitly excluded from
the fitted curve.

## Deliberate non-decisions / DEFER (YAGNI)

- **Per-passage per-take exclusion.** Curation stays take-level (global
  INCLUDED/EXCLUDED). The fold is already passage-granular (takes are partial;
  cells only aggregate takes that cover them; the weighted median is robust),
  and the musician's natural remedy -- record a short take of the weak passage
  -- is per-passage curation that already works and produces better data.
  Re-entry trigger: a passage stuck on the Readiness board because one take's
  cells dominate it and re-recording has not displaced them.
- **A manual/override seam for the follower.** Some passages will be too
  artistic for any fit; we do not redesign for that now, but we keep the
  follower/tempo model's knobs clean and explicit (see
  [Rehearsal Model Workflow](../concepts/rehearsal-model-workflow.md)) so a
  human -- or an LLM given the trace -- can override tuning for specific
  measures later without a rewrite.
- Learning-curve history, velocity/pedal in the live model, Kalman/JADAM,
  multiple named Interpretations, auto-curation, any cell-level UI.

## Links

- [Decision 0008](0008-predictive-follow-clock.md)
- [Rehearsal Model Workflow](../concepts/rehearsal-model-workflow.md)
- [Rehearsal Take Coverage Design](../design/REHEARSAL_TAKE_COVERAGE_DESIGN.md)
- [Accompaniment Control](../concepts/accompaniment-control.md)
