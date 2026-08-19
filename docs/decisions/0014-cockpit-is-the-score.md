# Decision 0014 — The cockpit is the score, and the beat clock is visible

Status: **Accepted** (2026-07-29)

## Context

Eric, after a take he could not debug:

> "The UX is so bad it turned the page but the system was off of the viewport, I
> had no ability to see where the cursor was... the more elements there are
> competing for my attention the worse the quality is, so measure by simplicity."

And, on validating the system by ear:

> "The source of truth is the internal beat clock. I need a way to check on it
> and help validate it with my listening."

The rehearsal surface had grown into a two-column cockpit: a hero title block,
three intent cards, a side column of decks, and four standing paragraphs of
explanation above the engraving. The score — the only thing a performer reads
while playing — was squeezed to roughly 60% of the window and rendered at a
fixed 760px, and the panel it shared the row with was what the page scrolled to
whenever a control was pressed.

## Decision

### 1. The score owns the screen

`main.hall` is a single column with the score first and full width. The page
renders to fill its host rather than a fixed 760px cap.

Everything else — takes, coverage, alignment, device setup, the intent cards —
moves into an **inspection panel that starts collapsed**, toggled from the
masthead. It is a deliberate data-grooming modality, not something in the way of
playing. Most sessions never open it.

### 2. The beat clock is always visible, and it is a literal read of internal state

The masthead carries the transport instruments: state word (`REC`, `LEAD-IN`,
`FOLLOWING`, `PLAYING`, `IDLE`), **measure and beat**, elapsed time while
recording, and a `projected` flag. Fixed position, tabular figures, legible from
the piano bench.

This exists so the performer can arbitrate disagreements between what they hear,
what the cursor shows, and what the system believes — by ear, in real time,
without an agent reconstructing it from logs afterwards.

Two rules make it trustworthy:

- **Beats are floored, never rounded.** `beat_in_measure` runs `[0, 4)` in 4/4,
  so 3.999 just before a barline used to render as "beat 5.0" for one frame — a
  beat that cannot exist. Eric spotted this independently.
- **Dead reckoning is labelled.** When the shown position comes from the
  transport projection rather than the follower, the readout says `projected`,
  so a drifting cursor is never mistaken for a tracking one.

### 3. The follower outranks the transport projection

The cursor previously preferred `activeHardwareScorePosition`: an **open-loop**
projection of wall clock since the take started times a fixed `tempo_scale`,
which never looks at what the performer played. Any rubato accumulated as
visible drift — the "cursor slowly drifted faster ahead" reports.

Authority now depends on what is making sound:

| Hardware state | Authority | Why |
| --- | --- | --- |
| `playback` (review, orchestra-only) | transport | no performer to follow; a stale follower must not freeze the cursor |
| `record`, `record_with_cue` | follower | the only position derived from the performer |
| in-app review player | preview | an explicit "play me this take" |

### 4. Whatever sounds, the score comes back

Two scroll behaviours, both previously gated wrongly:

- The active system is scrolled into view **in every mode**, not only the
  performance view. The page already auto-turned in all modes, so gating the
  scroll meant the page flipped and left the cursor below the fold. The
  rehearsal view scrolls the *window* (its wrap is `overflow: hidden`), the
  performance view scrolls the wrap.
- **Any transition into a sounding state scrolls the score into view.** The
  controls that start a take live in the panel below, so pressing one otherwise
  begins the take with score, cursor and readout all off screen.

## Consequences

- The score-and-controls "one simultaneous workstation" contract is retired.
  Operating a panel control scrolls to the panel; that detour is deliberate now,
  and the app pulls the score back when anything sounds.
- Prose above the score is gone: the kicker, the "Rehearsal score" title (kept
  for assistive tech only), the reduction description, the estimate caveat
  (now a tooltip) and the idle "current location will appear" placeholder
  (redundant with the masthead).
- `data-score-beat` on `score-position-cursor` remains the machine-readable
  cursor contract even when nothing is drawn, so instrumentation and tests keep
  a stable hook.

## Links

- [Decision 0013](0013-rehearsal-converges-on-performance.md) (rehearsal converges)
- [Decision 0012](0012-performance-readiness.md) (readiness, not coverage)
- Issue #153 (collapse the intent cards into one play/stop action)
- Issue #145 (decompose `App.svelte`)
