# Rehearsal Model Workflow

How Rubato turns rehearsal takes into the model the live accompanist follows,
and how to tell whether more rehearsal is helping. This is the working reference
for the lifecycle in [Decision 0009](../decisions/0009-rehearsal-as-dataset-lifecycle.md);
the live tempo clock it feeds is [Decision 0008](../decisions/0008-predictive-follow-clock.md).

## The loop

```
  PRACTICE ──────────► RUN-THROUGH ──────► CONCERT
  record · curate      live + diagnostics   frozen model
  (Interpretation      (a dress rehearsal    (pinned revision)
   refits on every      is validation on
   curation change)     a new take)
     ▲                        │
     └──── rehearse the weak passages the Readiness board flags ◄──┘
```

Training is not a step the performer takes: fitting the Interpretation is
milliseconds, so it refits in the background whenever the selects change. The
only explicit acts are curating, reading the compass, and freezing for a concert.

## Concepts

- **Take** -- one recorded, aligned rehearsal pass. Each supplies a per-cell
  local tempo (`seconds_per_quarter`) on the canonical half-quarter grid.
- **Selects** -- the curated training set: takes that are *kept*
  (`UserDisposition.KEPT`) and *included* (`ProfileMembership.INCLUDED`).
  Two independent controls: discard a take entirely, or bench it from the fit.
- **Interpretation** -- the fitted model: per-cell expected tempo + dispersion +
  support, learned from the selects by a quality- and recency-weighted median
  (`profile.fold_cells`). Persisted as `profile.json`; the concept is the
  pianist's repeatable rubato shape.
- **TempoPrior** -- the read-only view the live clock uses: `period_at(tick)`
  (expected tempo) and `dispersion_at(tick)` (how consistently it's played).
- **Readiness** -- the per-passage report card (below).

## Fitting and evaluation (the APIs)

- `profile.fold_cells(aligned_results) -> (cells, base)` -- the single fitting
  implementation. Pure; no store I/O. Both the persisted Interpretation and the
  evaluator go through it, so the metrics grade exactly the runtime's model.
- `profile.fit_interpretation_for(piece, movement) -> Interpretation` -- reads
  the selects and folds them, with provenance.
- `tempo_expectation.evaluate_interpretation(takes) -> Readiness` -- grades the
  fit **leave-one-take-out**: fit on the others, predict the held-out take,
  measure error. Only held-out numbers are produced.

## The compass: reading Readiness

Four signals per passage:

- **Takes** -- how many selects cover it (support; target ~3).
- **Consistency** -- take-to-take tempo MAD (%). Low = you play it the same way
  twice, so it is learnable. High = decide how you want to shape it first.
- **Skill** -- held-out onset error vs a reactive baseline
  (`1 - LTE/reactive`). The one number that answers "is rehearsing making live
  tracking better?"
- **Verdict** -- one word:
  - **thin** -- fewer than ~3 takes; record more.
  - **unsteady** -- played too inconsistently (or skill <= 0); more takes won't
    help until you play it steadier or decide the interpretation.
  - **learning** -- the model trails your consistency floor; more takes should
    help.
  - **ready** -- held-out error is near your consistency floor: you are the
    limit now, not the model. Stop collecting.

Measured example (3 takes over the measure-44 passage, Movement 2): onset error
~38 ms vs reactive ~76 ms (skill +0.50), consistency ~3%, verdict *learning*.

## Follower knobs and the override seam

The live tempo clock ([predictive_follow.py](../../src/aimusic/accompaniment/predictive_follow.py))
exposes explicit, bounded tuning knobs rather than magic constants, so tracking
can be tuned -- by a human, or by an LLM handed a run trace -- without a rewrite:

- `LteTempoModel(lead_fraction, lead_gain, max_lead_seconds, min_progress_ref)`
  -- how much and how fast the orchestra leads, bounded so a misprediction
  cannot run away.
- `RuntimeConfig.follow_clock` (`reactive | lte`) and the `RUBATO_FOLLOW_CLOCK`
  launch override -- swap clocks for A/B.
- Planned (Decision 0008 P1): a dispersion-scaled lead gain from the TempoPrior,
  and a per-passage override so a measure that is too artistic for the fit can
  carry a hand- (or LLM-) tuned strategy. The design does not build that now, but
  the knobs above are the seam it will attach to; keep them clean and explicit.

## Related

- [Decision 0009](../decisions/0009-rehearsal-as-dataset-lifecycle.md)
- [Decision 0008](../decisions/0008-predictive-follow-clock.md)
- [Accompaniment Control](accompaniment-control.md)
- [Rehearsal Take Coverage Design](../design/REHEARSAL_TAKE_COVERAGE_DESIGN.md)
