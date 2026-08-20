# Decision 0010: Human beat anchors fire reactively; LTE is the default clock

- Status: accepted
- Date: 2026-07-26; amended 2026-08-10

## Context

Decision 0008 established the predictive **LTE** FOLLOW clock (reference-fitted
tempo expectation + damped reactive correction). Live testing of the
mvt-2 climax (measure 44) confirmed LTE cut the orchestral onset error at the
downbeat from ~+132 ms to ~+47 ms, and analysis showed the residual is
*variance* (jitter around a near-zero mean), not a constant lag -- so no fixed
offset can remove it; only a better predictor or more information can.

Two isolated results reframed the problem:

1. An **echo test** (play a note, emit it an octave up) measured the software +
   MIDI turnaround at ~0.07 ms; the perceived floor is effectively zero. The
   ~50 ms seen in FOLLOW is entirely the follower -> tempo-model -> scheduler
   *prediction* pipeline, not I/O.
2. At a handful of structurally important beats (a climax bass note, a phrase
   downbeat) the performer knows *exactly* which note marks the beat and plays
   it crisply. There, prediction is the wrong tool: the pianist's own onset is a
   ground-truth beat marker available in real time.

So: let the human annotate those beats, and at those beats fire the orchestra
**reactively** off the matched piano onset instead of predicting it.

## Decision

Add **beat anchors** -- a small, human-curated set of `(score_tick, measure)`
marks -- and default the FOLLOW clock to **LTE** everywhere else.

### Reactive firing (guarded, roll-preserving)

At an anchored beat the accompaniment chord for that beat fires the instant a
pitch-matched, follower-position-gated piano onset arrives:

- **Bass-pitch-matched**: the incoming note must match the lowest solo pitch
  within 0.25 canonical beats of the human click. Other chord tones, wrong
  notes, and most ornaments do not trigger.
- **Position-gated**: because the fast path runs before the triggering note is
  sent through the follower, the previous follower position may be at most
  1.25 canonical beats behind the anchor and 0.25 beats past it. When multiple
  nearby anchors share a bass pitch, canonical distance selects the candidate,
  not insertion order.
- **Fires once per beat through the scheduler-owned dispatch seam**. A mutable
  plan is retired directly. If the event has transferred to the deadline
  output, the entire chord is replaced atomically while it is still pending;
  once any note has begun delivery, the anchor declines instead of
  double-playing or partially replacing the chord.
- **Roll-preserving**: a rolled/spread orchestral chord keeps its recorded
  intra-chord offsets; only the *first* (bass) note is pinned to the piano onset,
  later notes retain their source-performance offsets multiplied by the
  reference-performance period. Bundles without that coordinate explicitly
  fall back once to canonical offsets and the canonical period. If the period
  is invalid/non-finite, the roll collapses rather than smearing with an
  invented tempo. This matched the human judgement that the natural roll
  sounded best; the win was the reactive *anchoring* of the roll's start, not
  collapsing it during normal tracking.
- **Bounded degradation**: anchors are optional. With none, or when a bundle has
  no qualifying bass/chord near an anchor beat (`AnchorFirer.is_empty`), the
  runtime is exactly the LTE clock. A misplayed matching bass before the marked
  onset can still pre-fire an anchor; the bass-only and position gates reduce
  that risk but cannot prove performer intent. Mark only crisp, structurally
  unique bass onsets and use the runtime JSONL MIDI trace to adjudicate misses.

This is the *fast path* the residual-variance analysis pointed to: at anchored
beats the orchestra's onset error collapses toward the echo-test floor because it
is reacting, not predicting.

### Data model and persistence

- `Anchor(score_tick, measure, label)` and `AnchorSet(schema_version, piece_id,
  movement, updated, anchors)` are ArtifactModels in `takes/models.py`.
- Persisted per movement at
  `data_root()/profiles/<piece>/<movement>/anchors.json`, separate from the
  Interpretation (`profile.json`): anchors are a *human structural annotation*,
  not a fitted quantity, and should survive re-fitting.
- Store API (`takes/store.py`): `load_anchor_set` / `save_anchor_set` /
  `add_anchor` (idempotent on tick, sorted) / `remove_anchor` /
  `move_anchor` / `remove_anchors_in_measure` / `restore_anchors`. Every
  read-modify-write operation serializes per piece/movement. Move, measure
  clear, and multi-anchor restore are single durable transactions, so correction
  and undo cannot expose a transient duplicate or a half-restored cluster.
  Moving onto another anchor is rejected instead of silently deleting the
  occupied mark, preserving the reversibility contract.

### API and UI

- REST (`/api/scores/{movement}/anchors`): `GET` list, `POST` add,
  `PATCH /{score_tick}` atomic move, `DELETE /{score_tick}` remove, `DELETE
  ?measure=N` atomic measure clear, and `POST /restore` atomic undo restore, all
  returning the full `AnchorSet`. `piece_id` is validated against the
  session-id pattern and `movement >= 1` (422 otherwise).
- The performer edits anchors **at the engraved note**, without entering a
  distant mode or leaving the current score viewport. Right-clicking an exact
  onset opens a score-local menu beside the pointer. An existing anchor is a
  compact blue locator tab with an `A`, visually separate from amber rehearsal
  coverage and the red now-line. Its 36 px direct-manipulation target can be
  dragged horizontally to move it, or clicked / right-clicked for direct
  deletion. A measure with an accidental cluster
  offers one clear-all action. Add, move, delete, and clear immediately expose
  an adjacent Undo; clear/restore are atomic rather than a client-side loop.
  Cursor projection, click inversion, and dragging share the same explicit
  measure-start, detected-beat, and measure-end knots.

### LTE is the default clock

`runtime_contracts.follow_clock` now defaults to `"lte"` (was `"reactive"`);
`"reactive"` remains selectable for A/B and diagnostics. Anchors layer on top of
whichever clock is active.

### Starting live or replay from a printed measure

The performer may right-click an exact printed measure and choose **Start live
with orchestra here** beside the notation (the top-level **Perform live**
control remains an alternate route). The selected measure crosses the runtime
boundary once:

1. printed measure number -> canonical score beat at 960 PPQ;
2. canonical score beat -> reference-performance beat through the existing
   score/reference warp;
3. both coordinates are carried explicitly in the `runtime_start` trace and
   seeded at one shared monotonic entry time.

The live runtime starts the orchestra immediately at the selected canonical
barline with no metronome. This is an explicit `ORCHESTRA_ENTRY` transport
authority, not confidence-zero follower dropout coasting: the orchestra owns a
monotonic clock while the performer listens for the pulse, and the usual
`follower_coast_ms` dropout timer therefore cannot silence it after two beats.
Matchmaker is constructed at the mapped reference beat but consumes no piano
evidence during that listening phase. When the first piano note arrives, its
PTHMM prior is recentered once at the orchestra's then-current
canonical/reference position. A confident match whose position falls in the
narrow entry window around that moving orchestra position certifies *where* the
pianist entered — but not *how fast*. Position acquisition is not tempo
acquisition: two matches span ~0 beats and carry no pace, so transferring full
timing authority on them (as an earlier revision did, seeding FOLLOW from the
orchestra's continuous pace) hands the clock the warp-inflated lead-in tempo and
then smooths the pianist's real pace into that artifact.

Before the pianist joins, the Oguri orchestra is itself the performance. It
advances on one uniform **source-performance clock**, preserving Oguri's
original inter-event timing and note lengths while one global scale makes the
authored section average match the performer-selected quarter-note tempo.
Canonical beat remains the projected identity used by the score cursor,
section policy, and entry matcher; it does not independently retime each
orchestral beat. The rehearsal pace profile is a FOLLOW prior and cannot reshape
this autonomous playback. This distinction was confirmed by run
`live-1786391346736`: mm.3 and 8 had no piano input, authority change, or device
lateness, yet sounded lurchy because `ORCHESTRA_ENTRY` dropped the reference
coordinate and scheduled Oguri events on their projected canonical positions.

Movement II's checked-in simultaneity contract uses exact canonical beat ticks.
For m.44 through the m.45 resolution they are `165120`, `166080`, `167040`,
`168000`, and `168960`, arming bass pitches 40, 39, 32, 31, and 30. The previous
nearby source-derived values armed treble pitch 71 on the first cue and missed
the m.45 downbeat entirely. The analogous m.93→94 cues were already exact-grid
ticks and therefore worked in the same live run.

The entry therefore has an explicit tempo/phase-acquisition phase between
position match and `FOLLOW`. Once position is certified, the orchestra **freezes
at the entry beat** and gathers piano onsets until they span a musical interval.
Two live traces show the fit only settles on the true pace once **elapsed time**
reaches ~1.3 s (~10 onsets): right after recentering the follower does a fast
catch-up burst and its raw canonical beat then plateaus, so a shorter window
reads 65–90 BPM for a 51 BPM entrance. Defaults are therefore ≥6 onsets,
≥0.5 canonical beats, and ≥1.3 s elapsed (time is the reliable gate; span in
beats plateaus). A bounded timeout settles for less if a sparse but active
entrance keeps playing. A robust Theil–Sen line
is then fit to those onsets *alone* and `FOLLOW` is **hard-clamped** onto that
pace and phase via `seed_timing`, discarding the pre-entry orchestra seed rather
than blending across the evidence discontinuity. Phase comes from the fit
evaluated at handoff and is floored at the frozen beat, so transport advances
once to the soloist's current position and never rewinds; listening time is
never interpreted as pianist tempo, and a stale cue-point estimate cannot replay
accompaniment. This is the same "hard sync at a known beat" principle as a human
beat anchor, applied to the entry as the first sync point.
Authored `LEAD`, `HOLD`, and `STOP` boundaries remain authoritative, as do the
performer's **Silence** and stop controls. `STOP` rejects an initial request
before opening MIDI ports.

This supersedes the four-click `CUE_ENTRY` contract after the 2026-07-26 Yamaha
trace `live-1785113806755` showed its hidden assumption: the soloist selected a measure
to *hear* an orchestral lead-in, but the generic dropout grace assumed he would
play immediately and entered HOLD around score beat 150 after only two beats.
Merely extending that timer would preserve the mismatch. Re-enter the bounded
entry question only if runtime traces show an explicit orchestra-led start
running beyond a musically safe handoff; any bound must then come from an
authored or score-derived cue endpoint, not another arbitrary wall-clock delay.

The no-sound replay API accepts the same printed measure, truncates only fully
aligned observations at the interpolated entry time, and shifts their relative
timestamps to zero. This is the deterministic parity check for the live seam:
events after the selected measure retain the same relative target timing as the
full replay. The resolved live start coordinates and
`start_kind="orchestra_lead_in"` are recorded in the `runtime_start` JSONL row;
no `count_off` rows are emitted for this live path. Follower traces record
`orchestra_entry_position_mismatch` while a lock disagrees with the moving
orchestra, `orchestra_entry_acquiring` for each onset gathered during the
tempo/phase window, and `orchestra_entry_handoff` when authority transfers; the
clamp's tempo row carries `observation_decision="orchestra_entry_piano_seed"`.

## Deliberate non-decisions / DEFER (YAGNI)

- **Anchor-aware prediction between anchors.** The deterministic closed-loop
  harness isolates this proposal's downstream phase mechanism with an oracle
  follower, a matched reference warp, and a
  deliberately injected 200 ms scheduler miss only at the anchored beat. The
  active anchor removes the full 200 ms miss. The next orchestral beat has
  `0.0 ms` error and the same delivery time as the no-anchor arm to `1e-12`
  seconds. This is synthetic isolation evidence, not a claim about Matchmaker
  jitter or Yamaha delivery; the production trigger is therefore not met.
  This follows the production order:
  after reactive firing, the same matched piano onset immediately passes through
  follower -> LTE and already refreshes the tempo state's phase. Feeding it into
  LTE a second time would double-count one observation. Re-entry trigger: a
  Yamaha runtime trace shows repeatable drift immediately after a successfully
  fired anchor while the local reference/Interpretation curve is independently
  shown to match the passage. Expectation-shape mismatch by itself belongs at
  the dispersion-gated curve seam, not in anchor phase coupling.
- **Beat snapping / sub-beat quantization in the UI.** Clicks store an exact tick;
  the firer's trigger windows (`~0.25` beat) absorb click imprecision, so no
  snap-to-grid is needed yet.
- **Structural anchors / multiple takes as first-class dataset objects.** The
  richer take/anchor data model is scoped to what reactive firing needs today.

## Links

- [Decision 0008](0008-predictive-follow-clock.md) (predictive FOLLOW clock)
- [Decision 0009](0009-rehearsal-as-dataset-lifecycle.md) (rehearsal as dataset)
- [Accompaniment Control](../concepts/accompaniment-control.md)
