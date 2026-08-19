# Rubato: Vision and UX Design

> An accompanist is a musician who listens more than they play.

This document is the product vision and interaction design for Rubato. It is
written to be read before code, and to guide the agents who write the code. It
covers what the system is, how it learns, how it follows, what Eric sees, and
what to build in what order. Architecture and data contracts remain owned by
[System Design](SYSTEM_DESIGN.md); this document owns intent, workflow, and the
interface.

---

## 1. What Rubato Is

Rubato is an orchestra that rehearses with you.

Eric sits at a Yamaha CLP-795GP and plays the solo part of Chopin's Piano
Concerto No. 1 in E minor. Rubato listens to the MIDI stream from the piano,
knows the score, follows his interpretation in real time — his tempo, his
rubato, his dynamics — and plays the orchestral accompaniment back through the
piano's own synth, in time with him, the way an orchestra follows a soloist
under a good conductor.

Three first-principles observations shape everything below.

**First: accompaniment is mostly memory, not reflexes.** A professional
orchestra does not sight-track a concerto soloist note by note. It rehearses.
By the performance, the ensemble already knows where the soloist breathes,
where she broadens, where she pushes. Live listening confirms and corrects a
shared plan; it does not create one. The research record agrees: in
ACCompanion's evaluation, a purely reactive tempo follower produced onset
errors of multiple seconds, a corrective linear model got to ~82 ms, and a
model given *reference performances of the same player* got to ~23 ms — see
[ACCompanion Evaluation](sources/accompanion-evaluation.md). The single
biggest quality lever available to Rubato is that Eric will rehearse with it.
**Rehearsal is where the ensemble is formed. Performance is where it is
trusted.** Everything the system learns must flow from simply playing —
never from labeling, configuring, or annotating.

**Second: musical identity precedes musical time.** Score time (beats and
measures) answers *where*; wall time answers *when*; the tempo curve describes
*how the performance moves between already-identified locations*. A
performance is a mapping from score time to wall time, plus a dynamic shape
laid over it. But that mapping can be learned only after pitch/chord/order
evidence has established which performed events correspond to which score
events. A huge rubato stretches a known beat; it does not create a new measure.
The PDF is a fourth coordinate system—where the notation is painted—not a
clock. This is why [System Design](SYSTEM_DESIGN.md) uses the sequence **score
identity → symbolic correspondence → timing warp → PDF geometry**, and why the
scheduler consumes both `score_beat` (location) and tempo state (rate).

**Third: the interface is furniture, not software.** Eric is at the piano,
hands on keys, laptop an arm's length away on the music desk or a stand. The
UI succeeds when it behaves like a music stand light: glanceable from two
meters, operable in one press, invisible while playing. Every screen, control,
and state indicator below is designed against that test. If a feature needs a
mouse and reading glasses mid-piece, it is wrong.

What Rubato is *not*, for now: not a style-transfer engine, not a generative
model, not an audio pipeline, not a cloud service. It is a local, symbolic,
deterministic instrument that gets to know one pianist. (See
[Decision 0001](decisions/0001-pivot-live-accompanist.md) and
[Decision 0003](decisions/0003-magenta-rt2-renderer-research.md) for what is
deferred and why.)

---

## 2. The Core Loop

The deepest simplification available to this project: **rehearsal and
performance run the same loop.** There is one runtime path — the difference is
what the system does with the data afterward, and how much it displays while
running. This keeps the codebase honest (no "performance path" that was never
rehearsed) and keeps the product honest (what you rehearsed is exactly what
will happen on stage).

That shared loop sits inside a three-workflow product lifecycle:

1. offline score bundling creates piece knowledge;
2. rehearsal turns completed takes into Eric-specific alignment and prior
   knowledge;
3. live performance consumes the prepared piece knowledge and frozen prior
   causally.

“Same runtime path” is an implementation invariant for the musical loop, not a
claim that rehearsal and performance are the same workflow. Rehearsal has
capture, background alignment, review, coverage, and profile updates. Live
performance has a hard real-time boundary and may record traces, but it never
changes the profile while playing.

### 2.1 The loop, precisely

```text
        ┌────────────────────────────────────────────────────────┐
        │                     every note-on                      │
        │                                                        │
 Yamaha ─▶ MIDI input ─▶ Score follower ─▶ Tempo model ─┐        │
        │   adapter        (where is he?)   (how fast?)  │       │
        │                        │                       ▼       │
        │                        │              Section policy   │
        │                        │             (follow/lead/     │
        │                        │              hold/stop)       │
        │                        ▼                       │       │
        │                  Trace logger ◀────────────────┤       │
        │                                                ▼       │
        │              Interpretation ──▶ Accompaniment          │
        │              profile (prior)     scheduler             │
        │                                       │                │
        └───────────────────────────────────────┼────────────────┘
                                                ▼
                                  output fanout ─┬▶ Yamaha synth
                                                 └▶ BBCSO room zone
```

Per-component behavior, tight enough to implement:

1. **MIDI input adapter.** Reads the CLP-795GP over USB. Timestamps each
   message on arrival with a monotonic clock. Target: events available to the
   follower within 5 ms of arrival. Chord grouping (notes within ~10 ms are
   one musical onset, per ACCompanion's convention) happens here or in the
   follower's feature processor, not downstream.

2. **Score follower.** Consumes timestamped note events plus the solo
   reference from the score bundle; emits `FollowerUpdate {score_beat,
   perf_time, confidence}` per salient onset. First production candidate is
   Matchmaker's OLTW (`arzt`) per
   [Decision 0002](decisions/0002-tracker-and-synth-mvp.md); the seam is the
   `ScoreFollower` protocol already proven by `OracleFollower` and
   `ReferencePitchFollower` (see
   [Simulated Online Harness](concepts/simulated-online-harness.md)). The
   follower is the only component that ever looks at pitches. Wrong notes,
   ornaments, and flourishes are its problem alone; everything downstream
   sees only position and confidence.

3. **Tempo model.** Smooths follower updates into a beat period estimate and
   predicts the wall time of future beats. Crucially, it blends two sources:
   the live estimate and the **interpretation profile** (Section 3) — the
   learned prior of how Eric plays this passage. Blend weight follows
   confidence in each: fresh piece, no takes → almost purely reactive;
   well-rehearsed passage played consistently → the prior carries most of the
   weight and the orchestra moves with quiet conviction. Raw inter-onset
   tempo is never used directly; it is too jagged to be musical
   (see [Accompaniment Control](concepts/accompaniment-control.md)).

4. **Section policy.** A lookup from score region + confidence to a mode:
   `FOLLOW` (solo passages), `LEAD` (tutti — the orchestra carries the music
   at the profile/score tempo while the soloist rests), `HOLD` (wait for a
   cue or safe re-entry), `STOP` (emergency). Explicit map, authored per
   movement, per [Section Policy](concepts/section-policy.md).

5. **Accompaniment scheduler.** Maintains a lookahead window (~500 ms of
   predicted wall time) over upcoming accompaniment events, converts their
   score beats to wall times through the tempo model's prediction, and
   dispatches them to the output adapter. Events already dispatched are never
   recalled; events still in the window are re-timed continuously as the
   prediction updates. Late corrections therefore bend the future, not the
   past — the orchestral equivalent of recovering gracefully instead of
   flinching. (Note: the current `AccompanimentScheduler` schedules each
   event exactly once and never revisits it; continuous re-timing of
   not-yet-dispatched events is a required refactor, covered by roadmap
   item 3.)

6. **Expressive renderer.** Maps scheduled events to concrete MIDI: GM/XG
   program/channel from the instrument map, velocity = notated dynamic ×
   learned dynamic curve × live intensity trend (Section 4.3), duration from
   the score at current tempo.

7. **Trace logger.** Every follower update, tempo estimate, mode transition,
   and emitted event goes to `runs/<run_id>/trace/*.jsonl`. Non-negotiable in
   both rehearsal and performance: a musical failure that isn't traced cannot
   be fixed.

### 2.2 Rehearsal vs. performance

| | Rehearsal | Performance |
|---|---|---|
| Runtime path | This loop | This same loop |
| Solo MIDI recorded | Always | Always (it's a performance — you want it) |
| Learning afterward | Take auto-aligns, folds into profile | Frozen — nothing changes the profile |
| Prior blend | Current profile | Current profile (identical code path) |
| UI | Live face with tempo/position detail | Live face, minimal variant |
| Starting point | Anywhere (right-click a printed measure) | Top of the movement by default; a selected printed measure starts an orchestra-led entry |
| After a run | Take card: keep / discard / listen | Nothing. The music ends. Review later |

The rehearsal-mode promise: **everything learned in rehearsal carries to
performance automatically**, because performance reads the same profile the
rehearsals wrote. There is no export step, no "training," no transfer. You
rehearse until the orchestra breathes with you; then you perform.

Startup behavior in both modes follows
[Realtime Performance Dataflow](concepts/realtime-performance-dataflow.md):
the bundle, profile, section map, and devices load before a note sounds; the
system sits in *Listening* until the follower locks (typically 2–4 salient
onsets); risky output is gated on confidence, not on a fixed warm-up timer.
When a printed measure is selected, its canonical and reference coordinates
seed an immediate orchestra-led entry instead of replaying the opening. The
local score menu exposes **Start live with orchestra here** so the performer
does not leave the chosen notation or scroll to a remote transport. There is no
metronome: the orchestra establishes the pulse and retains transport authority
until confident piano evidence hands control to the follower.

---

## 3. The Learning Model

### 3.1 What is learned

The minimum viable learned representation is deliberately humble: **two
beat-indexed curves with uncertainty, plus a small set of marked moments.**
No neural networks, no embeddings — order statistics over aligned takes. This
is not a compromise; it is the direct implementation of the strongest result
in the literature we hold (LTE's reference-performance prior), and every byte
of it is inspectable.

**The interpretation profile**, one per movement:

```text
data/profiles/<piece_id>/<movement>/profile.json

{
  "schema_version": 2,
  "piece_id": "chopin_op11",
  "movement": 2,
  "take_count": 14,
  "updated": "2026-07-07T18:40:00Z",
  "coordinate_system": "canonical_score",
  "canonical_ppq": 960,
  "grid_step_ticks": 480,
  "base_seconds_per_quarter": 0.91,
  "cells": [
    { "score_tick": 0, "seconds_per_quarter": 0.895,
      "seconds_per_quarter_mad": 0.031, "rubato_ratio": 0.984,
      "rubato_ratio_mad": 0.024, "velocity": 54.0,
      "velocity_mad": 6.5, "pedal": 0.42, "pedal_mad": 0.11,
      "support": 14, "mean_quality": 0.94 }
  ],
  "moments": [
    { "score_tick": 92160, "kind": "broadening", "consistency": 0.93 },
    { "score_tick": 116160, "kind": "breath", "consistency": 0.87 },
    ...
  ]
}
```

- `score_tick`: the sole cell identity on the canonical 960-PPQ timeline;
  `grid_step_ticks=480` samples every half-quarter.
- `seconds_per_quarter`: explicit absolute pace. `rubato_ratio` divides it by
  that take's robust baseline so local phrase shape survives global tempo
  changes. Both central values and weighted MAD are retained across takes.
- `velocity` and `pedal`: local performance baselines, with spread, support,
  and mean alignment quality. They do not replace scored expression.
- `moments`: places where takes consistently deviate from local tempo —
  detected ritardandi, luftpausen, holds. These get special scheduler
  treatment (widen the lookahead, wait for confirmation before the downbeat
  after a marked breath).

### 3.2 How it is learned

Learning is a byproduct of playing. The pipeline already exists in embryo as
the offline alignment scaffold
([Offline Alignment And Render](concepts/offline-alignment-render.md)):

1. Eric finishes a take. The solo MIDI is already on disk (recording is
   unconditional).
2. In the background, the take is aligned against the solo reference —
   note anchors, then phrase-level anchors for smoothing (per
   [Yamaha Take Analysis](concepts/yamaha-take-analysis.md), individual
   mismatches are noise: chord rolls, pedal artifacts, honest mistakes; the
   profile learns from stable matched groups, never from raw events).
3. The aligned timing map is resampled onto the profile's beat grid and
   folded into the running statistics with **recency weighting** — an
   exponential half-life of about eight takes, so the profile tracks Eric's
   *current* interpretation as it matures rather than averaging this month
   with last month.
4. The take card (Section 6.4) appears. Keeping the take is the default and
   requires nothing; discarding it (false start, phone rang) is one press
   and unwinds the fold.

Total human labor per rehearsal cycle: zero actions to learn, one optional
action to veto. This is the "minimize human labor" requirement made
structural: there is nothing else to do.

### 3.3 How it is applied

At runtime the profile enters in exactly three places:

- **Tempo prediction (the big one).** The tempo model's prediction for the
  next beats is a confidence-weighted blend of the live estimate and the
  profile's curve. Where `spread_s` is small (Eric is consistent here) and
  live confidence is moderate, the prior dominates — the orchestra
  *anticipates*, entering with him rather than a beat behind him. Where
  spread is large or the passage is unrehearsed, the model leans reactive
  and the orchestra plays more cautiously. In `LEAD` sections the prior (or
  notated tempo, if no takes) is the only clock.
- **Dynamic shaping.** The dynamic curve sets the accompaniment's expected
  loudness contour; the live intensity trend (Section 4.3) modulates around
  it in real time.
- **Moment handling.** Marked moments arm special behavior: at a consistent
  broadening, the scheduler pre-stretches; at a marked breath, it holds the
  next entrance until the follower confirms Eric has moved.

A useful way to say it: **the profile is the rehearsal letter markings in the
orchestra's parts.** The score says what to play; the profile says how *this
soloist* plays it; the live follower says where he is right now.

### 3.4 What is deliberately not learned (yet)

Articulation ratios, per-voice balance preferences, ornament timing,
anything requiring more machinery than aligned-take statistics. Each can be
added later as another curve or annotation on the same grid without changing
the storage shape or the runtime seams. Start humble; the grid is the
extension point.

---

## 4. The Accompaniment Engine

"Following" decomposes into three live questions — *where is he?* (follower),
*how is time moving?* (tempo model), *what should sound now?* (scheduler) —
answered continuously under a policy that knows when not to follow at all.

### 4.1 Position: the follower

The follower matches live onsets against the solo reference and emits
position + confidence. Design commitments:

- **Follower-agnostic seam.** Everything downstream consumes
  `FollowerUpdate` only. Matchmaker today; anything better tomorrow; the
  `OracleFollower` forever in tests.
- **Confidence is a first-class output**, not a debug value. It gates output
  risk (section policy), sets the prior blend (tempo model), and drives the
  UI state word (Section 6.3).
- **Expressive deviation is signal, not error.** Rubato — the practice, not
  the product — means the performed mapping constantly diverges from
  metronomic time. The follower's job is to ride that divergence; the tempo
  model's job is to smooth it; the profile's job is to expect it.

### 4.2 Time: the tempo model

Failure to model tempo is the classic accompanist disease, documented in
ACCompanion's musician feedback: a purely reactive system amplifies
hesitations (soloist slows → system slows → soloist hears it and slows
further — a death spiral in ensemble trust), while an over-smoothed one
feels like it is dragging the soloist behind it. The blend architecture in
Section 3.3 is the treatment: reactive where uncertain, convicted where
rehearsed. The model must also resist single-note anomalies (one clipped
note must not lurch the orchestra) and handle the marked moments — fermatas
and broadenings are score-time locations where wall time dilates, and the
profile tells us they are coming.

### 4.3 Dynamics: real-time intensity tracking

Live dynamic response is a hard product requirement. Mechanism:

- Compute a **live intensity trend** from solo note velocities: an
  exponentially weighted mean over roughly the last one to two seconds of
  onsets, normalized against the profile's dynamic curve at the current
  position (so "louder than usual *here*" is the signal, not absolute
  velocity).
- Map the trend to a bounded accompaniment velocity modulation (first cut:
  ±20% around the rendered dynamic, tuned by ear against the Yamaha's XG
  velocity response).
- Never let live dynamics override the score's structural dynamics — a
  subito piano in the orchestra part stays subito piano; the trend scales
  it, it doesn't erase it.

The pleasant property: crescendo with the orchestra swelling under you is
exactly the "it's alive" moment this system exists for, and it needs no
learning at all to work on day one — the profile only refines the baseline.

### 4.4 When not to follow: section policy

A concerto is a conversation with long stretches where the pianist is
silent. Following is undefined there; the orchestra must carry. The section
map assigns each region `FOLLOW` / `LEAD` / `HOLD` / `STOP` with authored
boundaries, and policy arbitrates transitions: a `LEAD` tutti ends at a
solo re-entry, where the follower re-acquires (helped by cue-note matching —
the first solo onsets after a tutti are usually distinctive) and control
crosses back to `FOLLOW`.

### 4.5 Failure modes and their musical answers

The design principle: **a lost accompanist should behave like a good
musician who is lost — get quiet, keep counting, and come back in at a place
of certainty.** Never silence mid-phrase from a confidence wobble; never
plow ahead fortissimo while lost. Each failure has a musical behavior and a
one-word UI state (Section 6.3):

| Failure | Musical behavior | UI state |
|---|---|---|
| Confidence dips (dense passage, ornaments) | Keep moving on blended prior; damp new entrances slightly; widen re-sync tolerance | `Following` dims toward `Listening` |
| Follower lost (repeated figuration, big skip) | Finish the current phrase on the prior, taper dynamics, then hold at the next phrase boundary; listen for a distinctive re-entry match anywhere in a widening window | `Waiting` |
| Soloist pauses mid-phrase | Sustain/hold per section policy; profile's marked breaths distinguish "his usual pause" (wait, calmly) from "something's wrong" (taper and hold) | `Waiting` |
| Soloist skips or repeats a measure | Follower re-acquires at the new position; scheduler jumps the lookahead window — never plays catch-up through skipped material at 4× speed | brief `Listening`, then `Following` |
| Long tutti, then re-entry | `LEAD` at profile tempo; re-acquire on cue notes | `Leading`, then `Following` |
| MIDI device vanishes | All-notes-off immediately, stop, preserve trace and partial take | `Silent`, with "Connection lost; all sound stopped" as the secondary status line |
| Eric presses Silence | All-notes-off + stop, everything else preserved | `Silent` |

Recovery is also a UX contract: from `Waiting`, Eric can simply **start
playing anywhere** and the system re-acquires — restarting must never
require touching the laptop.

---

## 5. The Workflow

The complete journey, from "I have a new piece" to "I just performed it."
Six named stages; at every point the interface presents exactly one obvious
next action. Stages 2–5 are the weekly loop; 1 and 6 are the bookends.

```text
 1 INGEST ──▶ 2 SOUND CHECK ──▶ 3 FIRST CONTACT ──▶ 4 REHEARSAL ⟲ ──▶ 5 PERFORMANCE ──▶ 6 REFLECTION
   (once            (automatic,        (once per            (the loop:          (same loop,        (optional,
    per piece,       every session)     piece: hear it,      play · keep ·       frozen profile,     later, away
    mostly agents)                      trust it)            learn)              minimal UI)         from piano)
```

**Stage 1 — Ingest.** A new piece arrives as MusicXML/MIDI/PDF and becomes a
canonical score bundle: solo reference, accompaniment events, section map,
instrument map ([Score Bundle Contract](concepts/score-bundle-contract.md)).
This is agent work with Eric consulted only where the score is genuinely
ambiguous (repeats, cadenza boundaries, cue choices). Eric's experience of
this stage should be: *"I asked for the Larghetto; the next time I opened the
laptop, the Larghetto was there."* An empty interpretation profile is created
alongside.

**Stage 2 — Sound Check.** Happens automatically when the app opens: find
the Clavinova in and out, verify with a silent round-trip, load the piece
and profile. The Ready face (Section 6.2) shows the result as a quiet
checkmark, not a form. Only on failure does it become interactive — one
line, one retry button. This replaces "Setup" as a screen; setup is not a
place Eric goes, it is a thing that happens.

**Stage 3 — First Contact.** The first time through a new piece, trust is
zero — for both parties. This stage exists to build it cheaply, using the
machinery that already exists today: record a cued take (orchestra lead-in,
then Eric enters — the current workflow in
[Yamaha MIDI Setup](runbooks/yamaha-midi.md)), auto-render the offline
accompaniment against that take, and play the recorded solo and retimed
orchestra together on the Yamaha. Eric hears the orchestra shaped to a
performance he just gave, *before* any live following is attempted. The UI
owns that whole handoff: stop → alignment → review preparation → one playback
action. Session IDs, uploads, render commands, and individual artifact variants
are implementation details. The system gets its first profile fold; Eric gets
evidence. This is also the permanent fallback path whenever live following
misbehaves: it isolates alignment/render quality from real-time behavior.

**Stage 4 — Rehearsal.** The heart of the product; the loop of Section 2
with learning on. Sit down → *Rehearse* → play (a passage or the whole
movement; jump anywhere) → stop → take card → play again. Each kept take
sharpens the profile; over sessions, the state word spends more of its time
on `Following`, entrances tighten, and the tempo blend shifts from reactive
to convicted. Progress is *audible*, and the UI's only progress indicator is
the pass count attached to each score location and profile strength on the
Ready face — no global take counter, no dashboards, no charts at the piano.

**Stage 5 — Performance.** *Perform* on the Ready face. Same loop, profile
frozen, top of the movement, and the Live face drops to its minimal variant:
position, one state word, Silence. No tempo readouts, no confidence — a
performer does not want telemetry, and anything on screen at 2 m is
something Eric might involuntarily read mid-phrase. The take is recorded
like any other (it would be absurd for the *performance* to be the one
thing not captured).

**Stage 6 — Reflection.** Later, away from the piano — possibly on a
different device, possibly by an agent preparing a summary. Traces, tempo
curves against profile, alignment metrics, listening to takes, feedback
notes filed to the run folder. Deliberately excluded from the at-piano
surfaces: analysis is for after, not during. This is where
[Experimentation](ML_EXPERIMENTATION.md) metrics meet Eric's ears.

---

## 6. The UI

### 6.1 One window, three faces

The entire at-piano interface is **one window with three mutually exclusive
faces** — Ready, Live, After — that the system moves between on its own.
Eric never navigates; the situation navigates. (A fourth surface, the
Library, is a drawer on the Ready face used a few times a month; Reflection
tooling lives outside this window entirely.)

The masthead exposes three stable score workspaces in task order: **Data · Mix ·
Perform**. Perform collapses rehearsal and performance onto their shared causal
runtime; Data owns tracking evidence and correction; Mix owns the spatial
program. The engraved score and MusicXML-derived measure labels stay visible in
all three. A compact Layers menu controls whether tracking data or mix cues are
visible in each workspace, while interaction remains mode-owned: reactive
anchors are editable only in Data and automation only in Mix. Perform defaults
to a clean score with both optional overlays off. See
[Mixing Mode](design/MIX_AUTHORING_MODE.md).

Design constraints, stated as hard rules:

- Readable from 2 m: primary state legible at that distance (measure number
  ≥ 120 px equivalent; state word ≥ 40 px).
- One primary action per face, physically large (≥ 64 px tall), always in
  the same screen position.
- Silence is visible on every face, top right, always the same pixels —
  muscle memory space. (Already established in the cockpit; see
  [PWA Rehearsal UI](concepts/pwa-rehearsal-ui.md).)
- Spacebar = the primary action of the current face. Escape = Silence. The
  whole system is playable with one hand and no aim.
- Nothing on any face requires text entry at the piano. Naming, tagging,
  and file wrangling are Reflection-stage activities.
- In rehearsal, the engraved score is the persistent spatial anchor. The
  rendered music, current-position cursor, and adjacent situation controls
  must fit in one desktop viewport through cue, recording, stop, and review;
  controls may not push the score above the fold.
- A score-local edit stays local: its action surface opens beside the clicked
  notation, never behind a mode switch or in a remote page region. Existing
  marks are directly manipulable, destructive edits are explicit, and every
  such edit offers immediate Undo in the same visual neighborhood. Pointer
  travel and scroll travel are part of the interaction cost, not cosmetic
  concerns.
- “Latest take” and “coverage” are separate visual truths. The first marks the
  just-aligned score span. Coverage accumulates kept takes; one observation is
  amber/touched, while the repeated-take target is green/covered.
- Coverage color always carries a sentence and a next action. Amber means
  “learning: one or more aligned observations, but some half-beat has fewer
  than three observations or less than 50% average alignment quality”; green
  means “every half-beat meets both targets; another pass is optional.” It
  never means alignment succeeded or
  failed. A score context menu exposes record, latest-take review, accompanied
  review, and passage details at the clicked measure.
- Tracking annotations and mix-program annotations are separate modalities.
  Layer visibility is configurable, but correction actions remain available
  only in Data and mix editing only in Mix. Both use one downward beat-marker
  primitive: a Data anchor attaches reactive timing behavior, while a hollow or
  filled Mix marker starts or ends a horizontal notation-like effect span.
- The score explains its own overlays. Any control that names a measure must
  have a visible `m.` label and target marker on the engraving, and every color,
  hatch, outline, or cursor needs a nearby plain-language key. Color is never
  the only carrier of meaning. An automatically suggested passage must also
  bring its PDF page into view.

### 6.2 The Ready face

What Eric sees when he sits down:

```text
┌──────────────────────────────────────────────────────────────────┐
│ RUBATO                                              [ ⏻ Silence ]│
│                                                                  │
│                                                                  │
│                     Chopin · Op. 11                              │
│                      II. Larghetto                               │
│                                                                  │
│            14 takes rehearsed · orchestra knows you              │
│               Clavinova in ✓ · Clavinova out ✓                   │
│                                                                  │
│                 ┌──────────────────────────┐                     │
│                 │       ▶  Rehearse        │                     │
│                 └──────────────────────────┘                     │
│                        Perform ▸                                 │
│                                                                  │
│  ⌸ Library                        last take yesterday · kept     │
└──────────────────────────────────────────────────────────────────┘
```

- The piece is simply *there* — last piece resumes. Changing pieces is the
  Library drawer: a short list of ingested bundles, one tap.
- Profile strength in words, not numbers: "first rehearsal" → "learning your
  tempo" → "orchestra knows you." One honest sentence, no gauges.
- Device health is a checkmark, not a settings panel. Tapping it reveals
  the picker only if something is wrong or Eric asks.
- *Rehearse* is the hero. *Perform* is quieter — present, aspirational,
  never nagging. Pressing either starts listening immediately; **Eric's
  hands go to the keys, and playing is what starts the music.** In `LEAD`
  openings (orchestra begins), the Ready face names where the orchestra starts
  and where Eric enters; pressing the explicit orchestra-start action starts
  the first sounding score event rather than waiting for piano input.

### 6.3 The Live face

While playing — the face that must be perfect and is therefore the
emptiest:

```text
┌──────────────────────────────────────────────────────────────────┐
│ ● Following                                         [ ⏻ Silence ]│
│                                                                  │
│                                                                  │
│                                                                  │
│                          m. 37                                   │
│                                                                  │
│                       ♩ = 66   ·   p                             │
│                                                                  │
│      solo ────────────●────────── tutti ───────── solo ── end    │
│                                                                  │
│                                                                  │
│                      ⏹  Stop (space)                             │
└──────────────────────────────────────────────────────────────────┘
```

- **The state word** (top left) is the single channel for all engine and
  failure states: `Listening` (dim brass — acquiring), `Following` (brass —
  locked to you), `Leading` (brass — tutti, orchestra carrying),
  `Waiting` (dim ivory — holding for you; play anywhere to resume),
  `Recording` (ember — cued-take flow), `Silent` (gray). Color and word
  change together; nothing else on screen changes its meaning. Confidence
  is expressed *within* the word's brightness, not as a number — Eric needs
  "it's with me / it's unsure / it's waiting," never "0.83."
- **The measure number** is the anchor and the largest thing on screen —
  the answer to the only question a playing pianist asks the machine:
  *are you with me?* Beneath it, current tempo and dynamic level in small
  type (rehearsal variant only).
- **The journey bar** is a thin strip mapping the movement: section modes
  as subtle segment shading (solo vs. tutti), the playhead as a brass dot.
  At 2 m it reads as "where in the piece, roughly" — that is all it is for.
- The pulse dot beside the state word breathes with the live beat — visual
  confirmation of lock that peripheral vision can consume. (Respects
  `prefers-reduced-motion`, as the cockpit already does.)
- Performance variant: state word, measure number, journey bar, Silence.
  Nothing else — the tempo/dynamic readouts and the Stop affordance's label
  disappear (space still stops).

### 6.4 The After face

The moment after Stop, the only decision of the loop — and it defaults to
requiring no decision at all:

```text
┌──────────────────────────────────────────────────────────────────┐
│ RUBATO                                              [ ⏻ Silence ]│
│                                                                  │
│    Take 15 · Larghetto · mm. 1–46 · 3:12                         │
│                                                                  │
│    tempo ▁▂▃▂▁▂▄▆▄▂▁  (you, over your usual)                     │
│    entrances together · one late orchestra entry at m. 24        │
│                                                                  │
│                 ┌──────────────────────────┐                     │
│                 │      ▶  Play it back     │                     │
│                 └──────────────────────────┘                     │
│                                                                  │
│         Kept ✓ (learning from it)         Discard ✗              │
│                                                                  │
│                     Rehearse again (space)                       │
└──────────────────────────────────────────────────────────────────┘
```

- The take is **already kept and already learning** when this face appears;
  `Discard` is the veto. One glance, and space bar rolls into the next
  take.
- One or two plain-language observations from the trace (an entrance
  offset, a spot where the follower lost him) — the system's side of the
  rehearsal conversation. Not a metrics table; those live in Reflection.
- The face appears immediately after Stop and tells the truth while work
  advances: `Saving take…` → `Placing it in the score…` → `Preparing
  orchestra playback…`. These are durable backend job states, not a chain of
  browser promises; refresh and server restart recover the same operation.
- *Play it back* (product label: **Hear take + orchestra**) replays the
  recorded solo and rendered accompaniment together on one synchronized
  timeline through the selected Yamaha output. **Solo only** remains a
  secondary diagnostic action. Loading only `accompaniment.mid` is not a
  substitute: without the recorded solo, Eric cannot judge ensemble timing.
- Outside the immediate After card, recordings are organized by their musical
  entry on the PDF: **From measure 12**, **From measure 53**, and so on.
  Multiple recordings at one entry are local passes within that passage.
  Global IDs and capture order remain implementation metadata because they do
  not help a performer find anything in the music. The selected passage owns
  one compact recording picker; passage groups in the broader library stay
  collapsed until opened. Recording another pass never requires navigating a
  vertical take list: the same **Record pass** action is always present, with
  a nearby **Orchestra cue** switch that defaults on and names its two-measure
  musical lead-in. Destructive-sounding curation verbs are kept in secondary
  recording options and described as recoverable.
- An ambiguous alignment replaces playback with **Choose location** and
  resumes preparation after the choice. A render failure preserves the take
  and alignment and offers **Retry orchestral review**. A missing output
  directs Eric to Sound Check selection/refresh. No failure sends Eric to a
  terminal.
- After review, **Record another take** continues the learning loop and
  **Go live · Experimental** enters the live hardware/follower test with the
  same Sound Check input and output choice. The output may be the Yamaha synth
  or **None — orchestra via VST only** when a configured room zone owns the
  orchestra. Review informs Eric's decision; it is not a hidden
  performance-readiness gate.

Implemented foundation (2026-07): capture queues background alignment, then the
UI automatically queues a durable take-owned review job for the latest aligned
take. The worker consumes the persisted alignment, writes normalized solo,
cropped/retimed accompaniment, and combined ensemble artifacts, and projects
state through `TakeResponse.review` plus `take:review_status`. The persistent
**After this take** card exposes Yamaha **Hear take + orchestra**, browser
**Preview here**, **Solo only**, **Record another**, retry, and experimental Go
Live without a Library or terminal detour. The owning implementation detail is
recorded in [PWA Rehearsal UI](concepts/pwa-rehearsal-ui.md#take-native-accompanied-review).

The score-first rehearsal workstation keeps the actual PDF page beside those
controls. Sparse score-transport anchors drive a continuously scanning,
sub-measure cursor during cue and accompanied review, with page lookahead;
performed take spans are durable response data and paint as a latest-take
layer through a bounded terminal sustain/rest estimate. Piano-active gaps are
inferred from the declared
solo MIDI through the provisional display map, selectable directly on the
engraving, and feed a **From here** orchestral cue. The coverage hero reports the honest nonzero
observed percentage after one kept take while retaining the three-take maturity
threshold. Ordinary capture copy says **in the bank** / **kept**, not “safe.”

### 6.5 What happened to the cockpit

The large “What do you want to do?” launcher and its explanatory intent cards
are removed. They repeated the same three concepts with too much prose and
pushed the score down. At rest, Perform has one compact action rail: **Go
live**, **Record passage**, and **Orchestra**. Each action has distinct text and
iconography, while the persistent workspace switch answers the higher-level
modality question. Keyboard shortcuts stop an active workflow but do not
silently choose one from the idle state.

The current Perform/Record/Session cockpit
([PWA Rehearsal UI](concepts/pwa-rehearsal-ui.md)) is the correct *plumbing*
— backend device selection, cued recording, offline render, hardware
playback, Silence in the masthead — arranged as three parallel tool decks.
The first-class take-to-review contract and combined solo-plus-orchestra
playback now sit above that plumbing; remaining evolution is principally the
full three-face information architecture and richer musical observations.
This design keeps every one of those capabilities and re-hangs them on the
situation: device pickers fold into Sound Check, the Record deck becomes the
cued-take path within Rehearse, the Latest Take card becomes the After face,
and the Session deck's upload/variant management moves to the Library drawer
and Reflection. The cockpit's aesthetic groundwork (palette, masthead,
Silence) carries forward unchanged. Session/slot vocabulary disappears from
the visible product: Eric has *pieces* and *takes*; sessions remain a
backend concept.

**Mix** is an explicit idle-state workspace in the three-way masthead switch,
not a hero route or overlay checkbox. Choosing Perform or Data leaves it;
Perform cannot start while a live mixing audition remains active.

---

## 7. The Aesthetic Language

The room this software lives in contains a grand piano. The design bar:
**it should feel inevitable next to the instrument** — closer to a Steinway
fallboard, a concert program, or a well-engraved score than to any web app.
The 2026-07 cockpit redesign chose this direction
(stage-black/ivory/brass/ember, Fraunces + Inter); this section makes it
law and extends it.

**Color.** The existing palette is affirmed as canonical (values in
`webapp/src/app.css`):

- `--stage #121317` — near-black, warm, the unlit hall. Backgrounds only.
- `--ink #f4efe6` — warm ivory, all primary text. Never pure white.
- `--brass #d8a94c` — the single accent: primary actions, the following
  state, the playhead. Brass is *earned* — it marks music happening or
  about to happen, nothing else.
- `--ember #d1483a` — recording and Silence only. Ember never decorates.
- `--ready #7fae67` — small confirmations (device checks, kept takes).
- No purple, no gradients-as-decoration, no glassmorphism. One radial
  brass glow at the top of the stage is the hall's house lights, already
  present, and enough.

**Typography.** Fraunces for identity and musical text (piece titles,
movement names, the state word); Inter for operational text; **tabular
numerals everywhere a number can change** (measure, tempo, timings) so
nothing shivers while playing. The measure number on the Live face is the
typographic event of the product — Fraunces, huge, ivory. Sentence case
throughout; small-caps eyebrow labels sparingly. No exclamation points
anywhere in the product voice, ever: the voice is a stage manager, calm and
brief — "Orchestra ready." "Take 15 kept." "Connection lost; all sound
stopped."

**Space and rhythm.** Engraver's spacing: few elements, generously
separated, aligned to a strict vertical rhythm (8 px base). One idea per
region; silence between them. Panels keep the cockpit's soft-shadowed cards
but tend toward fewer, larger, calmer. Hairlines (existing
`--stage-hairline`) instead of boxes wherever separation is needed.

**Motion.** Motion means music. The pulse dot breathes at the live tempo;
face transitions are a ≤200 ms crossfade (stage lights, not slide-in
drawers); nothing else moves. `prefers-reduced-motion` collapses all of it
to state changes. Nothing ever animates to seem busy.

**Sound.** The UI is mute. All sound is music, through the piano. (The
count-in for orchestra-first starts is visual.)

**Feel.** Controls respond like good keys: immediate press feedback
(the existing subtle scale-down), no debounce hesitation on the primary
action, focus states in brass. Hover is a courtesy, not a requirement —
everything works by tab/space and by touch.

---

## 8. Open Questions and Tradeoffs

Decisions to make before or during early implementation. Each carries a
recommendation so agents can proceed unless Eric overrules.

1. **Re-entry after tutti (the cue problem).** How does the follower
   re-acquire when Eric enters after a long `LEAD` section — especially if
   he enters early or late? *Recommendation:* explicit cue windows in the
   section map (expected entry beat ± tolerance), distinctive-onset
   matching within the window, orchestra briefly elastic (±1 beat) at the
   seam. Fallback: spacebar as a manual "I'm here" cue. Needs live
   experiments; this is the highest-risk musical moment.

2. **Prior/live blend weights.** The confidence-weighted blend (Section
   3.3) needs a concrete function and tuning. *Recommendation:* start with
   inverse-variance weighting with a live-confidence multiplier and a floor
   that keeps ≥25% live weight in `FOLLOW` sections (never fully ignore the
   human), then tune against simulated-online replays of real takes before
   any live tuning.

3. **Interpretive drift vs. profile stability.** If Eric changes his mind
   about a passage, how fast should the orchestra agree? *Recommendation:*
   the eight-take half-life (Section 3.2), no UI. Revisit only if he
   reports the orchestra "arguing" with a new interpretation; resist adding
   a reset button until then.

4. **Measure numbers for MIDI-only sources.** The Oguri bundle has no
   measure map — the live anchor today is ticks/seconds
   ([Oguri source](sources/oguri-kunstderfuge-midi.md)), but the Live face
   promises `m. 37`. The file carries a single 120 BPM tempo event, and it
   is a sequenced *performance*: if its expressive timing is baked into
   tick positions (likely, given Eric preferred its playback over the
   metronomic MuseScore export), tick beats are proportional to wall time,
   not musical beats, and beats-per-measure on the grid is not constant —
   a mechanical grid ÷ time-signature derivation would produce wrong
   barlines. *Recommendation:* first inspect whether the timing is
   expressive or quantized; if expressive, derive the measure map by
   aligning the Oguri solo track to a metronomic score reference (e.g., a
   Larghetto MusicXML export) or by hand-anchoring measure downbeats
   against the PDF, then spot-check. Until the map exists, show rehearsal
   letters or a bar-count-from-entry rather than fake measure numbers.

5. **Latency budget and anticipation offset.** Yamaha round-trip latency is
   unmeasured. *Recommendation:* a hardware measurement runbook item before
   live scheduling work; scheduler gains a single global `output_advance_ms`
   calibration constant. Budget goal: ≤30 ms output jitter, follower→sound
   response comfortably under 100 ms for reactive corrections.

6. **Local control and the doubled-piano question.** Can the CLP-795GP
   sound its own piano voice for Eric while receiving orchestra on other
   channels — and should local control be split? Open hardware question in
   [Yamaha MIDI Setup](runbooks/yamaha-midi.md). *Recommendation:* resolve
   in the next hardware session; it constrains the whole output design and
   costs one evening to answer.

7. **Profile scope.** Per movement (recommended) or per excerpt?
   *Recommendation:* per movement on a global beat grid; excerpt rehearsal
   folds into the same curves at the covered beats. `n` per grid cell
   already records unevenness.

8. **Keep-by-default risk.** A truly bad take pollutes the profile until
   decayed. *Recommendation:* keep-by-default with a robustness guard —
   folds are median-based and a take whose curve is a gross outlier
   (>3× spread over long spans) is quarantined with the observation shown
   on the After face ("this one was different — kept, but not learning
   from it"). No prompt.

9. **Velocity calibration on XG voices.** MIDI velocity → perceived
   loudness is voice-dependent; dynamic response may feel wrong even when
   numerically right. *Recommendation:* a one-time per-instrument-map
   calibration pass by ear (agent plays velocity ladders, Eric ranks),
   stored in the instrument map. Accept "serviceable, not beautiful" —
   Yamaha XG realism is explicitly a non-goal at this stage
   ([Decision 0002](decisions/0002-tracker-and-synth-mvp.md)).

10. **Does performance mode freeze learning?** Section 2.2 says yes.
    Counterargument: a performance is the best take there is.
    *Recommendation:* freeze during, then offer the fold in Reflection —
    never at the piano, and never silently, because a performance under
    adrenaline may not be the interpretation to teach.

11. **Web MIDI vs. backend-only I/O.** The cockpit consolidated on
    backend-owned MIDI (one source of truth, no dual pickers).
    *Recommendation:* keep backend-only. Latency-sensitive following wants
    the Python runtime owning the stream; the browser stays a display.

12. **Svelte app evolution vs. rebuild.** *Recommendation:* evolve. The
    aesthetic base, API client, and Silence/masthead pattern carry over;
    the change is information architecture (faces instead of decks), which
    Svelte handles as component restructuring, not a rewrite.

---

## 9. Implementation Roadmap

Ordered so each item is independently pickable by an agent, has a crisp
"done when," and delivers value even if the next item never happens. Items
1–4 make live following real; 5–7 make it musical; 8–10 make it Eric's;
11–13 make it a product.

1. **Live follower spike (no sound).** Wire Matchmaker (`arzt`) to live
   Yamaha input against the Oguri movement-2 solo reference; log
   `FollowerUpdate` traces while Eric plays; no output. *Done when:* a
   trace from a real take shows lock within 4 onsets and plausible beat
   tracking through the first solo span, and the same harness replays
   deterministically in simulated-online mode.

2. **Hardware latency measurement.** Measure input-to-output round trip
   through the full stack; record method + numbers as a runbook/source
   note; add `output_advance_ms` to the scheduler. *Done when:* the number
   is in docs and applied.

3. **Live loop v0 — follow only.** Follower → existing `OnlineTempoModel` →
   `AccompanimentScheduler` → Yamaha output, `FOLLOW` mode only, on a
   1-minute movement-2 excerpt. Purely reactive (no profile). Full traces.
   *Done when:* Eric plays the excerpt and the orchestra sounds with him
   end to end, and a simulated-online replay of a recorded take produces
   the same trace shape in CI.

4. **Movement-2 section map + policy transitions.** Author
   `sections.json` for the Larghetto (solo/tutti/holds); implement
   `LEAD` playback and `FOLLOW↔LEAD` seams; first cue-window re-entry
   (Open Question 1). *Done when:* a full-movement simulated run
   transitions modes correctly and one live session survives a tutti.

5. **Interpretation profile v1 — write path.** Beat-grid resampling of
   offline-alignment output; recency-weighted median/spread folds;
   `profile.json` contract; auto-fold on take completion; CLI
   (`rubato profile show/fold/unfold`). *Done when:* three real takes
   produce a profile whose tempo curve visibly tracks Eric's rubato, and
   fold/unfold round-trips in tests.

6. **Prior-blended tempo model.** Blend profile prior with live estimate
   (Open Question 2); use profile/notated tempo in `LEAD`. Evaluate on
   simulated-online replays: onset error with prior vs. without, per
   [Experimentation](ML_EXPERIMENTATION.md) metrics. *Done when:* blended
   beats purely-reactive on real-take replays and the improvement is
   recorded in a run analysis.

7. **Live dynamics.** Intensity trend + bounded velocity modulation +
   dynamic-curve baseline; velocity-ladder calibration pass (Open
   Question 9). *Done when:* Eric plays a passage twice, piano then forte,
   and reports the orchestra audibly moving with him both times.

8. **The three faces — Ready and Live.** Restructure the Svelte app:
   auto Sound Check, Ready face, Live face with state word / measure or
   letter anchor / journey bar / pulse; spacebar+escape model; WebSocket
   live status from the runtime. Cockpit plumbing folds in per Section
   6.5. *Done when:* a rehearsal happens start-to-finish without touching
   anything but Rehearse and the keyboard, and every engine state is
   legible from 2 m.

9. **The After face and take-native review.** The 2026-07 foundation now has a
   persistent post-take card, durable automatic alignment-to-render handoff,
   synchronized solo-plus-orchestra review on hardware/browser, and no
   session/terminal detour. Complete the target with a true mutually exclusive
   After face, keep-default/discard-veto wired to profile fold/unfold, and one
   trace-derived observation line. *Done when:* the post-take review remains
   the sole focused situation, a newly recorded take reaches **Hear take +
   orchestra** without a terminal or Library detour, both parts are audible in
   sync, recovery works across refresh, and the rehearse → stop → glance →
   space → rehearse loop takes under five seconds of human interaction time
   (background preparation may continue visibly).

10. **Failure-mode behaviors.** Implement the Section 4.5 table: taper-
    and-hold on lost, `Waiting` + play-anywhere re-acquisition, skip
    handling, disconnect safety. Simulated tests inject each failure.
    *Done when:* each row has a deterministic test and the state word
    tells the truth throughout.

11. **Performance mode.** Frozen profile, minimal Live variant, top-of-
    movement start, Reflection-side fold offer (Open Question 10).
    *Done when:* Eric performs the Larghetto end to end and afterward the
    take, trace, and render are waiting for review.

12. **Measure map + journey bar fidelity.** Beat→measure mapping for the
    Oguri bundle (Open Question 4); real measure numbers and section
    shading. *Done when:* the Live face's `m. N` matches the PDF at spot
    checks.

13. **Reflection surface v1.** Off-piano review: takes, tempo-vs-profile
    curves, alignment metrics, feedback notes to run folders. Can be
    plain generated HTML/notebook per run before it is an app surface.
    *Done when:* Eric reviews a week of rehearsal away from the piano and
    files feedback an agent can act on.

Then: the Allegro maestoso (movement 1) excerpt by excerpt, additional output
targets beyond the bounded BBCSO room renderer (IAC/Logic/Kontakt), and — far
down the road, per
[Decision 0003](decisions/0003-magenta-rt2-renderer-research.md) — neural
audio rendering. None of it changes the loop, the profile, or the three
faces. That is the point of this design: the architecture already knows how
to grow, and the product already knows how to stay small.

---

## Related

- [System Design](SYSTEM_DESIGN.md) — architecture and data contracts
- [Accompaniment Control](concepts/accompaniment-control.md) — control-layer
  synthesis
- [Score Following](concepts/score-following.md) — follower method families
- [Section Policy](concepts/section-policy.md) — FOLLOW/LEAD/HOLD/STOP
- [Offline Alignment And Render](concepts/offline-alignment-render.md) — the
  learning pipeline's foundation
- [PWA Rehearsal UI](concepts/pwa-rehearsal-ui.md) — current cockpit
  implementation
- [Mixing Mode](design/MIX_AUTHORING_MODE.md) — Perform/Data/Mixing score
  workspace boundary and mix-program editing
- [ACCompanion Evaluation](sources/accompanion-evaluation.md) — evidence for the
  rehearsal-prior thesis
- [Decision 0002](decisions/0002-tracker-and-synth-mvp.md) — tracker and synth
  starting choices
