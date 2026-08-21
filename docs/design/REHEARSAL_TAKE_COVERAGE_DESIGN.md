# Rehearsal Take Coverage: System Design and UX

> Record freely. The system does the bookkeeping. The score tells you what's left.

This document designs the **take-driven rehearsal workflow**: the soloist records many
short, overlapping, imperfect takes of the solo part; the system aligns each
take to the global score offline, fuses expression data where takes overlap,
computes coverage of the piano part, and shows that coverage painted directly
onto the PDF score. The accumulated expression profile then serves as the prior
for live performance following.

It extends — and does not replace — [Vision and UX Design](../VISION_AND_UX_DESIGN.md)
(the three faces, the core loop, the interpretation profile) and
[System Design](../SYSTEM_DESIGN.md) (score bundle, follower/tempo/scheduler
seams). What is new here:

1. Takes become first-class, score-located objects (not session slots).
2. Alignment gains a **localization** stage: a take can start anywhere in the
   movement, so the system must find *where* before it can align *how*.
3. Coverage becomes a computed, displayed quantity — the score-with-overlay is
   the rehearsal to-do list.
4. The profile fold pipeline is generalized from "one take = one pass through
   the excerpt" to "many partial, overlapping takes accumulate per-region."

The three phases in the soloist's words map onto the system like this:

| Phase | System name | Owned by |
|---|---|---|
| 1. Rehearsal / take recording | **Take capture** | This doc §3.1, §4.2 |
| 2. Offline alignment & coverage | **Alignment + coverage pipeline** | This doc §2, §3.2–3.4, §4.3 |
| 3. Live performance with prior | **Prior-blended following** | [Vision §3.3–4.2](../VISION_AND_UX_DESIGN.md); integration notes in §3.5, §4.4 |

---

## 1. Design Position (read this first)

Opinionated calls made in this document, so implementers don't relitigate them:

1. **Alignment is symbolic, not audio.** The input is MIDI from a Disklavier;
   we have exact pitches and millisecond onsets. Chromagram matching (librosa)
   is an audio-domain tool and throws that away. **Do not build an audio
   pipeline for this.** The recommended algorithm is *seeded local sequence
   alignment* on note events (§2.4): n-gram seeding for localization, then
   Smith–Waterman-style local alignment (a constrained DTW variant on the
   symbolic event sequence) for refinement. It is an extension of the
   pitch-sequence aligner that already exists in
   `src/aimusic/accompaniment/offline_alignment.py`.

2. **Coverage truth lives on the canonical grid; coverage display lives on
   measures.** The interpretation profile defines a 960-PPQ grid
   (`grid_step_ticks: 480`) with per-cell support. Coverage *is* support (plus quality),
   rolled up to measures for the score overlay. No second bookkeeping system.

3. **The PDF overlay and musical clock are separate mappings.** Audiveris GRID
   supplies measure boxes and full recognition supplies note/beat x evidence;
   neither defines score time. The offline beat-fusion artifact joins that
   geometry to canonical score ticks and reference MIDI. Human anchoring is an
   exceptional repair path, not the normal build or a runtime dependency.

4. **Take capture must be dumber than the current cockpit.** One button, no
   session naming, no cue configuration required, no device fiddling. The
   existing `LiveControl.start_record` path already does uncued recording; the
   work is UI subtraction plus automatic take registration.

5. **Everything folds through the same profile.** The live-performance prior is
   the interpretation profile from [Vision §3](../VISION_AND_UX_DESIGN.md),
   unchanged in role. This design changes how the profile gets *filled*
   (many partial takes) and adds coverage/pedal data, not how it is consumed.

6. **Ambiguity is deferred, never guessed silently.** A take that could match
   two places in the score (concertos repeat material) is stored, flagged
   `ambiguous`, excluded from the profile, and surfaced for one-tap resolution
   later — never folded on a coin flip, never blocking the recording flow.

---

## 2. System Architecture

### 2.1 Data flow, end to end

```text
 PHASE 1 · CAPTURE                PHASE 2 · OFFLINE PIPELINE (background worker)
┌───────────────────┐   take.mid ┌──────────────┐  ┌──────────────┐  ┌─────────────┐
│ Yamaha Disklavier │──────────▶│ 1 LOCALIZE    │─▶│ 2 ALIGN      │─▶│ 3 QUALIFY   │
│  Record ▸ play ▸  │  take.json │  n-gram seed  │  │  local seq.  │  │  score span │
│  Stop  (repeat)   │            │  where in the │  │  alignment   │  │  quality    │
└───────────────────┘            │  score is it? │  │  (SW / DTW)  │  │  gating     │
        │                        └──────────────┘  └──────────────┘  └──────┬──────┘
        │ pedal CC64, velocities kept in raw MIDI                           │
        ▼                                                                   ▼
┌───────────────────┐            ┌─────────────────────────────┐   ┌───────────────┐
│ Take store        │◀──────────│ 4 FOLD                       │◀──│ aligned_take  │
│ data/takes/…      │  status    │  resample → score_tick grid  │   │ .json         │
└───────────────────┘  updates   │  recency-weighted median     │   └───────────────┘
                                 │  fuse velocity/timing/pedal  │
                                 └──────────────┬──────────────┘
                                                ▼
                     ┌──────────────────────────────────────────────┐
                     │ Interpretation profile  (profile.json)       │
                     │  canonical timing/rubato/dynamic cells       │
                     │  per-cell support  ⇒  COVERAGE INDEX         │
                     └───────────┬──────────────────────┬───────────┘
                                 ▼                      ▼
                     PHASE 2 UI: coverage       PHASE 3: live loop
                     overlay on PDF score       tempo-model prior blend
                     (webapp)                   (Vision §3.3, unchanged seam)
```

Everything left of the profile is new or generalized; everything right of it is
the existing design consuming richer data.

### 2.2 The take store

Session slots (`data/processed/<session_id>/`) disappear from the product
model. The unit is the **take**, organized per piece and movement:

```text
data/takes/<piece_id>/<movement>/
  takes.jsonl                      # append-only manifest, one line per take
  <take_id>/
    take.mid                       # raw recorded MIDI, verbatim (notes, CC64, aftertouch)
    take.json                      # capture metadata (below)
    aligned.json                   # written by the pipeline (below); absent until aligned
```

`take_id` is `t<UTC timestamp>-<4 hex>`, e.g. `t20260709T193214Z-9f3a` —
sortable, collision-safe, never typed by a human.

`take.json` (written at Stop, before any analysis):

```json
{
  "take_id": "t20260709T193214Z-9f3a",
  "piece_id": "chopin_op11",
  "movement": 2,
  "recorded_at": "2026-07-09T19:32:14Z",
  "duration_seconds": 84.2,
  "note_on_count": 412,
  "input_name": "CLP-795GP USB",
  "cue": null,
  "status": "captured"
}
```

`status` lifecycle: `captured → aligning → aligned | ambiguous | unalignable`,
and orthogonally `folded | quarantined | discarded`. `cue` carries the existing
cued-recording metadata when the take came from the cue flow (which remains
available; a cue is just a strong localization hint).

DVC manages `data/takes/` exactly as it manages `data/recordings/` today.

### 2.3 The aligned-take artifact

`aligned.json` is the pipeline's full output for one take — the bridge between
raw MIDI and the profile fold, and the thing Reflection tooling inspects when
alignment looks wrong:

```json
{
  "take_id": "t20260709T193214Z-9f3a",
  "aligner": "seeded-local-v1",
  "localization": {
    "candidates": [
      { "start_beat": 96.0, "end_beat": 152.5, "score": 0.94 }
    ],
    "chosen": 0,
    "ambiguous": false
  },
  "span": { "start_beat": 96.0, "end_beat": 152.5 },
  "quality": {
    "match_rate": 0.91,
    "matched_notes": 375,
    "extra_notes": 22,
    "missing_notes": 31,
    "median_abs_residual_s": 0.038
  },
  "anchors": [
    { "beat": 96.0, "take_s": 0.41, "n_notes": 6 },
    { "beat": 97.0, "take_s": 1.29, "n_notes": 4 }
  ],
  "performance_model": "canonical-performance-v2",
  "profile_grid_step_ticks": 480,
  "base_seconds_per_quarter": 0.91,
  "cell_samples": [
    { "score_tick": 92160, "seconds_per_quarter": 0.88,
      "rubato_ratio": 0.967, "velocity": 52.0, "pedal": 0.7,
      "quality": 0.97 }
  ],
  "edge_trim_ticks": [960, 480]
}
```

- `anchors` are the phrase/beat-level timing anchors (the existing
  `PhraseTimingAnchor` concept, re-indexed from reference-seconds to
  **score beats** — see §2.6).
- `cell_samples` is the take resampled onto the canonical 960-PPQ profile grid:
  this take's vote for each covered cell. `seconds_per_quarter` is an explicit
  physical unit; `rubato_ratio` divides it by the take's robust baseline to
  separate local phrase shape from global pace. The fold consumes only these
  canonical samples; everything else is diagnostics.
- `edge_trim_beats`: the first/last beats of a take are unreliable (the soloist
  settling in, releasing mid-gesture). Trim one beat at the head, half at the
  tail by default; cells inside the trim get `quality` scaled down, not
  excluded. Overlapping takes make edge data cheap to replace. Coverage and
  profile learning deliberately differ here: coverage also retains the first
  and last matched note onsets as coarse votes, so an ending chord on the next
  measure's downbeat remains visible even when profile sampling trims that
  edge.

### 2.4 The alignment algorithm — recommendation and reasoning

Three candidate families were on the table:

| Method | Verdict | Why |
|---|---|---|
| **Chromagram DTW** (librosa-style) | **No.** | Audio-domain. We have symbolic MIDI with exact pitches; projecting to chroma discards octave and voicing information we get for free, adds an audio dependency the project has deliberately excluded ([Decision 0001](../decisions/0001-pivot-live-accompanist.md)), and is *less* accurate for MIDI-to-MIDI matching. |
| **Global DTW on note sequences** | **No, not alone.** | Global DTW assumes both sequences run start-to-end. Takes are partial and can start anywhere; global alignment of a 40-second take against a 10-minute movement produces pathological warping. Subsequence DTW fixes the localization problem but is O(take × movement) per take and still fragile around repeated material. |
| **Seeded local sequence alignment** (recommended) | **Yes.** | Two stages: fast exact localization by pitch n-gram seeding, then a banded local alignment (Smith–Waterman scoring, equivalently a gap-tolerant DTW restricted to the candidate window) for the fine warp. Handles 90% note accuracy by construction, is near-linear in practice, and is an incremental extension of `align_note_events` in `offline_alignment.py`. |

**Stage 1 — Localize (where in the score is this take?).** Build once per
bundle: an index from pitch n-grams (n = 4..6, chord-grouped, ordered by pitch
within a chord to defeat roll-order noise) to the score-beat positions where
they occur in the solo reference. For a new take, extract its n-grams, look
them up, and vote in a histogram over candidate start beats (BLAST-style
seeding). With ~90% note accuracy, 4-grams survive frequently enough that the
true region wins by a large margin in a few dozen notes. Output: one or more
candidate windows with scores.

*Repeated material is the real enemy here* — concertos restate themes. Two
defenses: (a) longer n-grams and inter-onset-interval binning as a tiebreaker
(the same notes played in the same rhythm at two score locations is rare over
20+ notes); (b) if the top two candidates score within 15% of each other, the
take is marked `ambiguous` and *not folded* — resolution is a one-tap choice in
the coverage view later (§4.3), or automatic once a neighboring unambiguous
take overlaps it.

**Stage 2 — Align (fine warp inside the window).** Within the chosen window
(± a few measures of slack), run local alignment between the take's
chord-grouped note events and the reference events: match reward for pitch
equality, mismatch/gap penalties tuned so ~10% wrong/extra/missing notes cost
little, with a Sakoe-Chiba-style band to keep it linear. This is the existing
pitch-sequence aligner with (a) chord grouping (~10 ms, per ACCompanion
convention), (b) gap tolerance, and (c) beat indexing. Output: matched pairs →
timing anchors → `PiecewiseLinearTimingMap`, all machinery that already exists.

**Stage 3 — Qualify.** Compute `match_rate` and residuals over the aligned
span. Gates:

- `match_rate ≥ 0.75` over the span → usable; below → `unalignable` (shown,
  never folded).
- Per-cell quality = local match density × residual consistency; cells below
  0.5 contribute to coverage display as "weak" but not to the profile fold.
- Gross-outlier guard from [Vision Open Question 8](../VISION_AND_UX_DESIGN.md):
  a take whose tempo curve deviates >3× spread over long spans is `quarantined`
  — kept, visible, not learning.

**Why not Matchmaker offline?** Matchmaker's OLTW is the *online* follower
(Decision 0002) and assumes roughly known position. Offline we can afford
global search and exact DP — better results, deterministic, dependency-free.
The two share nothing but the score bundle, and that is correct: offline
alignment is ground truth production; the online follower is the thing being
helped.

### 2.5 Expression fusion and the profile

Performance-profile v2 replaces the former parallel float-beat curves with one
explicit canonical cell record:

```json
{
  "schema_version": 2,
  "piece_id": "chopin_op11",
  "movement": 2,
  "coordinate_system": "canonical_score",
  "canonical_ppq": 960,
  "grid_step_ticks": 480,
  "take_count": 37,
  "updated": "2026-07-09T21:04:00Z",
  "base_seconds_per_quarter": 0.91,
  "cells": [
    { "score_tick": 92160,
      "seconds_per_quarter": 0.895,
      "seconds_per_quarter_mad": 0.031,
      "rubato_ratio": 0.984,
      "rubato_ratio_mad": 0.024,
      "velocity": 54.0, "velocity_mad": 6.5,
      "pedal": 0.62, "pedal_mad": 0.18,
      "support": 9, "mean_quality": 0.93 }
  ]
}
```

The one-time migration recomputes these cells from `take.mid` and each
canonical timing map. It does not rename or reinterpret old `period_s` values
in place, and runtime code has no fallback to the old profile contract.

Fusion rules (per grid cell, across takes):

- **Recency-weighted median** for the central value, **median absolute
  deviation** for spread — exactly the Vision §3.2 statistics. Median fusion is
  what makes overlapping imperfect takes *desirable*: each new take is another
  robust vote, and a wrong note in one take is outvoted, not averaged in.
- **Recency half-life of eight takes per cell**, not eight takes globally —
  a cell rehearsed 20 times last month and once yesterday should weight
  yesterday heavily; a cell untouched since last month keeps its old statistics
  at full weight (stale is better than empty, and spread already encodes doubt).
- **Quality-weighted:** each `cell_sample` carries its per-cell quality; votes
  are weighted by it. Edge-trimmed and low-match-density cells whisper rather
  than shout.
- **Pedal:** CC64 resampled to per-cell median depth (0–1). Captured from day
  one (it's in the raw MIDI regardless). The live
  runtime ignores it until the renderer learns to use it — the profile is the
  extension point, per Vision §3.4.
- **Unfold:** discarding a take replays the fold log without it. Keep the fold
  incremental in the common path, but store `cell_samples` per take so a full
  rebuild (`rubato profile rebuild`) is always available and canonical.
  Incremental state that cannot be rebuilt from takes is forbidden.

### 2.6 Coverage index

Coverage is **derived, never stored independently** — computed from the profile
(`n`, spread) plus take manifests, cached as
`data/profiles/<piece_id>/<movement>/coverage.json` for the UI:

```json
{
  "piece_id": "chopin_op11",
  "movement": 2,
  "computed_at": "2026-07-09T21:04:05Z",
  "n_target": 3,
  "measures": [
    { "measure": 24, "start_beat": 92.0, "end_beat": 96.0,
      "solo": true, "min_n": 4, "mean_quality": 0.93, "state": "covered" },
    { "measure": 25, "start_beat": 96.0, "end_beat": 100.0,
      "solo": true, "min_n": 1, "mean_quality": 0.88, "state": "touched" },
    { "measure": 26, "start_beat": 100.0, "end_beat": 104.0,
      "solo": false, "state": "tutti" }
  ],
  "summary": { "solo_measures": 214, "covered": 121, "touched": 38,
               "uncovered": 55, "percent_covered": 56.5 }
}
```

- A measure's state is the **minimum** over its solo-part grid cells:
  `covered` = every cell has `n ≥ n_target` (default 3) at quality ≥ 0.5;
  `touched` = every cell has `n ≥ 1`; `uncovered` otherwise; `tutti` = no solo
  events in the measure (excluded from percentages — coverage is of the
  *piano part*).
- Why `n_target = 3`: one take establishes existence, the median needs three
  votes to reject an outlier. Configurable per movement; the Vision doc's
  "orchestra knows you" wording thresholds can key off the same number.
- The performer-facing evidence summary uses
  `support × mean alignment quality × exp(-typical relative MAD / 0.12)`, where
  support saturates at three kept aligned passes. It is an interpretable
  progress signal, not a probability of musical correctness. Report shared
  cell count and high-variance measure/beat locations alongside it so a single
  percentage never hides why confidence rose or fell.
- **Requires the beat→measure map.** For MusicXML-derived bundles this is free
  (movement 1 has `score.mxl`). For the Oguri movement-2 MIDI it is
  [Vision Open Question 4](../VISION_AND_UX_DESIGN.md) — unresolved, and now on
  the critical path (§6, item 2). Until it exists, coverage rolls up to
  *rehearsal-letter regions* from `sections.json` instead of measures. The
  contract above is measure-based; the fallback uses the same shape with
  region ids.

### 2.7 The background worker

Alignment must never make the soloist wait. A single in-process worker (same pattern
as `LiveControl`: one thread, a queue, status inspectable over the API)
processes takes strictly after Stop:

```text
POST /api/takes/record/start        → LiveControl.start_record (existing path)
POST /api/takes/record/stop         → take.json written, enqueue align job
GET  /api/takes?movement=2          → manifest + statuses
GET  /api/coverage/2                → cached coverage.json (read-only)
POST /api/takes/{id}/discard        → unfold + status
POST /api/takes/{id}/resolve        → choose localization candidate (ambiguous takes)
GET  /api/profile/2                 → profile.json
WS   /api/events                    → take status transitions, coverage updates
```

Target: a 90-second take localizes, aligns, folds, and updates coverage in
**< 5 seconds** on the Mac — comfortably inside the gap between takes. If the
pipeline ever exceeds the gap, jobs queue; recording is never blocked by
analysis, by contract.

---

## 3. UX / UI Design

All four surfaces obey the established laws: readable at 2 m, one primary
action per face, Silence top-right always, spacebar = primary action, Escape =
Silence, stage-black/ivory/brass/ember palette, no text entry at the piano
([Vision §6–7](../VISION_AND_UX_DESIGN.md)). Coverage introduces one new color
duty: **`--ready` green is the coverage color** (it already means "confirmed
good"); uncovered regions are dim ivory hatching, never red — missing coverage
is a to-do, not an error.

### 3.1 Take capture cockpit (the Record face variant)

Recording many takes is the whole job of Phase 1, so it gets the most brutal
minimalism in the product. Entered from the Ready face via **Rehearse** — free
recording *is* rehearsal mode now; the cued flow remains an option inside it.

The score-first implementation keeps the engraved score page visible beside
this minimal control surface on desktop. The full-frame sketch below describes
the control hierarchy, not permission to hide the score. During an orchestral
cue, recording, or review, the performer must simultaneously see the music and
the score-position cursor.

```text
┌──────────────────────────────────────────────────────────────────┐
│ ● Recording                                          [ ⏻ Silence ]│
│                                                                  │
│                                                                  │
│                        ● 0:47                                    │
│                                                                  │
│              ▁▂▄▆▄▃▅▇▅▂▁▃▄▂▁  (last few seconds of notes)        │
│                                                                  │
│                                                                  │
│                     ⏹  Stop (space)                              │
│                                                                  │
│  3 passages today · movement 56% covered                         │
└──────────────────────────────────────────────────────────────────┘
```

- **Idle → armed is automatic.** On entering Rehearse, the system is already
  listening; the first note-on *starts the take* (space also starts it
  explicitly, for silence-first passages — rare but real). Stop is space or
  the button. This kills the "walk to laptop, click record, walk back, play"
  loop: hands on keys is the workflow.
- The activity strip is a **note-density sparkline of the last ~10 seconds**,
  not a waveform (there is no audio). Its only job is "yes, I am hearing you"
  — peripheral-vision reassurance, ember-tinted while recording.
- The elapsed counter is the largest element (tabular numerals, Fraunces).
- After Stop: a **three-second toast**, not a modal —
  `Recording · 0:47 · placing it in the score…` which resolves in place to
  `From m. 24 · covers mm. 24–41 · kept ✓` when the pipeline finishes (typically before
  the next take starts). The full After face with keep/discard/play-back
  remains available but is *not* forced between takes; space immediately
  arms the next take. Machine-gun take cycles are the design target:
  **non-playing time between takes < 3 seconds.**
- If alignment lands `ambiguous` or `unalignable`, the toast says so in stage-
  manager voice (`Couldn't place this recording — saved for later`) and the
  coverage face collects it. Never interrupt the flow to ask questions.
- The score header is the only persistent telemetry: takes today and movement
  **observed** percent. It updates as folds land — watching that number climb
  *is* the progress bar of Phase 1. Observed means at least one kept take;
  maturity coverage still uses `n_target` (three by default), so a first take
  is amber/touched rather than green/covered.

### 3.2 Coverage face — the score with the overlay

The key new surface. Reachable from the Ready face (`Coverage` sits beside the
Library drawer) and shown on a loop between takes if the soloist leaves it open on a
second display/iPad — it is designed to be *ambient*.

```text
┌──────────────────────────────────────────────────────────────────────┐
│ RUBATO · Coverage                                       [ ⏻ Silence ]│
│                                                                      │
│   II. Larghetto                                  56% covered          │
│   ████████████░░░░▓▓▓▓████░░░░░░▓▓████████░░░  measure strip          │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │                                                                │  │
│  │      [ PDF page, pdf.js canvas ]                               │  │
│  │      ── green wash over covered measures ──                    │  │
│  │      ── dim hatch over uncovered solo measures ──              │  │
│  │      ── neutral over tutti ──                                  │  │
│  │                                                                │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                     ◀  page 4 / 11  ▶                                 │
│                                                                      │
│  needs takes: mm. 25–31 · mm. 58–66 · mm. 102–104                     │
│  1 take needs placing ▸                                               │
└──────────────────────────────────────────────────────────────────────┘
```

**Rendering architecture: pdf.js canvas + one SVG overlay element.** The PDF
page renders to a canvas; an absolutely-positioned SVG on top draws coverage
geometry in page coordinates scaled with the canvas. All overlay data comes
from `coverage.json` joined with the measure-geometry map (§3.3). No PDF
mutation, no server-side rasterization; the overlay re-renders on every
WebSocket coverage update, so a fold landing mid-glance visibly turns a
measure green — a small, correct piece of theater worth having.

Three **fidelity levels**, shipped in order:

- **Level 0 — measure strip (no PDF).** The horizontal strip under the title:
  one cell per measure (or per section region pre-measure-map), green/dim-
  green/hatched/neutral. This alone delivers "see at a glance what's left" and
  ships with zero geometry work. It persists forever as the always-visible
  summary and the navigation scrubber for the PDF below.
- **Level 1 — system bands on the PDF.** Geometry map contains, per page, the
  vertical extents of each *system* and the measure-range each system spans.
  Overlay = full-width horizontal bands per system, subdivided proportionally
  by measure count. Bands are coarse but honest, and system extents are an
  order of magnitude easier to produce than measure boxes.
- **Level 2 — measure boxes.** True per-measure bounding boxes; the wash hugs
  the music. Same overlay code, finer geometry.

**Interaction (deliberately almost none at the piano):**

- Tap/click an uncovered region → the Ready/Rehearse context is set to that
  spot; if the bundle supports it, offer the cued-record flow starting a few
  measures earlier. This is the *only* action that matters: see gap → record gap.
- `needs takes` list: the top uncovered spans in reading order, as plain text.
- `N takes need placing ▸`: the ambiguous-take queue. Opens a card per take:
  the two candidate spans highlighted on the strip, `It was here / It was
  there / Discard` — one tap each. This is glanceable enough for the piano but
  equally fine from the couch in Reflection.
- Percentage indicator: solo-measure coverage percent, Fraunces, top right.
  The performer-facing number is percent observed; mature percent covered is
  retained as quieter detail. This prevents a successful first take from
  leaving the visible progress at 0% while preserving the repeated-take target.
- The latest aligned take paints a labeled span independently of cumulative
  coverage. It answers “where was that take?”; coverage answers “what is in the
  bank across takes?” These layers must never share state or be inferred from
  one another. Adjacent measures on one staff system form one continuous dashed
  outline rather than a grid of small dashed boxes.
- A score-position cursor is shown whenever a score-time transport is active.
  Cue and hardware review use canonical transport anchors plus 100 ms
  wall-clock interpolation. Every recognized beat has an offline source-MIDI
  anchor; interpolation is bounded to the current beat, and Audiveris beat-x
  knots place the cursor in the engraving. A 750 ms lookahead preturns pages.
  Live mode uses follower positions. Mapping review state and confidence remain
  explicit even though the coordinate is canonical.
- Measure numbers are printed as unobtrusive `m. N` labels above every overlay
  box. The selected passage has a `Next take` badge on the score; the moving
  cursor carries `Now`; and the latest span carries `Newest recording`. A compact
  score key spells out amber/learning (the weakest half-beat is below the
  three-observation or 50%-quality target), green/every half-beat meeting both, gray
  hatch/no aligned take, dashed/newest recording, and red/playing now. A
  selected measure names the failing threshold in prose and tells the performer
  whether another pass is recommended. Right-click opens passage-local record
  and review actions. If the system
  suggests a next passage, its page opens automatically so the target card and
  score can never refer to different locations.

Implemented movement-2 targeting combines two independent symbolic artifacts:
Audiveris GRID barlines provide 126 exact PDF boxes across 15 pages, while the
declared Oguri solo-reference MIDI projected through the same machine mapping
marks piano-active measures. The latter supplies honest provisional
solo/tutti scope and uncovered targets without promoting expressive MIDI beats
to printed measures.

### 3.3 The measure-geometry map (how the overlay knows where measures are)

New per-bundle artifact, `measure_boxes.json`:

```json
{
  "pdf": "source/score.pdf",
  "pages": [
    { "page": 4,
      "systems": [
        { "y0": 0.118, "y1": 0.236, "measures": [
          { "measure": 24, "x0": 0.071, "x1": 0.204 },
          { "measure": 25, "x0": 0.204, "x1": 0.371 }
        ]}
      ]}
  ]
}
```

Coordinates are page-normalized (0–1) so they survive any render scale. Level 1
needs only `systems[].y0/y1` plus first/last measure numbers per system;
Level 2 fills in the x-splits. Movement 2 now ships Level 2 from Audiveris GRID
barline clusters; rebuilding the map from the `.omr` source reproduces the
committed JSON exactly.

**How it gets built — offline evidence with sparse fallback:**

1. **Audiveris build artifacts (normal path).** Retain `.omr`, exported
   MusicXML, measure rectangles, and note x positions. Validate page and
   measure scope, and preserve recognition gaps explicitly.
2. **Agent-assisted anchoring tool (fallback).** A dev-mode overlay
   page: the PDF with a click-to-mark flow — click the barline positions of
   system boundaries, type nothing (measure numbers count up automatically from
   the previous anchor; systems inherit). A full concerto movement is 15–40
   systems: **under an hour of human-or-agent clicking, once per piece.** This
   is boring, deterministic, and produces exactly correct geometry. Build this.
3. **MusicXML cross-check.** Where a bundle has MusicXML,
   measure *counts and numbering* come from it (music21 or direct parse),
   so the anchor tool validates that clicked systems sum to the right measure
   total per page. Catches off-by-one drift immediately.

Movement II uses the normal path for measures 1–125 after the repaired Audiveris
export and independently cross-validated beat-map rebuild. The B-natural pickups
in m.12 and m.53 are explicit piece annotations; they do not become a reusable
pitch heuristic. Measure 126 remains explicitly unmapped.

### 3.4 Live face — the prior made visible

The Live face ([Vision §6.3](../VISION_AND_UX_DESIGN.md)) is already correct;
this design adds exactly one element to the **rehearsal variant** and nothing
to the performance variant:

- The **journey bar gains coverage tinting** — the same green/dim/hatched
  vocabulary at 2 px height. In rehearsal, glancing down answers "am I about to
  cross into unrehearsed territory?", which is precisely when the orchestra
  gets more cautious (the blend leans reactive where `n` is small — Vision
  §3.3). The bar's brass playhead is unchanged.
- The state word's brightness already encodes confidence; no new indicator.
  When the tempo model is riding a strong prior, that manifests as *behavior*
  (conviction through drops in live confidence), not as UI.
- Performance variant stays as designed: state word, measure number, journey
  bar (uncolored), Silence. A performer gets no coverage telemetry — Phase 1
  is over.

### 3.5 Project and recording management

- **Hierarchy: piece → movement → score location → recordings.** The Library drawer on the Ready
  face lists pieces; picking a piece shows its movements with their coverage
  percents (the measure strip miniaturized to a spark-strip per movement).
  Movements are the working unit, matching the profile scope decision
  (Vision Open Question 7). Within a movement, the score is the organizer:
  recordings are grouped by the performer-selected entry measure, falling
  back to the aligned start only for legacy or free recordings.
- **Recordings have no global user-facing sequence number.** A database ID or
  chronological ordinal has no musical meaning. The library reads **From
  measure 24 · 3 aligned passes · covers mm. 24–41**, followed by the local
  passes with timestamps, spans, and statuses. Failed placement attempts stay
  in the same score-location group with an explicit needs-attention label, but
  do not increase its aligned-pass count. Filters: needs placing / quarantined
  / excluded. Bulk operations live in Reflection only.
- **The PDF answers “what do I have here?”** Measure labels expose the number
  of kept aligned passes crossing each measure. Clicking either a measure or a
  passage group's **Show on score** action selects the same score location and
  opens its contextual review. This keeps location primary and recordings
  secondary.
- **Session slots survive only as API plumbing** until fully migrated (§5);
  the visible vocabulary is pieces and takes, per Vision §6.5.
- **Retention:** takes are never auto-deleted; DVC manages the store; a
  discarded take keeps its files (status flag only). Storage is trivial
  (MIDI ≈ 20 kB/take).

---

## 4. Technical Feasibility Assessment

**Overall verdict: achievable, with two genuinely hard parts — neither of them
is the alignment.**

### 4.1 The easy parts (days each, mostly existing machinery)

| Piece | Basis | Confidence |
|---|---|---|
| Uncued take capture | `LiveControl.start_record` + `record_midi` exist and work; add first-note-on auto-start and take registration | Very high |
| Fine alignment | `align_note_events` + `PiecewiseLinearTimingMap` + `PhraseTimingAnchor` exist; add chord grouping, gap tolerance, beat indexing | High — this codebase has already aligned a real 881-note take ([Yamaha Take Analysis](../concepts/yamaha-take-analysis.md)) |
| Profile fold statistics | Specified to the formula in Vision §3; pure NumPy, order statistics, deterministic tests | Very high |
| Coverage computation | Arithmetic over the profile grid + measure map | Very high |
| PDF display | pdf.js is mature, renders in Svelte trivially; SVG overlay is standard | Very high |
| Measure strip (Level 0) | A row of divs | Very high |

### 4.2 The hard parts

1. **Localization against repeated material.** The honest risk in Phase 2.
   Chopin restates themes with identical pitch content; a 15-second take of a
   recurring figure may be genuinely ambiguous. Mitigations are designed in
   (§2.4): rhythm-profile tiebreaking, the `ambiguous` status with one-tap
   resolution, cue metadata as a hint, and neighbor-take propagation. Expected
   steady state: >90% of takes place automatically, the rest are two taps each.
   **Validate first** (§6 item 1) — the n-gram index over the movement-2
   reference plus the existing recorded take answers this in a day.
2. **Measure geometry for the PDF overlay.** Not algorithmically hard —
   *tediously* hard, which is why the design refuses to depend on OMR and
   budgets a one-hour-per-piece anchoring tool (§3.3). The staged fidelity
   levels mean the product never blocks on it.
3. **Beat→measure map for MIDI-only bundles** (Vision Open Question 4,
   inherited). The Oguri file's expressive timing means tick-derived barlines
   are wrong. The alignment-against-metronomic-reference approach recommended
   there is now *required* Phase-2 work, not optional polish, because coverage
   speaks in measures. Movement 1 (has MusicXML + PDF) dodges this entirely —
   an argument for making movement 1 the coverage showcase even though
   movement 2 is the live-following testbed.
4. **Live prior integration** is already designed (Vision §3.3, roadmap items
   5–6) and is *not* re-scoped here; this design's obligation to Phase 3 is
   only to fill the profile the tempo model reads and to keep `n`/spread
   honest per cell so the blend weights mean something.

### 4.3 Libraries

- **mido** (in use) — MIDI I/O, capture, CC64. Sufficient.
- **pretty_midi** — convenient note/CC extraction and tempo handling for the
  pipeline; optional, mido covers it.
- **music21** — MusicXML parsing for measure maps and reference validation
  (offline/dev only; keep it out of the `live` extra — it is heavy).
- **NumPy** — the DP matrix for local alignment and all fold statistics. A
  hand-rolled banded Smith–Waterman over int8 pitch arrays is ~100 lines and
  microseconds-fast at these sizes; **do not** add `fastdtw`/`dtw-python` —
  they solve the unconstrained problem we specifically avoid, and the DP core
  is the part worth owning and testing.
- **pdf.js** — score rendering in the webapp. Chosen over pdfium/server-side
  rasterization: no native deps, standard Svelte integration, and we need
  page canvases + coordinates, nothing exotic.
- **Audiveris** (optional, dev-tool only) — OMR pre-population of the anchor
  tool. Never a runtime dependency.
- **librosa / midiutil — not used.** No audio; mido already writes MIDI.

### 4.4 Performance envelope

Movement-scale numbers: a movement is ~10⁴ reference notes; a take is ~10³.
The n-gram index is a dict built in milliseconds; banded local alignment over a
few-hundred-beat window is < 10⁷ cell updates — well under a second in NumPy.
Fold and coverage recompute are trivial. The < 5 s pipeline budget (§2.7) has
an order of magnitude of headroom, which is exactly how much headroom a
background pipeline should have.

---

## 5. Relationship to the Current Codebase

### Preserve (and build on)

| Asset | Role in this design |
|---|---|
| `accompaniment/offline_alignment.py` | The fine-alignment core. Extend with chord grouping, gap-tolerant scoring, beat indexing, and the localization stage. Its dataclass/artifact discipline is the pattern `aligned.json` follows. |
| `server/live_control.py` | Stays the single hardware-job manager verbatim. `start_record` is the take-capture path; the cue flow remains as the optional cued variant. Gains only: take registration on completion and a completion hook that enqueues the align job. |
| `accompaniment/live_midi.py`, `midi_ports.py` | Untouched capture/playback plumbing. |
| Score bundle contract | Unchanged; gains two optional artifacts per bundle: `ngram_index.json` (derived, rebuildable) and `measure_boxes.json` (§3.3). Coordinate the schema addition per [DEVELOPMENT.md](../DEVELOPMENT.md) high-conflict rules. |
| Scheduler / tempo model / follower seams | Untouched. The profile is the only interface Phase 3 consumes, by design. |
| Webapp aesthetic system (`app.css`, masthead, Silence, keyboard model) | Carries forward whole. The Coverage face uses the existing palette with `--ready` promoted to coverage duty. |
| Trace/run discipline, DVC layout | Extended to `data/takes/` and `data/profiles/`, same philosophy. |

### Redesign / retire

| Current | Disposition |
|---|---|
| Session-slot model (`/api/sessions`, `data/processed/<session_id>/`) | Retired from the product model; kept as internal plumbing during migration, deleted when the take store is authoritative. One take ≠ one session with named uploads — that friction is what Phase 1 exists to remove. |
| `App.svelte` three-deck cockpit | Restructured into the faces (already the Vision §6.5 plan). The Take deck's record controls become the capture face (§3.1); Preview and Library become the Ready face drawer; upload/variant management moves to Reflection. Evolve, don't rewrite (Vision Open Question 12 stands). |
| `render-offline` per-session endpoint | Its alignment internals move into the background pipeline; the render-and-play-back capability itself survives as the After-face *Play it back* (First Contact stage is unchanged). |
| Fixed default `movement=2` wiring in routes/UI | Replaced by piece/movement context from the take store. |

### In-progress Codex work: `rehearsal_plan.py`, `live_control.py`

- `live_control.py` (on `main`, read above): **no conflict** — this design
  treats it as finished infrastructure and adds hooks only. Any in-flight
  changes should preserve the one-job-at-a-time invariant; the align worker is
  deliberately a *separate* queue so analysis never contends with the MIDI
  hardware lock.
- `rehearsal_plan.py` (not on `main`; in-progress on a Codex branch):
  presumably the "what to rehearse next" logic. **This document owns the
  contracts it should target:** a rehearsal plan is a *consumer of
  `coverage.json`* (§2.6) — its job is ordering the uncovered/weak spans into
  suggestions (the `needs takes` list, cue points for jump-in recording), not
  maintaining its own record of what has been played. If the branch currently
  derives coverage from session artifacts, rebase that logic onto the take
  manifest + coverage index before merging; otherwise there will be two
  disagreeing answers to "what's left." Coordinate via the usual worktree/PR
  flow ([DEVELOPMENT.md](../DEVELOPMENT.md)).

---

## 6. Implementation Roadmap

Ordered; each item is independently pickable, has a crisp done-when, and
delivers value alone. Items 1–5 are the Phase-1/2 spine; 6–9 are the coverage
UI; 10–11 close the loop with Phase 3. This slots between items 4 and 6 of the
[Vision roadmap](../VISION_AND_UX_DESIGN.md) — it *is* the expanded version of
Vision item 5 ("Interpretation profile v1 — write path").

1. **Localization spike (de-risk first).** Build the pitch n-gram index over
   the movement-2 solo reference; localize (a) the existing
   `movement2_take` fixture, (b) synthetic partial takes cut from the
   reference with 10% injected note errors, at various lengths and positions.
   *Done when:* ≥90% of ≥20-note synthetic takes localize uniquely and
   correctly, and the ambiguity detector flags the genuinely repeated spans
   rather than mis-placing them.

2. **Take store + capture path.** `data/takes/` layout, `take.json`,
   manifest; `POST /api/takes/record/{start,stop}` wrapping `LiveControl`;
   first-note-on auto-start; registration on stop. No UI change yet beyond
   pointing Record at the new endpoint. *Done when:* ten rapid-fire takes from
   the Yamaha land as ten take dirs with correct metadata and zero naming or
   slot interaction.

3. **Alignment pipeline v1.** Localize → banded local alignment (chord-grouped,
   gap-tolerant, beat-indexed) → `aligned.json` with anchors, cell samples,
   quality gates, edge trim; background worker + status transitions + WS
   events. *Done when:* the pipeline processes a real take in <5 s, the
   fixture take's aligned span matches the known ground truth, and
   `ambiguous`/`unalignable` paths have deterministic tests.

4. **Profile fold v1 + coverage index.** Quality-weighted recency-median fold
   of cell samples into `profile.json` (tempo, dynamics; pedal captured but
   parked); unfold; quarantine guard; `coverage.json` computation;
   `rubato profile show/fold/unfold/rebuild` and `rubato coverage` CLI.
   *Done when:* three overlapping real takes produce a profile whose covered
   cells match the union of aligned spans, fold/unfold round-trips, and
   rebuild-from-takes equals incremental state exactly.

5. **Beat→measure map for movement 2** (Vision Open Question 4, now
   critical-path). Align the Oguri solo track to a metronomic reference or
   hand-anchor downbeats; emit the measure map into the bundle; movement 1
   gets its map from MusicXML for free. *Done when:* spot-checked measure
   numbers match the PDF at ≥10 locations per movement.

6. **Coverage Level 0 — measure strip + capture face.** The §3.1 capture
   cockpit (auto-arm, sparkline, toast lifecycle, footer coverage percent) and
   the Level-0 strip on a Coverage view fed by WS updates. *Done when:* a
   rehearsal session is: sit down, play, stop, glance, play — and the strip
   visibly fills as takes land, with non-playing time between takes under 3 s.

7. **Measure-geometry anchor tool + Level 1 PDF overlay.** Dev-mode anchoring
   page producing `measure_boxes.json` (systems first); pdf.js + SVG overlay
   with system bands; strip becomes the page scrubber. Start with movement 1's
   PDF. *Done when:* anchoring a full movement takes under an hour and the
   overlay's green regions agree with the strip everywhere.

8. **Coverage interactions.** Tap-region → cued-record-from-there; `needs
   takes` list; ambiguous-take resolution cards. *Done when:* the soloist can go from
   "gray gap on page 6" to recording that passage in two presses, and an
   ambiguous take is resolvable in one.

9. **Level 2 measure boxes** (fidelity polish). X-splits in the anchor tool;
   overlay hugs measures. *Done when:* per-measure highlighting matches the
   engraving at spot checks. (Deliberately after 8 — interaction value beats
   geometric precision.)

10. **Prior-blended tempo model on the fused profile** (= Vision roadmap
    item 6, unblocked by 4). Per-cell support/spread drive blend weight; evaluate
    on simulated-online replays of held-out takes: onset error with prior vs.
    without. *Done when:* the prior measurably beats purely-reactive on
    covered regions and degrades to reactive (never worse) on uncovered ones —
    the quantitative proof of the whole Phase-1→3 thesis.

11. **Pedal renderer hookup** (v1.1). Canonical cells already fold CC64;
    renderer consumes it in `LEAD` sections first. *Done when:* A/B renders
    with and without learned pedaling are distinguishable and the soloist prefers
    with.

Dependency spine: **1 → 3 → 4 → {6, 10}**; **5 → {6 rollup-by-measure, 7}**;
**2** is independent and immediate; **7 → 8 → 9**. If the localization spike
(1) surprises us badly, everything else still stands — takes fall back to
cue-hinted recording (the existing flow) for localization while the aligner
improves, and no interface changes.

---

## Related

- [Vision and UX Design](../VISION_AND_UX_DESIGN.md) — the three faces, core loop, interpretation profile, aesthetic law
- [System Design](../SYSTEM_DESIGN.md) — architecture and data contracts this extends
- [Offline Alignment And Render](../concepts/offline-alignment-render.md) — the existing aligner this generalizes
- [Yamaha Take Analysis](../concepts/yamaha-take-analysis.md) — the real-take evidence behind the quality gates
- [Score Bundle Contract](../concepts/score-bundle-contract.md) — bundle artifacts extended here
- [Section Policy](../concepts/section-policy.md) — solo/tutti knowledge reused for coverage's tutti exclusion
- [PWA Rehearsal UI](../concepts/pwa-rehearsal-ui.md) — current cockpit being restructured
- [Decision 0002](../decisions/0002-tracker-and-synth-mvp.md) — why online following stays Matchmaker while offline alignment stays ours
