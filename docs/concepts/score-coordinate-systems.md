# Score coordinate systems

Rubato mixes several coordinate frames. Confusing them has caused real bugs
(a follower reported "3.91 beats off" across a whole piece; the score cursor
"bounced" for a minute). This page is the single reference for what every
coordinate means, its origin, and its units. Convert explicitly at boundaries.

There are two independent families: **timing** (where are we in the music) and
**geometry** (where is that on the page). They never mix.

## Timing frames (beats and ticks)

Owned by [`score_coordinates.py`](../../src/aimusic/accompaniment/score_coordinates.py);
see its module docstring and tests for the conversions.

| frame | origin | unit | notes |
|---|---|---|---|
| **canonical** | m.1 beat 1 = beat 0.0 | quarter-note beats; ticks at PPQ 960 | Rubato's runtime coordinate. In 4/4 measure N starts at `4·(N-1)`. Everything the follower, tempo model, scheduler, sections and display map speak. |
| **partitura** | first *full* measure | beats | partitura / Parangonar / Matchmaker. A pickup places measure 1 at a **negative** beat. Never compare to canonical without `BeatOrigin`. |
| **reference** | a reference performance's own MIDI t=0 | beats/ticks at that file's PPQ | the Oguri timeline. Unrelated to the two above. |

Mixing canonical and partitura silently yields an offset of exactly one measure.

## Geometry frames (pixels on the page)

Two frames, from **two different Audiveris passes**, composed to place the
cursor. This is the part that is easy to get wrong.

### 1. Measure boxes — page-normalized

Source: the Audiveris **grid pass** (`.grid.omr`), which detects barlines and
staff lines. Parsed by
[`omr_layout.py`](../../src/aimusic/accompaniment/omr_layout.py).

- **Frame:** the whole page.
- **Origin:** page top-left.
- **Units:** a **fraction of the page image in [0, 1]** — `x0/x1` are barline
  pixels divided by the sheet pixel *width*, `y0/y1` staff pixels divided by the
  sheet pixel *height* at extraction time. Stored values are therefore
  resolution-independent, **not raw scan pixels**.
- Carried through `display_layout.machine.json` → `display_map.machine.json`
  (`coordinate_system: "normalized_pdf_page"`) → the `measure_boxes` API
  `x0/x1`.

### 2. Note positions — measure-local

Source: the Audiveris **note pass** (the recognition `.mxl`), a *different* pass
whose MusicXML gives each note head a `default-x`. Parsed into `RecognizedNote`
in [`score_fusion.py`](../../src/aimusic/accompaniment/score_fusion.py).

- **Frame:** a single measure.
- **Origin:** that measure's left barline.
- **Units:** MusicXML **tenths** (staff-space units, ~10 per interline gap).
  `default_x` lies in `[0, measure_width]`; the ratio `default_x / measure_width`
  is a **unit-free fraction across the measure**.
- `onset_beats` on the same note is rhythmic (from durations), not geometric.

### Composing them → beat pixel positions

There is no "beat" glyph in an engraving. A beat's x is inferred from the notes
that sound on it: group note heads by `onset_beats`, take the median
`default_x`, and project that measure-local fraction into the measure box's
page-normalized span. In [`beat_pdf_x`](../../src/aimusic/accompaniment/score_fusion.py):

```
fraction = note.default_x / note.measure_width        # [0,1] within the measure
pdf_x    = box_x0 + fraction * (box_x1 - box_x0)       # [0,1] across the page
```

The result (`PerformanceBeatAnchor.pdf_x`, and the `measure_boxes` API beat `x`)
is **page-normalized [0, 1]**, the same frame as the box, so a well-formed beat
satisfies `x0 <= x <= x1`. Beats with no note are interpolated between those
that have one. This is **display-only**; timing uses `score_tick` /
`source_midi_tick`, which the display rebuild never touches.

[`beat_geometry.py`](../../src/aimusic/accompaniment/beat_geometry.py) owns this,
and does more than "median x of the notes at an onset":

- **Best staff.** Every `(part, staff)` is scored for how cleanly its onsets
  sample the beat grid; the clearest wins (a plain left hand beats a right-hand
  run). The chosen staff is recorded on the anchor (`pdf_x_staff`) and surfaced.
- **One anchor per beat**, so a run's notes are not knots that crowd the markers.
- **Confidence** in [0, 1] (`pdf_x_confidence`), and a per-beat `pdf_x_anchored`
  flag (real note vs interpolated), both surfaced through the API.
- **Sparse/edge cases.** A measure with no usable notes falls back to beats
  evenly spaced with a half-beat inset from each barline (never flush), plus
  extra left padding on a new system for the clef/key it restates. The closing
  tutti bars (past the note pass's last measure) have a box but no notes, so the
  `measure_boxes` API synthesises this even-spacing so they are not blank.

`scripts/check_beat_geometry.py` re-runs the inference over every measure and
reports the invariants (in-box, monotonic), the confidence spread, and -- given
a baseline -- which measures moved, so a change that helps one measure cannot
silently regress a good one.

## The invariant, and the failure it guards

**Every beat's `x` must lie inside its own measure box.** The composition is
only valid when the note pass and the grid pass agree on which measure a note
belongs to. They are joined by measure number, and that join is the fragile
part:

- (Historical, fixed 2026-08-04.) The grid/canonical timeline has 126 measures;
  the Audiveris note pass used to stop at 113 because a phantom-chord crash
  aborted export at sheet 14, not because mm.114-126 were musically tacet --
  the piano solo is actively playing there (14-52 notes/measure through m.125).
  "13 measures carry no notes" described the OMR crash's absence of
  recognition, not the music. See [Joseffy source
  notes](../sources/joseffy-reduction.md#august-3--page-14-crash-root-caused-and-repaired)
  for the repair; the note pass now covers all 126 measures. The general
  pattern still applies to any future OMR gap: verify with `measure-content`
  whether missing measures are actually silent before assuming so.
- The Joseffy engraving needed human correction for a spurious measure sliver
  and a dropped barline. Those corrections live in the **display map**. Any
  derived artifact that carries `pdf_x` (the performance beat map) must be
  **rebuilt after** the display map changes, or it keeps stale geometry — which
  is exactly what stretched the right note fraction across the wrong box and
  made the cursor bounce. Rebuilding the beat map backfills those corrections.

Check it after any geometry change: for every measure, `min/max` of the beat
`x`s must fall within `[x0, x1]`. The renderer
([`PdfCoverageOverlay.svelte`](../../webapp/src/PdfCoverageOverlay.svelte)
`beatKnots`) additionally drops any out-of-box beat and falls back to uniform
interpolation, so a bad join degrades gracefully rather than lurching — but the
data should satisfy the invariant on its own.

## Related

- [`score_coordinates.py`](../../src/aimusic/accompaniment/score_coordinates.py)
  — the timing-frame conversions and their pinned tests.
- [Score Bundle Contract](score-bundle-contract.md) — the artifacts and their
  provenance.
- [`LOG.md`](../LOG.md) — the coordinate-mismatch incidents that motivated this.
